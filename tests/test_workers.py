import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from tests.helpers import git


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_workers", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class WorkerInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "source"
        self.source.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.config = root / "config"
        self.config.mkdir()
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
        git("init", "-q", self.source)
        git("-C", self.source, "config", "user.name", "Test")
        git("-C", self.source, "config", "user.email", "test@example.invalid")
        (self.source / "value.txt").write_text("first\n")
        git("-C", self.source, "add", "value.txt")
        git("-C", self.source, "commit", "-qm", "first")
        self.first = git("-C", self.source, "rev-parse", "HEAD")
        git("-C", self.source, "branch", "base", self.first)
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)
        self.container = patch.object(plugin, "container_status", return_value="not-found")
        self.container.start()
        self.addCleanup(self.container.stop)

    def create_worker(self, worker_id="TEST-001"):
        return plugin.create_worker(worker_id, str(self.source), "base")

    def test_list_rebuilds_view_from_persisted_state_and_git_reality(self):
        self.create_worker()

        records = plugin.list_workers()

        self.assertEqual([record["id"] for record in records], ["TEST-001"])
        record = records[0]
        self.assertEqual(record["source_repo"], str(self.source))
        self.assertEqual(record["repository"], str(self.state / "workers" / "TEST-001" / "repo"))
        self.assertEqual(record["base_ref"], "base")
        self.assertEqual(record["branch"], "agent/TEST-001")
        self.assertEqual(record["status"], "created")
        self.assertEqual(record["head"], self.first)
        self.assertFalse(record["dirty"])
        self.assertEqual(record["container"], "not-found")
        self.assertIn("TEST-001", plugin.format_workers(records))

    def test_inspect_exposes_ordinary_git_log_status_and_base_diff(self):
        worker = self.create_worker()
        clone = worker / "repo"
        git("-C", clone, "config", "user.name", "Worker")
        git("-C", clone, "config", "user.email", "worker@example.invalid")
        (clone / "value.txt").write_text("worker change\n")
        git("-C", clone, "commit", "-qam", "worker change")
        (clone / "untracked.txt").write_text("untracked\n")

        record = plugin.inspect_worker("TEST-001")

        self.assertEqual(record["actual_branch"], "agent/TEST-001")
        self.assertTrue(record["dirty"])
        self.assertIn("worker change", record["git_log"])
        self.assertIn("untracked.txt", record["git_status"])
        self.assertIn("worker change", record["git_diff"])
        rendered = plugin.format_worker(record)
        self.assertIn("Git log:", rendered)
        self.assertIn("Git status:", rendered)
        self.assertIn("Git diff base...HEAD:", rendered)

    def test_live_container_overrides_persisted_lifecycle_status(self):
        worker = self.create_worker()
        metadata_path = worker / "worker.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["status"] = "created"
        metadata_path.write_text(json.dumps(metadata))
        with patch.object(plugin, "container_status", return_value="running"):
            record = plugin.inspect_worker("TEST-001")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["container"], "running")

    def test_live_branch_is_displayed_when_metadata_is_stale(self):
        worker = self.create_worker()
        metadata_path = worker / "worker.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["branch"] = "agent/old-name"
        metadata_path.write_text(json.dumps(metadata))

        record = plugin.inspect_worker("TEST-001")

        self.assertEqual(record["branch"], "agent/TEST-001")
        self.assertEqual(record["metadata_branch"], "agent/old-name")
        self.assertTrue(any("Branch differs from metadata" in issue for issue in record["issues"]))

    def test_running_metadata_without_container_is_reported_stale(self):
        worker = self.create_worker()
        metadata_path = worker / "worker.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["status"] = "running"
        metadata_path.write_text(json.dumps(metadata))

        record = plugin.inspect_worker("TEST-001")

        self.assertEqual(record["status"], "stale")
        self.assertTrue(any("no corresponding container" in issue for issue in record["issues"]))

    def test_running_metadata_with_unavailable_container_is_unknown(self):
        worker = self.create_worker()
        metadata_path = worker / "worker.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["status"] = "running"
        metadata_path.write_text(json.dumps(metadata))
        with patch.object(plugin, "container_status", return_value="error: cannot connect"):
            record = plugin.inspect_worker("TEST-001")

        self.assertEqual(record["status"], "unknown")
        self.assertTrue(any("live container state is unavailable" in issue for issue in record["issues"]))

    def test_missing_state_is_kept_and_reported(self):
        worker = self.create_worker()
        shutil.rmtree(worker / "repo")
        missing_metadata = self.state / "workers" / "MISSING-METADATA"
        missing_metadata.mkdir(parents=True)
        records = plugin.list_workers()

        by_id = {record["id"]: record for record in records}
        self.assertEqual(by_id["TEST-001"]["status"], "missing-repo")
        self.assertTrue(any("Worker repository is missing" in issue for issue in by_id["TEST-001"]["issues"]))
        self.assertTrue(any("Missing worker metadata" in issue for issue in by_id["MISSING-METADATA"]["issues"]))
        self.assertIn("MISSING-METADATA", plugin.format_workers(records))

    def test_missing_source_is_reported_without_hiding_worker(self):
        worker = self.create_worker()
        metadata_path = worker / "worker.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["source_repo"] = str(self.source / "removed")
        metadata_path.write_text(json.dumps(metadata))

        record = plugin.inspect_worker("TEST-001")

        self.assertTrue(any("Source repository is missing" in issue for issue in record["issues"]))
        self.assertEqual(record["head"], self.first)

    def test_container_status_reports_running_container_when_podman_exists(self):
        self.container.stop()
        completed = subprocess.CompletedProcess([], 0, "running\n", "")
        with patch.object(plugin.shutil, "which", return_value="/usr/bin/podman"), patch.object(plugin, "run", return_value=completed) as run:
            self.assertEqual(plugin.container_status("TEST-001"), "running")
        run.assert_called_once_with("podman", "inspect", "--format", "{{.State.Status}}", "herdr-TEST-001")

    def test_container_status_reports_runtime_error_instead_of_not_found(self):
        self.container.stop()
        completed = subprocess.CompletedProcess([], 125, "", "cannot connect to Podman socket")
        with patch.object(plugin.shutil, "which", return_value="/usr/bin/podman"), patch.object(plugin, "run", return_value=completed):
            self.assertEqual(plugin.container_status("TEST-001"), "error: cannot connect to Podman socket")

        worker = self.create_worker()
        with patch.object(plugin.shutil, "which", return_value="/usr/bin/podman"), patch.object(plugin, "run", return_value=completed):
            record = plugin.inspect_worker("TEST-001")
        self.assertEqual(record["container"], "error: cannot connect to Podman socket")
        self.assertTrue(any("Unable to inspect worker container" in issue for issue in record["issues"]))

    def test_cli_list_json_and_inspect_text(self):
        self.create_worker()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "list", "--json"]), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(plugin.main(), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload[0]["id"], "TEST-001")

        with patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect", "TEST-001"]), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(plugin.main(), 0)
        self.assertIn("Worker: TEST-001", output.getvalue())
        self.assertIn("Git status:", output.getvalue())

        (self.config / "config.toml").unlink()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect", "TEST-001"]), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(plugin.main(), 0)
        self.assertIn("Worker: TEST-001", output.getvalue())

    def test_manifest_exposes_list_and_inspect_actions(self):
        manifest = tomllib.loads((SCRIPT.parent.parent / "herdr-plugin.toml").read_text())
        action_ids = {action["id"] for action in manifest["actions"]}
        self.assertTrue({"list", "inspect"}.issubset(action_ids))

    def test_inspect_rejects_missing_or_unsafe_worker(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            plugin.inspect_worker("missing")
        with self.assertRaises(ValueError):
            plugin.inspect_worker("../escape")


if __name__ == "__main__":
    unittest.main()
