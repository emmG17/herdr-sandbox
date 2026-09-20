import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_fetch", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()


class FetchTests(unittest.TestCase):
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
        (self.source / "value.txt").write_text("base\n")
        git("-C", self.source, "add", "value.txt")
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

    def create_committed_worker(self):
        worker = plugin.create_worker("TEST-001", str(self.source))
        repo = worker / "repo"
        git("-C", repo, "config", "user.name", "Worker")
        git("-C", repo, "config", "user.email", "worker@example.invalid")
        (repo / "value.txt").write_text("worker change\n")
        (repo / "added.txt").write_text("added\n")
        git("-C", repo, "add", "value.txt", "added.txt")
        git("-C", repo, "commit", "-qm", "worker change")
        return worker, git("-C", repo, "rev-parse", "HEAD")

    def test_fetches_worker_commit_without_changing_source_and_survives_destruction(self):
        worker, worker_head = self.create_committed_worker()
        branch_before = git("-C", self.source, "branch", "--show-current")
        status_before = git("-C", self.source, "status", "--porcelain")

        result = plugin.fetch_worker("TEST-001")

        self.assertEqual(result["commit"], worker_head)
        self.assertEqual(git("-C", self.source, "rev-parse", "FETCH_HEAD"), worker_head)
        self.assertEqual(result["changed_files"], ["added.txt", "value.txt"])
        self.assertIn("added.txt", result["diff_stat"])
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)
        self.assertEqual(git("-C", self.source, "branch", "--show-current"), branch_before)
        self.assertEqual(git("-C", self.source, "status", "--porcelain"), status_before)

        with patch.object(plugin, "remove_worker_container"):
            self.assertTrue(plugin.destroy_worker("TEST-001"))
        self.assertFalse(worker.exists())
        self.assertEqual(git("-C", self.source, "rev-parse", "FETCH_HEAD"), worker_head)
        self.assertEqual(git("-C", self.source, "show", "--format=%s", "--no-patch", "FETCH_HEAD"), "worker change")

    def test_fetch_action_reads_fixed_worker_id_and_reports_review_details(self):
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG + '\n[fetch]\nid = "configured"\n')
        result = {"commit": "a" * 40, "changed_files": ["one.py"], "diff_stat": " one.py | 1 +\n"}
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "fetch"]), \
                patch.object(plugin, "fetch_worker", return_value=result) as fetch, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(plugin.main(), 0)
        fetch.assert_called_once_with("configured")
        self.assertIn("Commit: " + "a" * 40, output.getvalue())
        self.assertIn("Changed files:\none.py", output.getvalue())

    def test_manifest_exposes_fetch_action(self):
        with (SCRIPT.parent.parent / "herdr-plugin.toml").open("rb") as stream:
            manifest = tomllib.load(stream)
        action = next(action for action in manifest["actions"] if action["id"] == "fetch")
        self.assertEqual(action["command"], ["python3", "bin/herdr-sandbox", "fetch"])


if __name__ == "__main__":
    unittest.main()
