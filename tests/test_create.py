import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.helpers import git


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_create", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class CreateTests(unittest.TestCase):
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
        (self.source / "value.txt").write_text("first\n")
        git("-C", self.source, "add", "value.txt")
        git("-C", self.source, "commit", "-qm", "first")
        self.first = git("-C", self.source, "rev-parse", "HEAD")
        git("-C", self.source, "branch", "base", self.first)
        (self.source / "value.txt").write_text("second\n")
        git("-C", self.source, "commit", "-qam", "second")
        self.head = git("-C", self.source, "rev-parse", "HEAD")
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)

    def test_creates_independent_clone_at_requested_base(self):
        before = git("-C", self.source, "status", "--porcelain")
        worker = plugin.create_worker("TEST-001", str(self.source), "base")
        clone = worker / "repo"
        metadata = json.loads((worker / "worker.json").read_text())
        self.assertEqual(worker, self.state / "workers" / "TEST-001")
        self.assertEqual(git("-C", clone, "rev-parse", "HEAD"), self.first)
        self.assertEqual(git("-C", clone, "branch", "--show-current"), "agent/TEST-001")
        self.assertEqual((clone / "value.txt").read_text(), "first\n")
        self.assertEqual((clone / ".git").resolve(), clone / ".git")
        self.assertTrue((clone / ".git").is_dir())
        self.assertNotEqual((clone / ".git").resolve(), (self.source / ".git").resolve())
        source_object = self.source / ".git" / "objects" / self.first[:2] / self.first[2:]
        clone_object = clone / ".git" / "objects" / self.first[:2] / self.first[2:]
        self.assertNotEqual(source_object.stat().st_ino, clone_object.stat().st_ino)
        self.assertEqual(metadata["base_ref"], "base")
        self.assertEqual(metadata["base_commit"], self.first)
        self.assertEqual(metadata["branch"], "agent/TEST-001")
        self.assertEqual(metadata["source_repo"], str(self.source))
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.head)
        self.assertEqual(git("-C", self.source, "status", "--porcelain"), before)
        self.assertFalse(git("-C", self.source, "branch", "--list", "agent/TEST-001"))

    def test_infers_source_from_herdr_context(self):
        with patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_cwd": str(self.source)})}):
            worker = plugin.create_worker("context")
        self.assertEqual(git("-C", worker / "repo", "rev-parse", "HEAD"), self.head)

    def test_rejects_duplicate_without_touching_existing_worker(self):
        worker = plugin.create_worker("same", str(self.source))
        with self.assertRaisesRegex(ValueError, "already exists"):
            plugin.create_worker("same", str(self.source), "base")
        self.assertEqual(git("-C", worker / "repo", "rev-parse", "HEAD"), self.head)

    def test_rejects_invalid_ids_and_refs_without_creating_worker(self):
        for worker_id in ("../escape", "bad/name", "..", "bad..id", "-bad", "bad name"):
            with self.subTest(worker_id=worker_id), self.assertRaises(ValueError):
                plugin.create_worker(worker_id, str(self.source))
        for base_ref in ("missing", "--help", "base ref", ""):
            with self.subTest(base_ref=base_ref), self.assertRaises(ValueError):
                plugin.create_worker("ref-test", str(self.source), base_ref)
        self.assertFalse((self.state / "workers").exists())

    def test_rejects_state_inside_source(self):
        with patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(self.source / "plugin-state")}):
            (self.source / "plugin-state").mkdir()
            with self.assertRaisesRegex(ValueError, "outside the source"):
                plugin.create_worker("unsafe", str(self.source))
        self.assertFalse((self.source / "plugin-state" / "workers").exists())

    def test_rejects_workers_directory_symlink_escape(self):
        (self.state / "workers").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "real directory"):
            plugin.create_worker("unsafe", str(self.source))
        self.assertFalse((self.source / "unsafe").exists())

    def test_removes_partial_clone_if_git_fails(self):
        original_git = plugin.git

        def fail_switch(*args):
            if "switch" in args:
                raise ValueError("switch failed")
            return original_git(*args)

        with patch.object(plugin, "git", side_effect=fail_switch):
            with self.assertRaisesRegex(ValueError, "switch failed"):
                plugin.create_worker("partial", str(self.source))
        self.assertFalse((self.state / "workers" / "partial").exists())

    def test_noninteractive_action_generates_worker_id(self):
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "create"]), \
                patch.object(plugin.sys, "stdin", io.StringIO()), \
                patch.object(plugin, "create_worker", return_value=Path("/tmp/generated")) as create:
            self.assertEqual(plugin.main(), 0)
        self.assertRegex(create.call_args.args[0], r"^worker-[0-9a-f]{12}$")

    def test_noninteractive_action_uses_configured_inputs(self):
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG + f'\n[create]\nid = "chosen"\nsource_repo = "{self.source}"\nbase_ref = "base"\n')
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "create"]), \
                patch.object(plugin.sys, "stdin", io.StringIO()), \
                patch.object(plugin, "create_worker", return_value=Path("/tmp/chosen")) as create:
            self.assertEqual(plugin.main(), 0)
        create.assert_called_once_with("chosen", str(self.source), "base")


if __name__ == "__main__":
    unittest.main()
