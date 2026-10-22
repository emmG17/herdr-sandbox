import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
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
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        # Clear pane context by default so create_worker records null pane.
        for key in ("HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "HERDR_SOCKET_PATH"):
            os.environ.pop(key, None)
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)
        self.container = patch.object(plugin, "container_status", return_value="not-found")
        self.container.start()
        self.addCleanup(self.container.stop)

    def create_worker(self, worker_id="TEST-001", agent="codex"):
        return plugin.create_worker(worker_id, str(self.source), "base", agent=agent)

    def test_list_shows_rfc_columns_without_git_query(self):
        with patch.dict(os.environ, {
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_WORKSPACE_ID": "w1",
            "HERDR_SOCKET_PATH": "/tmp/herdr.sock",
        }):
            self.create_worker()

        with patch.object(plugin, "_query_worker_git_snapshot") as query:
            records = plugin.list_workers()
            query.assert_not_called()

        self.assertEqual([record["id"] for record in records], ["TEST-001"])
        record = records[0]
        self.assertEqual(record["agent"], "codex")
        self.assertEqual(record["source_repo"], str(self.source))
        self.assertEqual(record["pane_id"], "w1:p1")
        self.assertEqual(record["status"], "created")
        self.assertIsNone(record["result_branch"])
        self.assertIsNone(record["head"])
        self.assertIsNone(record["dirty"])
        self.assertEqual(record["container"], "not-found")
        rendered = plugin.format_workers(records)
        self.assertIn("AGENT", rendered)
        self.assertIn("SOURCE", rendered)
        self.assertIn("PANE", rendered)
        self.assertIn("RESULT", rendered)
        self.assertIn("TEST-001", rendered)
        self.assertIn("codex", rendered)
        self.assertIn("w1:p1", rendered)
        self.assertIn("STATUS", rendered)
        self.assertIn("CREATED", rendered)
        self.assertNotIn("DIRTY", rendered)
        self.assertNotIn("LATEST", rendered)

    def test_list_shows_fetched_result_branch(self):
        worker = self.create_worker()
        metadata_path = worker / "worker.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["status"] = "fetched"
        metadata["result_branch"] = "sandbox/TEST-001"
        metadata_path.write_text(json.dumps(metadata))

        records = plugin.list_workers()
        self.assertEqual(records[0]["status"], "fetched")
        self.assertEqual(records[0]["result_branch"], "sandbox/TEST-001")
        self.assertIn("sandbox/TEST-001", plugin.format_workers(records))

    def test_inspect_exposes_ordinary_git_log_status_and_base_diff(self):
        worker = self.create_worker()
        clone = worker / "repo"
        git("-C", clone, "config", "user.name", "Worker")
        git("-C", clone, "config", "user.email", "worker@example.invalid")
        (clone / "value.txt").write_text("worker change\n")
        git("-C", clone, "commit", "-qam", "worker change")
        (clone / "untracked.txt").write_text("untracked\n")

        with patch.object(plugin, "_query_worker_git_snapshot", return_value={
            "status_porcelain": " M value.txt\n?? untracked.txt",
            "log": "abc123 worker change",
            "status_short": "## agent/TEST-001\n M value.txt\n?? untracked.txt",
            "diff": "diff --git a/value.txt\nworker change",
            "error": None,
        }):
            record = plugin.inspect_worker("TEST-001")

        self.assertEqual(record["actual_branch"], "agent/TEST-001")
        self.assertTrue(record["dirty"])
        self.assertIn("worker change", record["git_log"])
        self.assertIn("untracked.txt", record["git_status"])
        self.assertIn("worker change", record["git_diff"])
        rendered = plugin.format_worker(record)
        self.assertIn("Git log:", rendered)
        self.assertIn("Git status:", rendered)
        self.assertIn("Git diff", rendered)
        self.assertIn("Review (run in source repo", rendered)
        quoted_source = shlex.quote(str(self.source))
        self.assertIn(f"git -C {quoted_source} log base..sandbox/TEST-001", rendered)
        self.assertIn(f"git -C {quoted_source} diff base...sandbox/TEST-001", rendered)

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

    def test_cli_ls_json_and_inspect_text(self):
        self.create_worker()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "ls", "--json"]), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(plugin.main(), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload[0]["id"], "TEST-001")
        self.assertEqual(payload[0]["agent"], "codex")
        self.assertIn("pane_id", payload[0])
        self.assertIn("result_branch", payload[0])

        with patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect", "TEST-001"]), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(plugin.main(), 0)
        text = output.getvalue()
        self.assertIn("Worker: TEST-001", text)
        self.assertIn("Git status:", text)
        self.assertIn("Review (run in source repo", text)

        (self.config / "config.toml").unlink()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect", "TEST-001"]), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(plugin.main(), 0)
        self.assertIn("Worker: TEST-001", output.getvalue())

    def test_inspect_config_table_is_ignored_with_deprecation_notice(self):
        self.create_worker()
        (self.config / "config.toml").write_text(
            plugin.DEFAULT_CONFIG + '\n[inspect]\nid = "WRONG-ID"\n'
        )
        stderr = io.StringIO()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect", "TEST-001"]), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(plugin.main(), 0)
        self.assertIn("Worker: TEST-001", output.getvalue())
        notice = stderr.getvalue()
        self.assertEqual(notice.count("legacy per-action config tables"), 1)
        self.assertIn("[inspect]", notice)
        self.assertIn("deprecated and ignored", notice)

    def test_inspect_without_id_requires_pane_and_lists_candidates(self):
        self.create_worker("ALPHA-001")
        self.create_worker("BETA-002")
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect"]), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(plugin.main(), 1)
        message = stderr.getvalue()
        self.assertIn("HERDR_PANE_ID is unset", message)
        self.assertIn("ALPHA-001", message)
        self.assertIn("BETA-002", message)

    def test_inspect_without_id_resolves_pane_worker(self):
        with patch.dict(os.environ, {
            "HERDR_PANE_ID": "w1:p9",
            "HERDR_WORKSPACE_ID": "w1",
            "HERDR_SOCKET_PATH": "/tmp/herdr.sock",
        }):
            self.create_worker("PANE-001")
        self.create_worker("OTHER-002")

        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p9"}), \
                contextlib.chdir(self.source), \
                patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(plugin.main(), 0)
        self.assertIn("Worker: PANE-001", output.getvalue())

    def test_inspect_without_id_errors_when_pane_match_is_ambiguous(self):
        with patch.dict(os.environ, {
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_WORKSPACE_ID": "w1",
            "HERDR_SOCKET_PATH": "/tmp/herdr.sock",
        }):
            self.create_worker("ONE-001", agent="codex")
            self.create_worker("TWO-002", agent="claude")

        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1"}), \
                contextlib.chdir(self.source), \
                patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect"]), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(plugin.main(), 1)
        message = stderr.getvalue()
        self.assertIn("Multiple workers match pane w1:p1", message)
        self.assertIn("ONE-001", message)
        self.assertIn("TWO-002", message)

    def test_inspect_without_id_errors_when_pane_has_zero_matches(self):
        with patch.dict(os.environ, {
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_WORKSPACE_ID": "w1",
            "HERDR_SOCKET_PATH": "/tmp/herdr.sock",
        }):
            self.create_worker("OTHER-001")

        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p-missing"}), \
                contextlib.chdir(self.source), \
                patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect"]), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(plugin.main(), 1)
        message = stderr.getvalue()
        self.assertIn("No worker found for pane w1:p-missing", message)
        self.assertIn("OTHER-001", message)

    def test_inspect_without_id_errors_when_cwd_is_outside_git_repo(self):
        self.create_worker("ONLY-001")
        outside = Path(self.temp.name) / "not-a-repo"
        outside.mkdir()
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1"}), \
                contextlib.chdir(outside), \
                patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect"]), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(plugin.main(), 1)
        message = stderr.getvalue()
        self.assertIn("not inside a source Git repository", message)
        self.assertIn("ONLY-001", message)

    def test_inspect_config_id_alone_is_ignored(self):
        self.create_worker("REAL-001")
        (self.config / "config.toml").write_text(
            plugin.DEFAULT_CONFIG + '\n[inspect]\nid = "REAL-001"\n'
        )
        stderr = io.StringIO()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect"]), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(plugin.main(), 1)
        message = stderr.getvalue()
        self.assertEqual(message.count("legacy per-action config tables"), 1)
        self.assertIn("[inspect]", message)
        self.assertIn("deprecated and ignored", message)
        self.assertIn("HERDR_PANE_ID is unset", message)
        self.assertIn("REAL-001", message)

    def test_inspect_ignores_sole_worker_fallback(self):
        self.create_worker("ONLY-001")
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect"]), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(plugin.main(), 1)
        self.assertIn("HERDR_PANE_ID is unset", stderr.getvalue())
        self.assertIn("ONLY-001", stderr.getvalue())

    def test_inspect_review_commands_ignore_malicious_result_branch_metadata(self):
        worker = self.create_worker()
        metadata_path = worker / "worker.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["result_branch"] = "sandbox/$(touch /tmp/pwned);evil"
        metadata_path.write_text(json.dumps(metadata))
        record = plugin.inspect_worker("TEST-001")
        rendered = plugin.format_worker(record)
        quoted_source = shlex.quote(str(self.source))
        self.assertIn(f"git -C {quoted_source} log base..sandbox/TEST-001", rendered)
        review_block = rendered.split("Review", 1)[1].split("Git log", 1)[0]
        self.assertNotIn("$(touch", review_block)
        self.assertNotIn(";evil", review_block)
        self.assertIn("sandbox/TEST-001", review_block)

    def test_manifest_exposes_list_and_inspect_actions(self):
        manifest = tomllib.loads((SCRIPT.parent.parent / "herdr-plugin.toml").read_text())
        action_ids = {action["id"] for action in manifest["actions"]}
        self.assertTrue({"list", "inspect"}.issubset(action_ids))

    def test_inspect_rejects_missing_or_unsafe_worker(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            plugin.inspect_worker("missing")
        with self.assertRaises(ValueError):
            plugin.inspect_worker("../escape")

    def test_format_worker_skips_review_when_source_missing(self):
        worker = self.create_worker()
        metadata_path = worker / "worker.json"
        metadata = json.loads(metadata_path.read_text())
        del metadata["source_repo"]
        metadata_path.write_text(json.dumps(metadata))
        record = plugin.inspect_worker("TEST-001")
        self.assertEqual(record["source_repo"], "-")
        rendered = plugin.format_worker(record)
        self.assertIn("Review: unavailable", rendered)
        self.assertNotIn("git -C - ", rendered)
        self.assertNotIn("git -C '-' ", rendered)

    def test_cwd_repo_root_fail_closed_when_workers_dir_unresolvable(self):
        from unittest.mock import MagicMock

        broken = MagicMock()
        broken.resolve.side_effect = OSError("cannot resolve workers dir")
        with contextlib.chdir(self.source), \
                patch.object(plugin, "state_workers_dir", return_value=broken):
            self.assertIsNone(plugin.cwd_repo_root(state_dir=self.state))

    def test_cwd_repo_root_accepts_gitfile_worktree(self):
        worktree = Path(self.temp.name) / "linked-worktree"
        git("-C", self.source, "worktree", "add", "-q", str(worktree), "HEAD")
        git_entry = worktree / ".git"
        self.assertTrue(git_entry.is_file(), "worktree .git should be a gitfile")
        with contextlib.chdir(worktree):
            self.assertEqual(plugin.cwd_repo_root(state_dir=self.state), worktree.resolve())

    def test_cwd_repo_root_follows_symlinked_parents(self):
        real_parent = Path(self.temp.name) / "real-parent"
        real_parent.mkdir()
        repo = real_parent / "project"
        repo.mkdir()
        git("init", "-q", repo)
        git("-C", repo, "config", "user.name", "Test")
        git("-C", repo, "config", "user.email", "test@example.invalid")
        (repo / "file.txt").write_text("ok\n")
        git("-C", repo, "add", "file.txt")
        git("-C", repo, "commit", "-qm", "init")
        link_parent = Path(self.temp.name) / "link-parent"
        link_parent.symlink_to(real_parent)
        nested = link_parent / "project" / "subdir"
        nested.mkdir()
        with contextlib.chdir(nested):
            root = plugin.cwd_repo_root(state_dir=self.state)
        self.assertEqual(root, repo.resolve())


if __name__ == "__main__":
    unittest.main()
