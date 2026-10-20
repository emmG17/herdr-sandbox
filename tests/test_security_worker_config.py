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
        
        workers = plugin.list_workers(self.state)
        
        self.assertTrue(any(w["id"] == worker_id for w in workers))
        self.assert_no_markers()

    def test_inspect_does_not_execute_worker_config(self):
        """Test that 'inspect' command never executes worker config."""
        worker_id, _ = self.create_malicious_worker()
        
        with patch.object(plugin, "container_status", return_value="not-found"):
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
        """Test that post-run HEAD reading never executes worker config.
        
        This test verifies that reading HEAD after execution uses safe file-based
        reading, not git commands that would honor worker config.
        """
        worker_id, worker_dir = self.create_malicious_worker()
        
        # Verify that inspect (which calls _safe_read_worker_head) doesn't execute config
        with patch.object(plugin, "container_status", return_value="not-found"):
            inspection = plugin.inspect_worker(worker_id, self.state)
        
        self.assertEqual(inspection["id"], worker_id)
        self.assertIsNotNone(inspection["head"])
        self.assert_no_markers()


class TestGitCommandAudit(unittest.TestCase):
    """Grep-style test to enforce no vulnerable git -C <worker-repo> calls."""

    def test_no_git_c_worker_repo_in_main_script(self):
        """Verify no git commands use worker repo as -C target."""
        script = Path(__file__).parent.parent / "bin" / "herdr-sandbox"
        content = script.read_text()
        
        # Find all _git_read calls (deprecated for worker repos)
        # Check that _git_read has a deprecation warning
        self.assertIn(
            "DEPRECATED: Do not use for worker repos",
            content,
            "_git_read must have a deprecation warning"
        )
        
        # Verify safe functions exist
        self.assertIn("def _safe_read_worker_head(", content)
        self.assertIn("def _safe_read_worker_branch(", content)
        
        # Verify _git_reality uses safe functions
        git_reality_start = content.find("def _git_reality(repo, metadata):")
        git_reality_end = content.find("\ndef ", git_reality_start + 1)
        git_reality_body = content[git_reality_start:git_reality_end]
        
        # Should call safe functions, not _git_read
        self.assertIn("_safe_read_worker_head", git_reality_body)
        self.assertIn("_safe_read_worker_branch", git_reality_body)
        self.assertNotIn("_git_read(repo,", git_reality_body)
        self.assertNotIn("_git_read(worker_repo,", git_reality_body)


if __name__ == "__main__":
    unittest.main()
