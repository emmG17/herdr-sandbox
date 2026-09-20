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
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_destroy", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class DestroyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.root / "config"
        self.config.mkdir()
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
        git("init", "-q", self.source)
        git("-C", self.source, "config", "user.name", "Test")
        git("-C", self.source, "config", "user.email", "test@example.invalid")
        (self.source / "source.txt").write_text("source stays put\n")
        git("-C", self.source, "add", "source.txt")
        git("-C", self.source, "commit", "-qm", "initial")
        self.source_head = git("-C", self.source, "rev-parse", "HEAD")
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)

    def create_worker(self, worker_id="TEST-001"):
        return plugin.create_worker(worker_id, str(self.source))

    def test_stops_container_before_removing_worker_and_preserves_source(self):
        worker = self.create_worker()
        other = self.state / "workers" / "OTHER"
        other.mkdir()
        (other / "keep.txt").write_text("keep\n")
        events = []
        responses = [subprocess.CompletedProcess([], 0, "", "")]

        def fake_run(*args):
            events.append(("podman", args))
            return responses.pop(0)

        real_rmtree = shutil.rmtree

        def record_rmtree(path):
            events.append(("rmtree", Path(path)))
            return real_rmtree(path)

        with patch.object(plugin, "run", side_effect=fake_run), patch.object(plugin.shutil, "rmtree", side_effect=record_rmtree):
            self.assertTrue(plugin.destroy_worker("TEST-001"))

        self.assertEqual(events[0], ("podman", ("podman", "rm", "-f", "herdr-TEST-001")))
        self.assertEqual(events[1], ("rmtree", worker))
        self.assertFalse(worker.exists())
        self.assertTrue((other / "keep.txt").exists())
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)
        self.assertEqual(git("-C", self.source, "status", "--porcelain"), "")
        self.assertEqual((self.source / "source.txt").read_text(), "source stays put\n")

    def test_repeated_destroy_is_harmless(self):
        self.create_worker()
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "Error: no such container"),
        ]
        with patch.object(plugin, "run", side_effect=lambda *args: responses.pop(0)):
            self.assertTrue(plugin.destroy_worker("TEST-001"))
            self.assertFalse(plugin.destroy_worker("TEST-001"))
        self.assertFalse((self.state / "workers" / "TEST-001").exists())
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)

    def test_container_failure_keeps_worker_files(self):
        worker = self.create_worker()
        failure = subprocess.CompletedProcess([], 125, "", "Error: cannot connect to Podman")
        with patch.object(plugin, "run", return_value=failure):
            with self.assertRaisesRegex(ValueError, "Unable to remove worker container"):
                plugin.destroy_worker("TEST-001")
        self.assertTrue(worker.exists())
        self.assertTrue((worker / "repo" / ".git").exists())

    def test_ambiguous_container_failure_keeps_worker_files(self):
        worker = self.create_worker()
        ambiguous = subprocess.CompletedProcess([], 1, "", "")
        with patch.object(plugin, "run", return_value=ambiguous):
            with self.assertRaisesRegex(ValueError, "Unable to remove worker container"):
                plugin.destroy_worker("TEST-001")
        self.assertTrue(worker.exists())
        self.assertTrue((worker / "worker.json").exists())

    def test_invalid_id_and_symlinked_storage_cannot_escape_workers(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "do-not-delete").write_text("protected\n")
        for worker_id in ("../outside", "bad/name", "..", "bad..id", "-bad", "bad name"):
            with self.subTest(worker_id=worker_id), self.assertRaises(ValueError):
                plugin.destroy_worker(worker_id)
        self.assertTrue((outside / "do-not-delete").exists())

        workers = self.state / "workers"
        workers.mkdir()
        (workers / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "real directory"):
            plugin.destroy_worker("escape")
        self.assertTrue((outside / "do-not-delete").exists())

    def test_destroy_action_uses_configured_id(self):
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG + '\n[destroy]\nid = "configured"\n')
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "destroy"]), \
                patch.object(plugin, "destroy_worker", return_value=True) as destroy:
            self.assertEqual(plugin.main(), 0)
        destroy.assert_called_once_with("configured")

    def test_manifest_exposes_destroy_action(self):
        with (SCRIPT.parent.parent / "herdr-plugin.toml").open("rb") as stream:
            manifest = tomllib.load(stream)
        action = next(action for action in manifest["actions"] if action["id"] == "destroy")
        self.assertEqual(action["command"], ["python3", "bin/herdr-sandbox", "destroy"])

    def test_destroy_action_requires_deliberate_id(self):
        stderr = io.StringIO()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "destroy"]), patch.object(plugin.sys, "stderr", stderr):
            self.assertEqual(plugin.main(), 1)
        self.assertIn("Worker ID is required", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
