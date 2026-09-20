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
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_cherry_pick", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()


class CherryPickTests(unittest.TestCase):
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

    def create_and_fetch_worker(self, contents="worker change\n"):
        worker = plugin.create_worker("TEST-001", str(self.source))
        repo = worker / "repo"
        git("-C", repo, "config", "user.name", "Worker")
        git("-C", repo, "config", "user.email", "worker@example.invalid")
        (repo / "value.txt").write_text(contents)
        git("-C", repo, "commit", "-am", "worker change", "-q")
        commit = git("-C", repo, "rev-parse", "HEAD")
        plugin.fetch_worker("TEST-001")
        return commit

    def test_requires_explicit_fetch_and_selected_full_commit(self):
        worker = plugin.create_worker("TEST-001", str(self.source))
        with self.assertRaisesRegex(ValueError, "40-character SHA"):
            plugin.cherry_pick_worker("TEST-001", "FETCH_HEAD")
        with self.assertRaisesRegex(ValueError, "Run fetch"):
            plugin.cherry_pick_worker("TEST-001", "a" * 40)
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)
        self.assertTrue(worker.exists())

    def test_cherry_picks_only_after_explicit_invocation(self):
        commit = self.create_and_fetch_worker()
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)

        result = plugin.cherry_pick_worker("TEST-001", commit)

        self.assertEqual(result["commit"], commit)
        self.assertEqual(result["integrated_commit"], git("-C", self.source, "rev-parse", "HEAD"))
        self.assertEqual((self.source / "value.txt").read_text(), "worker change\n")

    def test_conflict_leaves_continue_and_abort_recovery_path(self):
        commit = self.create_and_fetch_worker("worker change\n")
        (self.source / "value.txt").write_text("source change\n")
        git("-C", self.source, "commit", "-am", "source change", "-q")

        with self.assertRaisesRegex(ValueError, "cherry-pick --continue") as error:
            plugin.cherry_pick_worker("TEST-001", commit)

        self.assertIn("cherry-pick --abort", str(error.exception))
        self.assertTrue((self.source / ".git" / "CHERRY_PICK_HEAD").is_file())
        git("-C", self.source, "cherry-pick", "--abort")

    def test_rejects_commit_from_a_different_worker(self):
        self.create_and_fetch_worker()
        other = plugin.create_worker("OTHER-001", str(self.source))
        other_repo = other / "repo"
        git("-C", other_repo, "config", "user.name", "Worker")
        git("-C", other_repo, "config", "user.email", "worker@example.invalid")
        (other_repo / "other.txt").write_text("other worker\n")
        git("-C", other_repo, "add", "other.txt")
        git("-C", other_repo, "commit", "-qm", "other worker")
        other_commit = git("-C", other_repo, "rev-parse", "HEAD")
        plugin.fetch_worker("OTHER-001")

        with self.assertRaisesRegex(ValueError, "not a reviewed change from worker TEST-001"):
            plugin.cherry_pick_worker("TEST-001", other_commit)
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)

    def test_action_uses_configured_explicit_worker_and_commit(self):
        commit = "a" * 40
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG + f'\n[cherry_pick]\nid = "configured"\ncommit = "{commit}"\n')
        result = {"commit": commit, "integrated_commit": "b" * 40, "source": "/tmp/source"}
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "cherry-pick"]), \
                patch.object(plugin, "cherry_pick_worker", return_value=result) as cherry_pick:
            self.assertEqual(plugin.main(), 0)
        cherry_pick.assert_called_once_with("configured", commit)

    def test_action_requires_both_explicit_inputs(self):
        stderr = io.StringIO()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "cherry-pick"]), patch.object(plugin.sys, "stderr", stderr):
            self.assertEqual(plugin.main(), 1)
        self.assertIn("Worker ID is required", stderr.getvalue())

    def test_manifest_exposes_cherry_pick_action(self):
        with (SCRIPT.parent.parent / "herdr-plugin.toml").open("rb") as stream:
            manifest = tomllib.load(stream)
        action = next(action for action in manifest["actions"] if action["id"] == "cherry-pick")
        self.assertEqual(action["command"], ["python3", "bin/herdr-sandbox", "cherry-pick"])


if __name__ == "__main__":
    unittest.main()
