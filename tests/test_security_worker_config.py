#!/usr/bin/env python3
"""Security regression tests for malicious worker .git/config.

Verifies that host-side git operations never execute commands from worker config
like core.fsmonitor, core.hooksPath, diff textconv, core.sshCommand, etc.
"""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add parent directory to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))

from tests.helpers import git


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_security", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class TestMaliciousWorkerConfig(unittest.TestCase):
    """Test that malicious worker .git/config cannot execute code on the host."""

    def setUp(self):
        # Create marker directory for detecting code execution
        self.marker_dir = Path(tempfile.mkdtemp(prefix="herdr-security-test-"))
        self.fsmonitor_marker = self.marker_dir / "fsmonitor-executed"
        self.hook_marker = self.marker_dir / "hook-executed"
        self.textconv_marker = self.marker_dir / "textconv-executed"
        self.ssh_marker = self.marker_dir / "ssh-executed"

        # Set up plugin directories
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = root / "config"
        self.config.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        (self.config / "codex").mkdir()
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
        
        # Set environment
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

        # Create source repository
        self.source = root / "source"
        self.source.mkdir()
        git("-C", self.source, "init", "-q")
        git("-C", self.source, "config", "user.name", "Test")
        git("-C", self.source, "config", "user.email", "test@example.invalid")
        (self.source / "file.txt").write_text("initial content\n")
        git("-C", self.source, "add", "file.txt")
        git("-C", self.source, "commit", "-qm", "initial")
        self.source_head = git("-C", self.source, "rev-parse", "HEAD")

    def tearDown(self):
        import shutil
        if self.marker_dir.exists():
            shutil.rmtree(self.marker_dir, ignore_errors=True)
        self.env.stop()

    def create_malicious_worker(self, make_worker_change=False):
        """Create a worker and inject malicious .git/config settings.
        
        Args:
            make_worker_change: If True, makes an additional commit in worker repo
                               before injecting malicious config
        """
        # Create worker
        with patch.object(plugin, "check", return_value=[]):
            worker_dir = plugin.create_worker("MALICIOUS-001", str(self.source))
        worker_repo = worker_dir / "repo"
        
        # Create .gitattributes to trigger textconv (but don't commit yet)
        (worker_repo / ".gitattributes").write_text("*.txt diff=evil\n")
        
        # Make commits BEFORE adding malicious config
        # Use environment variables to prevent config execution during setup
        safe_env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        subprocess.run(
            ["git", "-C", str(worker_repo), "add", ".gitattributes"],
            env=safe_env, check=True
        )
        subprocess.run(
            ["git", "-C", str(worker_repo), "config", "user.name", "Worker"],
            env=safe_env, check=True
        )
        subprocess.run(
            ["git", "-C", str(worker_repo), "config", "user.email", "worker@example.invalid"],
            env=safe_env, check=True
        )
        subprocess.run(
            ["git", "-C", str(worker_repo), "commit", "-qm", "Add gitattributes"],
            env=safe_env, check=True
        )
        
        # If requested, make additional worker change BEFORE adding malicious config
        if make_worker_change:
            (worker_repo / "worker-change.txt").write_text("worker content\n")
            subprocess.run(
                ["git", "-C", str(worker_repo), "add", "worker-change.txt"],
                env=safe_env, check=True
            )
            subprocess.run(
                ["git", "-C", str(worker_repo), "commit", "-qm", "worker change"],
                env=safe_env, check=True
            )
        
        # NOW inject malicious config (after all commits are done)
        # From this point on, we MUST NOT run any git commands in worker repo
        malicious_config = f"""
[core]
    fsmonitor = "touch {self.fsmonitor_marker} && echo"
    hooksPath = {self.marker_dir / "hooks"}
    sshCommand = "touch {self.ssh_marker} && ssh"
    pager = "touch {self.marker_dir / 'pager-executed'} && cat"

[diff "evil"]
    textconv = "touch {self.textconv_marker} && cat"
"""
        config_file = worker_repo / ".git" / "config"
        with config_file.open("a") as f:
            f.write(malicious_config)
        
        # Create a hooks directory with an executable hook
        hooks_dir = self.marker_dir / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        post_checkout_hook = hooks_dir / "post-checkout"
        post_checkout_hook.write_text(
            f"#!/bin/sh\ntouch {self.hook_marker}\n"
        )
        post_checkout_hook.chmod(0o755)
        
        return "MALICIOUS-001", worker_dir

    def assert_no_markers(self):
        """Assert that no marker files were created (no code execution)."""
        markers = [
            ("core.fsmonitor", self.fsmonitor_marker),
            ("hooks", self.hook_marker),
            ("textconv", self.textconv_marker),
            ("ssh", self.ssh_marker),
        ]
        for name, marker in markers:
            self.assertFalse(
                marker.exists(),
                f"Security breach: {name} was executed on the host! Marker exists: {marker}"
            )

    def test_list_does_not_execute_worker_config(self):
        """Test that 'list' command never executes worker config."""
        worker_id, _ = self.create_malicious_worker()

        with patch.object(plugin, "_query_worker_git_in_container", return_value=(0, "", "")):
            workers = plugin.list_workers(self.state)

        self.assertTrue(any(w["id"] == worker_id for w in workers))
        self.assert_no_markers()

    def test_inspect_does_not_execute_worker_config(self):
        """Test that 'inspect' command never executes worker config."""
        worker_id, _ = self.create_malicious_worker()

        with patch.object(plugin, "container_status", return_value="not-found"), \
                patch.object(plugin, "_query_worker_git_in_container", return_value=(0, "", "")):
            inspection = plugin.inspect_worker(worker_id, self.state)

        self.assertEqual(inspection["id"], worker_id)
        self.assert_no_markers()

    def test_fetch_does_not_execute_worker_config(self):
        """Test that 'fetch' command never executes worker config."""
        # Create worker with a commit already made (before malicious config is added)
        worker_id, worker_dir = self.create_malicious_worker(make_worker_change=True)
        
        # Check no markers exist after creation
        self.assert_no_markers()
        
        result = plugin.fetch_worker(worker_id, self.state)
        
        self.assertIsNotNone(result["commit"])
        self.assert_no_markers()
        
        # Verify fetch succeeded (sandbox branch exists in source)
        sandbox_branch = f"sandbox/{worker_id}"
        git("-C", self.source, "rev-parse", "--verify", f"refs/heads/{sandbox_branch}")

    def test_cherry_pick_does_not_execute_worker_config(self):
        """Test that 'cherry-pick' command never executes worker config."""
        # Create worker with a commit already made (before malicious config is added)
        worker_id, worker_dir = self.create_malicious_worker(make_worker_change=True)
        
        # Read the commit SHA from the ref file (not using git command in worker repo)
        worker_repo = worker_dir / "repo"
        worker_ref_file = worker_repo / ".git" / "refs" / "heads" / f"agent/{worker_id}"
        worker_commit = worker_ref_file.read_text().strip()
        
        # Fetch first
        plugin.fetch_worker(worker_id, self.state)
        self.assert_no_markers()
        
        # Cherry-pick
        result = plugin.cherry_pick_worker(worker_id, worker_commit, self.state)
        self.assertIsNotNone(result["integrated_commit"])
        self.assert_no_markers()

    def test_execute_post_run_does_not_execute_worker_config(self):
        """Test that post-run HEAD reading never executes worker config."""
        worker_id, worker_dir = self.create_malicious_worker()

        with patch.object(plugin, "container_status", return_value="not-found"), \
                patch.object(plugin, "_query_worker_git_in_container", return_value=(0, "", "")):
            inspection = plugin.inspect_worker(worker_id, self.state)

        self.assertEqual(inspection["id"], worker_id)
        self.assertIsNotNone(inspection["head"])
        self.assert_no_markers()

    def test_packed_refs_are_read_without_host_git(self):
        """Packed worker refs must resolve without running git in the worker."""
        worker_id, worker_dir = self.create_malicious_worker(make_worker_change=True)
        worker_repo = worker_dir / "repo"
        branch = f"agent/{worker_id}"
        loose = worker_repo / ".git" / "refs" / "heads" / branch
        sha = loose.read_text().strip()
        packed = worker_repo / ".git" / "packed-refs"
        packed.write_text(f"# pack-refs with: peeled fully-peeled sorted\n{sha} refs/heads/{branch}\n")
        loose.unlink()

        self.assertEqual(plugin._safe_read_worker_ref(worker_repo, branch), sha)
        success, head, error = plugin._safe_read_worker_head(worker_repo)
        self.assertTrue(success, error)
        self.assertEqual(head, sha)

        result = plugin.fetch_worker(worker_id, self.state)
        self.assertEqual(result["commit"], sha)
        self.assert_no_markers()

    def test_symlinked_refs_and_objects_are_rejected(self):
        """Symlinks under worker .git must not be followed."""
        worker_id, worker_dir = self.create_malicious_worker(make_worker_change=True)
        worker_repo = worker_dir / "repo"
        branch = f"agent/{worker_id}"
        loose = worker_repo / ".git" / "refs" / "heads" / branch
        real_sha = loose.read_text().strip()
        target = self.marker_dir / "evil-ref"
        target.write_text("0" * 40 + "\n")
        loose.unlink()
        loose.symlink_to(target)

        with self.assertRaises(ValueError):
            plugin._safe_read_worker_ref(worker_repo, branch)

        # Restore a real ref, then poison objects with a symlink.
        loose.unlink()
        loose.write_text(real_sha + "\n")
        objects = worker_repo / ".git" / "objects"
        evil_objects = self.marker_dir / "evil-objects"
        evil_objects.mkdir()
        objects.rename(worker_repo / ".git" / "objects-real")
        objects.symlink_to(evil_objects)
        with self.assertRaises(ValueError):
            plugin.fetch_worker(worker_id, self.state)
        self.assertFalse((self.source / ".git" / "refs" / "heads" / f"sandbox/{worker_id}").exists())
        self.assertFalse((self.source / ".git" / "objects" / "info" / "alternates").exists())
        self.assert_no_markers()

    def test_fetch_failure_leaves_source_unchanged(self):
        """A failed fetch must not leave alternates or a dangling sandbox ref."""
        worker_id, worker_dir = self.create_malicious_worker(make_worker_change=True)
        alternates = self.source / ".git" / "objects" / "info" / "alternates"
        sandbox_ref = self.source / ".git" / "refs" / "heads" / f"sandbox/{worker_id}"
        head_before = git("-C", self.source, "rev-parse", "HEAD")

        real_run = subprocess.run

        def fail_bundle_create(command, **kwargs):
            command = list(command)
            if command and command[0] == "git" and "bundle" in command and "create" in command:
                return subprocess.CompletedProcess(command, 1, "", "simulated bundle create failure")
            return real_run(command, **kwargs)

        with patch.object(plugin.subprocess, "run", side_effect=fail_bundle_create):
            with self.assertRaises(ValueError):
                plugin.fetch_worker(worker_id, self.state)

        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), head_before)
        self.assertFalse(sandbox_ref.exists())
        self.assertFalse(alternates.exists())
        self.assert_no_markers()

    def test_concurrent_fetches_do_not_corrupt_source(self):
        """Two fetches must not race on shared source-repo metadata."""
        import threading

        first_id, _ = self.create_malicious_worker(make_worker_change=True)
        with patch.object(plugin, "check", return_value=[]):
            second_dir = plugin.create_worker("MALICIOUS-002", str(self.source))
        second_repo = second_dir / "repo"
        safe_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        (second_repo / "second.txt").write_text("second\n")
        subprocess.run(["git", "-C", str(second_repo), "config", "user.name", "Worker"], env=safe_env, check=True)
        subprocess.run(["git", "-C", str(second_repo), "config", "user.email", "worker@example.invalid"], env=safe_env, check=True)
        subprocess.run(["git", "-C", str(second_repo), "add", "second.txt"], env=safe_env, check=True)
        subprocess.run(["git", "-C", str(second_repo), "commit", "-qm", "second change"], env=safe_env, check=True)
        config_file = second_repo / ".git" / "config"
        with config_file.open("a") as stream:
            stream.write(
                f"\n[core]\n    fsmonitor = \"touch {self.fsmonitor_marker} && echo\"\n"
                f"    hooksPath = {self.marker_dir / 'hooks'}\n"
            )

        errors = []

        def fetch_one(worker_id):
            try:
                plugin.fetch_worker(worker_id, self.state)
            except Exception as exc:  # noqa: BLE001 - collect for assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=fetch_one, args=(first_id,)),
            threading.Thread(target=fetch_one, args=("MALICIOUS-002",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        git("-C", self.source, "rev-parse", "--verify", f"refs/heads/sandbox/{first_id}")
        git("-C", self.source, "rev-parse", "--verify", "refs/heads/sandbox/MALICIOUS-002")
        self.assertFalse((self.source / ".git" / "objects" / "info" / "alternates").exists())
        self.assert_no_markers()


class TestGitCommandAudit(unittest.TestCase):
    """Grep-style test to enforce no vulnerable git -C <worker-repo> calls."""

    def test_no_git_c_worker_repo_in_main_script(self):
        """Verify host worker inspection uses safe helpers, not _git_read."""
        script = Path(__file__).parent.parent / "bin" / "herdr-sandbox"
        content = script.read_text()

        self.assertIn("DEPRECATED for worker repos", content)
        self.assertIn("def _safe_read_worker_head(", content)
        self.assertIn("def _safe_read_worker_branch(", content)
        self.assertIn("def _safe_read_worker_ref(", content)
        self.assertIn("def _safe_fetch_from_worker(", content)
        self.assertIn('["git", "bundle", "verify"', content)
        # Safe fetch must not mutate the source repo via alternates/repack.
        self.assertNotIn('alternates_file = source_repo', content)
        self.assertNotIn('"repack"', content)
        self.assertNotIn("'repack'", content)

        git_reality_start = content.find("def _git_reality(")
        git_reality_end = content.find("\ndef ", git_reality_start + 1)
        git_reality_body = content[git_reality_start:git_reality_end]
        self.assertIn("_safe_read_worker_head", git_reality_body)
        self.assertIn("_safe_read_worker_branch", git_reality_body)
        self.assertIn("_query_worker_git_in_container", git_reality_body)
        self.assertNotIn("_git_read(repo,", git_reality_body)
        self.assertNotIn("_git_read(worker_repo,", git_reality_body)


if __name__ == "__main__":
    unittest.main()
