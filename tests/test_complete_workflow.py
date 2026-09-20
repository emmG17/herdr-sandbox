import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.helpers import git


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_complete_workflow", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class CompleteWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "source"
        self.source.mkdir()
        git("-C", self.source, "init", "-q")
        git("-C", self.source, "config", "user.name", "Source")
        git("-C", self.source, "config", "user.email", "source@example.invalid")
        (self.source / "source.txt").write_text("source stays protected\n")
        git("-C", self.source, "add", "source.txt")
        git("-C", self.source, "commit", "-qm", "initial source")
        self.source_head = git("-C", self.source, "rev-parse", "HEAD")

        self.config = root / "config"
        self.config.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        (self.config / "codex").mkdir()
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_create_execute_inspect_fetch_review_and_destroy_preserves_source(self):
        with patch.object(plugin, "check", return_value=[]):
            worker = plugin.create_worker("FLOW-001", str(self.source))

        worker_repo = worker / "repo"
        self.assertNotEqual(worker_repo.resolve(), self.source.resolve())
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)

        def execute_in_worker(*args, **kwargs):
            (worker_repo / "worker.txt").write_text("worker-only change\n")
            return subprocess.CompletedProcess(args[0], 0, "completed\n", "")

        with patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin, "_git_read", side_effect=[
                    (0, git("-C", worker_repo, "rev-parse", "HEAD"), ""),
                    (0, " M worker.txt", ""),
                ]), \
                patch.object(plugin.subprocess, "run", side_effect=execute_in_worker):
            result = plugin.execute_worker("FLOW-001", "write worker.txt")
        self.assertEqual(result["exit_status"], 0)
        self.assertTrue(result["dirty"])

        git("-C", worker_repo, "config", "user.name", "Worker")
        git("-C", worker_repo, "config", "user.email", "worker@example.invalid")
        git("-C", worker_repo, "add", "worker.txt")
        git("-C", worker_repo, "commit", "-qm", "worker change")
        worker_head = git("-C", worker_repo, "rev-parse", "HEAD")

        with patch.object(plugin, "container_status", return_value="not-found"):
            inspection = plugin.inspect_worker("FLOW-001")
        self.assertEqual(inspection["head"], worker_head)
        self.assertIn("worker-only change", inspection["git_diff"])

        fetched = plugin.fetch_worker("FLOW-001")
        self.assertEqual(fetched["commit"], worker_head)
        review = plugin.format_fetch_result(fetched)
        self.assertIn("worker.txt", review)
        self.assertEqual(git("-C", self.source, "rev-parse", "FETCH_HEAD"), worker_head)
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)
        self.assertEqual((self.source / "source.txt").read_text(), "source stays protected\n")

        with patch.object(plugin, "remove_worker_container") as remove_container:
            self.assertTrue(plugin.destroy_worker("FLOW-001"))
        remove_container.assert_called_once_with("FLOW-001")
        self.assertFalse(worker.exists())
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)


if __name__ == "__main__":
    unittest.main()
