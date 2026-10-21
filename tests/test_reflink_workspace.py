#!/usr/bin/env python3
"""Tests for experimental [workspace].strategy = reflink (#14)."""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
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

        for entry in ("/etc/passwd", "../secret", ".git", ".git/hooks"):
            with self.subTest(entry=entry):
                with self.assertRaises(ValueError):
                    plugin.validate_copy_untracked_entry(entry)


class ReflinkHelpersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "ok.txt").write_text("ok\n")

    def test_symlink_outside_detection(self):
        outside = self.root / "outside.txt"
        outside.write_text("nope\n")
        link = self.source / "escape"
        link.symlink_to(outside)
        self.assertTrue(plugin._symlink_points_outside(link, self.source))
        inside = self.source / "inside-link"
        inside.symlink_to("ok.txt")
        self.assertFalse(plugin._symlink_points_outside(inside, self.source))

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
        with patch.object(plugin, "reflink_file", side_effect=_copy2_as_reflink):
            plugin.copy_allowlisted_untracked(self.source, dest, ["node_modules"])

        self.assertTrue((dest / "node_modules" / "keep.js").is_file())
        self.assertTrue((dest / "node_modules" / "pkg" / "index.js").is_file())
        self.assertFalse((dest / "node_modules" / "leak").exists())
        self.assertFalse((dest / "node_modules" / ".git").exists())

    def test_top_level_escaping_symlink_allowlist_entry_skipped(self):
        outside = self.root / "outside-dir"
        outside.mkdir()
        (outside / "x").write_text("x\n")
        (self.source / "node_modules").symlink_to(outside)
        dest = self.root / "dest"
        dest.mkdir()
        with patch.object(plugin, "reflink_file", side_effect=_copy2_as_reflink):
            plugin.copy_allowlisted_untracked(self.source, dest, ["node_modules"])
        self.assertFalse((dest / "node_modules").exists())


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
        # Escaping symlink inside allowlisted tree
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
        notice = stderr.getvalue().strip().splitlines()
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
        self.assertNotEqual((clone / ".git").resolve(), (self.source / ".git").resolve())
        worker_hook = clone / ".git" / "hooks" / "pre-commit"
        self.assertTrue(
            (not worker_hook.exists())
            or worker_hook.read_text() != source_hook.read_text()
        )
        # Status: allowlisted ignored paths stay ignored
        status = git("-C", clone, "status", "--porcelain")
        self.assertEqual(status, "")
        self.assertEqual(git("-C", clone, "branch", "--show-current"), "agent/REFLINK-001")
        metadata = json.loads((worker / "worker.json").read_text())
        self.assertEqual(metadata["workspace_strategy"], "reflink")
        head = git("-C", self.source, "rev-parse", "HEAD")
        source_object = self.source / ".git" / "objects" / head[:2] / head[2:]
        clone_object = clone / ".git" / "objects" / head[:2] / head[2:]
        if source_object.is_file() and clone_object.is_file():
            self.assertNotEqual(source_object.stat().st_ino, clone_object.stat().st_ino)

    def test_rejects_absolute_and_dotdot_allowlist_at_config_parse(self):
        (self.config / "config.toml").write_text(
            _config_with_workspace("reflink", ["/tmp/x"])
        )
        with self.assertRaisesRegex(ValueError, "relative paths without"):
            plugin.workspace_settings(self.config)


@unittest.skipUnless(_reflink_test_root() is not None, "no reflink-capable test filesystem mounted")
class RealReflinkCreateTests(unittest.TestCase):
    """Runs the real FICLONE / cp --reflink=always path on XFS/btrfs when available."""

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
        # Distinct inodes for reflinked allowlisted file
        src_file = self.source / "node_modules" / "pkg" / "index.js"
        dst_file = clone / "node_modules" / "pkg" / "index.js"
        self.assertNotEqual(src_file.stat().st_ino, dst_file.stat().st_ino)
        # Write in worker must not affect source
        dst_file.write_text("mutated-in-worker\n")
        self.assertEqual(src_file.read_text(), "deps\n")
        status = git("-C", clone, "status", "--porcelain")
        self.assertEqual(status, "")
        metadata = json.loads((worker / "worker.json").read_text())
        self.assertEqual(metadata["workspace_strategy"], "reflink")


class ProbeCrossFilesystemTests(unittest.TestCase):
    def test_probe_fails_across_filesystems_when_possible(self):
        reflink_root = _reflink_test_root()
        if reflink_root is None:
            self.skipTest("no reflink-capable filesystem for cross-device contrast")
        # Source on XFS, state on default ext4 tmp — reflink must fail.
        with tempfile.TemporaryDirectory(dir=reflink_root) as src_dir, \
                tempfile.TemporaryDirectory() as state_dir:
            source = Path(src_dir)
            state = Path(state_dir)
            (source / "marker").write_text("x\n")
            self.assertFalse(plugin.probe_reflink(source, state))


if __name__ == "__main__":
    unittest.main()
