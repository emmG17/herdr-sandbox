import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
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
        (self.config / "codex" / "auth.json").write_text(
            '{"tokens":{"access_token":"test","refresh_token":"test"}}\n'
        )
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

        commit_code = (
            "from pathlib import Path; "
            "import subprocess; "
            f"repo = Path({str(worker_repo)!r}); "
            "(repo / 'worker.txt').write_text('worker-only change\\n'); "
            "subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Worker'], check=True); "
            "subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'worker@example.invalid'], check=True); "
            "subprocess.run(['git', '-C', str(repo), 'add', 'worker.txt'], check=True); "
            "subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'worker change'], check=True); "
            "print('completed', flush=True)"
        )
        with patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin, "execute_command", return_value=[sys.executable, "-c", commit_code]), \
                patch.object(plugin, "_query_worker_git_snapshot", return_value={
                    "status_porcelain": "",
                    "log": "abc worker change",
                    "status_short": "## agent/FLOW-001",
                    "diff": "worker-only change",
                    "error": None,
                }), \
                patch.object(plugin.sys, "stdout", new_callable=io.StringIO) as worker_output, \
                patch.object(plugin.sys, "stderr", new_callable=io.StringIO):
            result = plugin.execute_worker("FLOW-001", "write worker.txt")
        self.assertEqual(result["exit_status"], 0)
        self.assertFalse(result["dirty"])
        self.assertIn("completed\n", worker_output.getvalue())
        worker_head = git("-C", worker_repo, "rev-parse", "HEAD")
        self.assertEqual(result["head"], worker_head)
        self.assertEqual(result["result_branch"], "sandbox/FLOW-001")
        self.assertEqual(result["commit_count"], 1)

        with patch.object(plugin, "container_status", return_value="not-found"), \
                patch.object(plugin, "_query_worker_git_snapshot", return_value={
                    "status_porcelain": "",
                    "log": "abc worker change",
                    "status_short": "## agent/FLOW-001",
                    "diff": "worker-only change",
                    "error": None,
                }):
            inspection = plugin.inspect_worker("FLOW-001")
        self.assertEqual(inspection["head"], worker_head)
        self.assertIn("worker-only change", inspection["git_diff"])

        self.assertEqual(
            git("-C", self.source, "rev-parse", "refs/heads/sandbox/FLOW-001"),
            worker_head,
        )
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)
        self.assertEqual((self.source / "source.txt").read_text(), "source stays protected\n")

        with patch.object(plugin, "remove_worker_container") as remove_container:
            self.assertTrue(plugin.destroy_worker("FLOW-001"))
        remove_container.assert_called_once_with("FLOW-001")
        self.assertFalse(worker.exists())
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)


if __name__ == "__main__":
    unittest.main()
