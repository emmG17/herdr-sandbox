import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from tests.helpers import git


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_plain_git", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class PlainGitIntegrationTests(unittest.TestCase):
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
        current = git("-C", self.source, "branch", "--show-current")
        if current and current != "main":
            git("-C", self.source, "branch", "-m", current, "main")
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)
        snapshot = patch.object(plugin, "_query_worker_git_snapshot", return_value={
            "status_porcelain": "",
            "log": "",
            "status_short": "",
            "diff": "",
            "error": None,
        })
        snapshot.start()
        self.addCleanup(snapshot.stop)

    def create_and_fetch_worker(self, contents="worker change\n"):
        worker = plugin.create_worker("TEST-001", str(self.source))
        repo = worker / "repo"
        git("-C", repo, "config", "user.name", "Worker")
        git("-C", repo, "config", "user.email", "worker@example.invalid")
        (repo / "value.txt").write_text(contents)
        git("-C", repo, "commit", "-am", "worker change", "-q")
        commit = git("-C", repo, "rev-parse", "HEAD")
        result = plugin.fetch_worker("TEST-001")
        return commit, result

    def test_fetch_writes_sandbox_ref_and_plain_git_merge_integrates(self):
        commit, fetched = self.create_and_fetch_worker()
        self.assertEqual(fetched["branch"], "sandbox/TEST-001")
        self.assertEqual(
            git("-C", self.source, "rev-parse", "refs/heads/sandbox/TEST-001"),
            commit,
        )
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)

        review = plugin.format_fetch_result(fetched)
        self.assertIn("git log ", review)
        self.assertIn("..sandbox/TEST-001", review)
        self.assertIn("git diff ", review)
        self.assertIn("...sandbox/TEST-001", review)
        self.assertIn("git merge sandbox/TEST-001", review)

        git("-C", self.source, "merge", "--no-edit", "sandbox/TEST-001")
        self.assertEqual((self.source / "value.txt").read_text(), "worker change\n")
        self.assertNotEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)

    def test_legacy_cherry_pick_table_is_ignored_with_notice(self):
        (self.config / "config.toml").write_text(
            plugin.DEFAULT_CONFIG
            + '\n[cherry_pick]\nid = "STALE"\ncommit = "'
            + ("a" * 40)
            + '"\n'
        )
        stderr = io.StringIO()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "list"]), \
                patch.object(plugin.sys, "stderr", stderr):
            self.assertEqual(plugin.main(), 0)
        self.assertIn("[cherry_pick] is deprecated and ignored", stderr.getvalue())

    def test_manifest_no_longer_exposes_cherry_pick_action(self):
        with (SCRIPT.parent.parent / "herdr-plugin.toml").open("rb") as stream:
            manifest = tomllib.load(stream)
        ids = {action["id"] for action in manifest["actions"]}
        self.assertNotIn("cherry-pick", ids)
        fetch = next(action for action in manifest["actions"] if action["id"] == "fetch")
        self.assertEqual(fetch["command"], ["python3", "bin/herdr-sandbox", "fetch"])
        self.assertIn("sandbox/<id>", fetch["description"])


if __name__ == "__main__":
    unittest.main()
