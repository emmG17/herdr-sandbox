#!/usr/bin/env python3
"""Tests for experimental [workspace].strategy = reflink (#14)."""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import git


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_reflink", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


def _config_with_workspace(strategy="clone", copy_untracked=None, extra_settings=""):
    """Rewrite the example [workspace] table in place (DEFAULT_CONFIG already has one)."""
    if copy_untracked is None:
        copy_untracked = []
    array = json.dumps(copy_untracked)
    text = plugin.DEFAULT_CONFIG
    text = text.replace(
        'strategy = "clone"          # "clone" | "reflink" (experimental)',
        f'strategy = "{strategy}"',
        1,
    )
    text = text.replace(
        'copy_untracked = []         # reflink only, e.g. ["node_modules", "target", ".venv"]',
        f"copy_untracked = {array}",
        1,
    )
    if extra_settings:
        text = text.replace(
            f'strategy = "{strategy}"\n',
            f'strategy = "{strategy}"\n{extra_settings.rstrip()}\n',
            1,
        )
    return text


def _reflink_test_root():
    """Return a directory on a reflink-capable filesystem, or None."""
    env = os.environ.get("HERDR_REFLINK_TEST_ROOT", "").strip()
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/tmp/hs-xfs-mnt"))
    for root in candidates:
        if not root.is_dir():
            continue
        probe_src = root / f".probe-src-{os.getpid()}"
        probe_dst = root / f".probe-dst-{os.getpid()}"
        try:
            probe_src.write_bytes(b"probe\n")
            plugin.reflink_file(probe_src, probe_dst)
            if probe_src.stat().st_ino == probe_dst.stat().st_ino:
                return None
            return root
        except OSError:
            continue
        finally:
            for path in (probe_dst, probe_src):
                try:
                    path.unlink()
                except OSError:
                    pass
    return None


def _copy2_as_reflink(src, dst):
    """Test double: regular copy that preserves the reflink_file contract."""
    src = Path(src)
    dst = Path(dst)
    if src.is_symlink():
        raise OSError(f"Reflink source must be a regular file: {src}")
    if dst.exists() or dst.is_symlink():
        raise OSError(f"Reflink destination already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


class WorkspaceSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name)
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)

    def test_defaults_to_clone(self):
        settings = plugin.workspace_settings(self.config)
        self.assertEqual(settings["strategy"], "clone")
        self.assertEqual(settings["copy_untracked"], [])

    def test_parses_reflink_and_allowlist(self):
        (self.config / "config.toml").write_text(
            _config_with_workspace("reflink", ["node_modules", "target"])
        )
        settings = plugin.workspace_settings(self.config)
        self.assertEqual(settings["strategy"], "reflink")
        self.assertEqual(settings["copy_untracked"], ["node_modules", "target"])

    def test_rejects_unknown_keys_via_permitted_settings(self):
        (self.config / "config.toml").write_text(
            _config_with_workspace("clone", extra_settings="extra = true")
        )
        with self.assertRaisesRegex(ValueError, "not permitted"):
            plugin.workspace_settings(self.config)

    def test_rejects_bad_strategy_and_allowlist_entries(self):
        (self.config / "config.toml").write_text(_config_with_workspace("hardlink"))
        with self.assertRaisesRegex(ValueError, "clone' or 'reflink"):
            plugin.workspace_settings(self.config)

        for entry in (
            "/etc/passwd",
            "../secret",
            ".git",
            ".git/hooks",
            "foo/.git/bar",
            ".",
            "./",
            "",
            "foo/../bar",
        ):
            with self.subTest(entry=entry):
                with self.assertRaises(ValueError):
                    plugin.validate_copy_untracked_entry(entry)
        # Path collapses a lone '.' segment, so foo/./bar normalizes to foo/bar.
        self.assertEqual(plugin.validate_copy_untracked_entry("foo/./bar"), "foo/bar")

    def test_rejects_dot_and_nested_git_in_config_parse(self):
        (self.config / "config.toml").write_text(
            _config_with_workspace("reflink", ["."])
        )
        with self.assertRaises(ValueError):
            plugin.workspace_settings(self.config)
        (self.config / "config.toml").write_text(
            _config_with_workspace("reflink", ["vendor/.git/objects"])
        )
        with self.assertRaisesRegex(ValueError, r"\.git"):
            plugin.workspace_settings(self.config)


class ReflinkHelpersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "ok.txt").write_text("ok\n")
        git("init", "-q", self.source)
        git("-C", self.source, "config", "user.name", "Test")
        git("-C", self.source, "config", "user.email", "test@example.invalid")
        (self.source / ".gitignore").write_text(".env\nnode_modules/\n")
        git("-C", self.source, "add", "ok.txt", ".gitignore")
        git("-C", self.source, "commit", "-qm", "init")

    def test_copy_allowlist_skips_escaping_symlinks_and_nested_git(self):
        outside = self.root / "secret.env"
        outside.write_text("SECRET=1\n")
        mods = self.source / "node_modules"
        mods.mkdir()
        (mods / "keep.js").write_text("keep\n")
        (mods / "leak").symlink_to(outside)
        nested_git = mods / ".git"
        nested_git.mkdir()
        (nested_git / "config").write_text("stolen\n")
        sub = mods / "pkg"
        sub.mkdir()
        (sub / "index.js").write_text("pkg\n")

        dest = self.root / "dest"
        dest.mkdir()
        stderr = io.StringIO()
        with patch.object(plugin, "reflink_file", side_effect=_copy2_as_reflink), \
                patch.object(plugin.sys, "stderr", stderr):
            plugin.copy_allowlisted_untracked(self.source, dest, ["node_modules"])

        self.assertTrue((dest / "node_modules" / "keep.js").is_file())
        self.assertTrue((dest / "node_modules" / "pkg" / "index.js").is_file())
        self.assertFalse((dest / "node_modules" / "leak").exists())
        self.assertFalse((dest / "node_modules" / ".git").exists())
        self.assertIn("skipping symlink", stderr.getvalue())

    def test_top_level_symlink_allowlist_entry_rejected(self):
        outside = self.root / "outside-dir"
        outside.mkdir()
        (outside / "x").write_text("x\n")
        (self.source / "node_modules").symlink_to(outside)
        dest = self.root / "dest"
        dest.mkdir()
        with patch.object(plugin, "reflink_file", side_effect=_copy2_as_reflink):
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                plugin.copy_allowlisted_untracked(self.source, dest, ["node_modules"])

    def test_relative_symlink_within_allowlist_kept_absolute_and_escape_skipped(self):
        mods = self.source / "node_modules"
        pnpm = mods / ".pnpm" / "pkg@1" / "node_modules" / "pkg"
        pnpm.mkdir(parents=True)
        (pnpm / "index.js").write_text("pkg\n")
        bin_dir = mods / ".bin"
        bin_dir.mkdir()
        # pnpm-style relative link that stays inside node_modules
        (bin_dir / "tool").symlink_to("../.pnpm/pkg@1/node_modules/pkg/index.js")
        # absolute symlink — never recreate
        (mods / "abs").symlink_to(self.source / "ok.txt")
        # relative link escaping the allowlist entry to .env
        (self.source / ".env").write_text("SECRET=1\n")
        (mods / "to-env").symlink_to("../../.env")

        dest = self.root / "dest"
        dest.mkdir()
        stderr = io.StringIO()
        with patch.object(plugin, "reflink_file", side_effect=_copy2_as_reflink), \
                patch.object(plugin.sys, "stderr", stderr):
            plugin.copy_allowlisted_untracked(self.source, dest, ["node_modules"])

        self.assertTrue((dest / "node_modules" / ".bin" / "tool").is_symlink())
        self.assertEqual(
            os.readlink(dest / "node_modules" / ".bin" / "tool"),
            "../.pnpm/pkg@1/node_modules/pkg/index.js",
        )
        self.assertFalse((dest / "node_modules" / "abs").exists())
        self.assertFalse((dest / "node_modules" / "to-env").exists())
        notices = [line for line in stderr.getvalue().splitlines() if "skipping symlink" in line]
        self.assertGreaterEqual(len(notices), 2)

    def test_hardlink_to_env_inside_node_modules_skipped(self):
        (self.source / ".env").write_text("SECRET=do-not-copy\n")
        mods = self.source / "node_modules" / "pkg"
        mods.mkdir(parents=True)
        (mods / "index.js").write_text("deps\n")
        leaked = mods / "leaked.env"
        os.link(self.source / ".env", leaked)

        dest = self.root / "dest"
        dest.mkdir()
        stderr = io.StringIO()
        with patch.object(plugin, "reflink_file", side_effect=_copy2_as_reflink), \
                patch.object(plugin.sys, "stderr", stderr):
            plugin.copy_allowlisted_untracked(self.source, dest, ["node_modules"])

        self.assertTrue((dest / "node_modules" / "pkg" / "index.js").is_file())
        self.assertFalse((dest / "node_modules" / "pkg" / "leaked.env").exists())
        self.assertIn("skipping hardlink", stderr.getvalue())

    def test_reflink_file_refuses_symlink_source_with_nofollow(self):
        target = self.root / "target.txt"
        target.write_text("data\n")
        link = self.root / "link.txt"
        link.symlink_to(target)
        dest = self.root / "out.txt"
        with self.assertRaises(OSError):
            plugin.reflink_file(link, dest)
        self.assertFalse(dest.exists())

    def test_reflink_file_o_excl_refuses_preexisting_dest(self):
        src = self.root / "src.txt"
        src.write_text("data\n")
        dest = self.root / "out.txt"
        dest.write_text("existing\n")
        with self.assertRaises(OSError):
            plugin.reflink_file(src, dest)
        self.assertEqual(dest.read_text(), "existing\n")


class ReflinkCreateTests(unittest.TestCase):
    """create_worker reflink behavior with a mocked copy primitive (works on ext4)."""

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
        (self.config / "config.toml").write_text(
            _config_with_workspace("reflink", ["node_modules"])
        )
        git("init", "-q", self.source)
        git("-C", self.source, "config", "user.name", "Test")
        git("-C", self.source, "config", "user.email", "test@example.invalid")
        (self.source / "value.txt").write_text("tracked\n")
        (self.source / ".gitignore").write_text(".env\nnode_modules/\n")
        git("-C", self.source, "add", "value.txt", ".gitignore")
        git("-C", self.source, "commit", "-qm", "first")
        mods = self.source / "node_modules" / "pkg"
        mods.mkdir(parents=True)
        (mods / "index.js").write_text("deps\n")
        (self.source / ".env").write_text("SECRET=do-not-copy\n")
        outside = self.root / "outside-secret"
        outside.write_text("nope\n")
        (self.source / "node_modules" / "escape").symlink_to(outside)
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)

    def test_fallback_notice_when_probe_fails(self):
        stderr = io.StringIO()
        with patch.object(plugin, "probe_reflink", return_value=False), \
                patch.object(plugin.sys, "stderr", stderr):
            worker = plugin.create_worker("FALLBACK-001", str(self.source))
        notice = [line for line in stderr.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(notice), 1)
        self.assertIn("falling back to clone", notice[0])
        clone = worker / "repo"
        self.assertTrue((clone / "value.txt").is_file())
        self.assertFalse((clone / "node_modules").exists())
        self.assertFalse((clone / ".env").exists())
        metadata = json.loads((worker / "worker.json").read_text())
        self.assertEqual(metadata["workspace_strategy"], "clone")

    def test_allowlist_and_secret_exclusion_with_mocked_reflink(self):
        source_hook = self.source / ".git" / "hooks" / "pre-commit"
        source_hook.write_text("#!/bin/sh\necho source-only-hook\n")
        source_hook.chmod(source_hook.stat().st_mode | stat.S_IXUSR)
        with patch.object(plugin, "probe_reflink", return_value=True), \
                patch.object(plugin, "reflink_file", side_effect=_copy2_as_reflink):
            worker = plugin.create_worker("REFLINK-001", str(self.source))
        clone = worker / "repo"
        self.assertEqual((clone / "value.txt").read_text(), "tracked\n")
        self.assertEqual((clone / "node_modules" / "pkg" / "index.js").read_text(), "deps\n")
        self.assertFalse((clone / ".env").exists())
        self.assertFalse((clone / "node_modules" / "escape").exists())
        alternates = clone / ".git" / "objects" / "info" / "alternates"
        self.assertFalse(alternates.exists())
        worker_hook = clone / ".git" / "hooks" / "pre-commit"
        self.assertTrue(
            (not worker_hook.exists())
            or worker_hook.read_text() != source_hook.read_text()
        )
        status = git("-C", clone, "status", "--porcelain")
        self.assertEqual(status, "")
        self.assertEqual(git("-C", clone, "branch", "--show-current"), "agent/REFLINK-001")
        metadata = json.loads((worker / "worker.json").read_text())
        self.assertEqual(metadata["workspace_strategy"], "reflink")

    def test_create_worker_refuses_dirty_tree_for_reflink(self):
        (self.source / "dirty.txt").write_text("uncommitted\n")
        with patch.object(plugin, "probe_reflink", return_value=True), \
                patch.object(plugin, "reflink_file", side_effect=_copy2_as_reflink):
            with self.assertRaisesRegex(ValueError, "uncommitted or untracked"):
                plugin.create_worker("DIRTY-001", str(self.source))
        self.assertFalse((self.state / "workers" / "DIRTY-001").exists())

    def test_tracked_bytes_come_from_objects_not_assume_unchanged_worktree(self):
        git("-C", self.source, "update-index", "--assume-unchanged", "value.txt")
        (self.source / "value.txt").write_text("worktree-leaked-bytes\n")
        with patch.object(plugin, "probe_reflink", return_value=True), \
                patch.object(plugin, "reflink_file", side_effect=_copy2_as_reflink):
            worker = plugin.create_worker("OBJECTS-001", str(self.source))
        clone = worker / "repo"
        self.assertEqual((clone / "value.txt").read_text(), "tracked\n")
        self.assertNotEqual((clone / "value.txt").read_text(), "worktree-leaked-bytes\n")

    def test_tracked_bytes_ignore_worktree_autocrlf_mutation(self):
        # Simulate a worktree that does not match the blob (e.g. line-ending tools).
        (self.source / "value.txt").write_bytes(b"tracked\r\n")
        git("-C", self.source, "update-index", "--assume-unchanged", "value.txt")
        with patch.object(plugin, "probe_reflink", return_value=True), \
                patch.object(plugin, "reflink_file", side_effect=_copy2_as_reflink):
            worker = plugin.create_worker("CRLF-001", str(self.source))
        self.assertEqual((worker / "repo" / "value.txt").read_bytes(), b"tracked\n")

    def test_hardlink_env_into_allowlist_skipped_during_create(self):
        leaked = self.source / "node_modules" / "pkg" / "leaked.env"
        os.link(self.source / ".env", leaked)
        stderr = io.StringIO()
        with patch.object(plugin, "probe_reflink", return_value=True), \
                patch.object(plugin, "reflink_file", side_effect=_copy2_as_reflink), \
                patch.object(plugin.sys, "stderr", stderr):
            worker = plugin.create_worker("HARDLINK-001", str(self.source))
        clone = worker / "repo"
        self.assertFalse((clone / "node_modules" / "pkg" / "leaked.env").exists())
        self.assertFalse((clone / ".env").exists())
        self.assertIn("skipping hardlink", stderr.getvalue())

    def test_rejects_absolute_and_dotdot_allowlist_at_config_parse(self):
        (self.config / "config.toml").write_text(
            _config_with_workspace("reflink", ["/tmp/x"])
        )
        with self.assertRaisesRegex(ValueError, "relative paths"):
            plugin.workspace_settings(self.config)


@unittest.skipUnless(_reflink_test_root() is not None, "no reflink-capable test filesystem mounted")
class RealReflinkCreateTests(unittest.TestCase):
    """Runs the real O_NOFOLLOW + FICLONE path on XFS/btrfs when available."""

    def setUp(self):
        self.reflink_root = _reflink_test_root()
        self.temp = tempfile.TemporaryDirectory(dir=self.reflink_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.root / "config"
        self.config.mkdir()
        (self.config / "config.toml").write_text(
            _config_with_workspace("reflink", ["node_modules"])
        )
        git("init", "-q", self.source)
        git("-C", self.source, "config", "user.name", "Test")
        git("-C", self.source, "config", "user.email", "test@example.invalid")
        (self.source / "value.txt").write_text("tracked\n")
        (self.source / ".gitignore").write_text(".env\nnode_modules/\n")
        git("-C", self.source, "add", "value.txt", ".gitignore")
        git("-C", self.source, "commit", "-qm", "first")
        mods = self.source / "node_modules" / "pkg"
        mods.mkdir(parents=True)
        (mods / "index.js").write_text("deps\n")
        (self.source / ".env").write_text("SECRET=do-not-copy\n")
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)

    def test_real_reflink_worker_excludes_env_keeps_allowlist(self):
        self.assertTrue(plugin.probe_reflink(self.source, self.state))
        worker = plugin.create_worker("REAL-REFLINK-001", str(self.source))
        clone = worker / "repo"
        self.assertEqual((clone / "value.txt").read_text(), "tracked\n")
        self.assertEqual((clone / "node_modules" / "pkg" / "index.js").read_text(), "deps\n")
        self.assertFalse((clone / ".env").exists())
        self.assertFalse((clone / ".git" / "objects" / "info" / "alternates").exists())
        src_file = self.source / "node_modules" / "pkg" / "index.js"
        dst_file = clone / "node_modules" / "pkg" / "index.js"
        self.assertNotEqual(src_file.stat().st_ino, dst_file.stat().st_ino)
        dst_file.write_text("mutated-in-worker\n")
        self.assertEqual(src_file.read_text(), "deps\n")
        status = git("-C", clone, "status", "--porcelain")
        self.assertEqual(status, "")
        metadata = json.loads((worker / "worker.json").read_text())
        self.assertEqual(metadata["workspace_strategy"], "reflink")

    def test_real_reflink_skips_env_hardlink_inside_allowlist(self):
        leaked = self.source / "node_modules" / "pkg" / "leaked.env"
        os.link(self.source / ".env", leaked)
        stderr = io.StringIO()
        with patch.object(plugin.sys, "stderr", stderr):
            worker = plugin.create_worker("REAL-HARDLINK-001", str(self.source))
        self.assertFalse((worker / "repo" / "node_modules" / "pkg" / "leaked.env").exists())
        self.assertIn("skipping hardlink", stderr.getvalue())


class ProbeCrossFilesystemTests(unittest.TestCase):
    def test_probe_fails_across_filesystems_when_possible(self):
        reflink_root = _reflink_test_root()
        if reflink_root is None:
            self.skipTest("no reflink-capable filesystem for cross-device contrast")
        with tempfile.TemporaryDirectory(dir=reflink_root) as src_dir, \
                tempfile.TemporaryDirectory() as state_dir:
            source = Path(src_dir)
            state = Path(state_dir)
            (source / "marker").write_text("x\n")
            self.assertFalse(plugin.probe_reflink(source, state))


if __name__ == "__main__":
    unittest.main()
