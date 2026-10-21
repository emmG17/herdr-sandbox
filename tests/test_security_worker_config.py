#!/usr/bin/env python3
"""Security regression tests for malicious worker .git/config.

Verifies that host-side git operations never execute commands from worker config
like core.fsmonitor, core.hooksPath, diff textconv, core.sshCommand, etc.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
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

        with patch.object(plugin, "_query_worker_git_snapshot") as query:
            workers = plugin.list_workers(self.state)
            query.assert_not_called()

        self.assertTrue(any(w["id"] == worker_id for w in workers))
        self.assert_no_markers()

    def test_inspect_does_not_execute_worker_config(self):
        """Test that 'inspect' command never executes worker config."""
        worker_id, _ = self.create_malicious_worker()

        with patch.object(plugin, "container_status", return_value="not-found"), \
                patch.object(plugin, "_query_worker_git_snapshot", return_value={
                    "status_porcelain": "", "log": "", "status_short": "", "diff": "", "error": None,
                }):
            inspection = plugin.inspect_worker(worker_id, self.state)

        self.assertEqual(inspection["id"], worker_id)
        self.assert_no_markers()

    def test_inspect_cli_from_worker_cwd_does_not_execute_worker_config(self):
        """chdir into a malicious worker clone must not run host git on it."""
        worker_id, worker_dir = self.create_malicious_worker()
        worker_repo = worker_dir / "repo"
        stderr = io.StringIO()
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1"}), \
                contextlib.chdir(worker_repo), \
                patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect"]), \
                contextlib.redirect_stderr(stderr), \
                patch.object(plugin, "container_status", return_value="not-found"), \
                patch.object(plugin, "_query_worker_git_snapshot") as query:
            self.assertEqual(plugin.main(), 1)
            query.assert_not_called()
        message = stderr.getvalue()
        self.assertIn("not inside a source Git repository", message)
        self.assertIn(worker_id, message)
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

    def test_plain_git_merge_after_fetch_does_not_execute_worker_config(self):
        """Fetch then merge in the source repo must never read worker .git/config."""
        worker_id, worker_dir = self.create_malicious_worker(make_worker_change=True)
        worker_repo = worker_dir / "repo"
        worker_ref_file = worker_repo / ".git" / "refs" / "heads" / f"agent/{worker_id}"
        worker_commit = worker_ref_file.read_text().strip()

        result = plugin.fetch_worker(worker_id, self.state)
        self.assertEqual(result["commit"], worker_commit)
        self.assert_no_markers()

        git("-C", self.source, "merge", "--no-edit", f"sandbox/{worker_id}")
        self.assert_no_markers()
        self.assertEqual(
            git("-C", self.source, "rev-parse", "HEAD"),
            worker_commit,
        )

    def test_execute_post_run_does_not_execute_worker_config(self):
        """Post-run accounting must use source-repo refs, never worker git config."""
        worker_id, worker_dir = self.create_malicious_worker(make_worker_change=True)
        worker_head = plugin._safe_read_worker_ref(
            worker_dir / "repo", f"agent/{worker_id}"
        )

        with patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin, "execute_command", return_value=[
                    sys.executable, "-c", "print('ok', flush=True)",
                ]), \
                patch.object(plugin, "_query_worker_git_snapshot", return_value={
                    "status_porcelain": "", "log": "", "status_short": "", "diff": "", "error": None,
                }), \
                patch.object(plugin.sys, "stdout", new_callable=io.StringIO), \
                patch.object(plugin.sys, "stderr", new_callable=io.StringIO):
            result = plugin.execute_worker(worker_id, "noop")

        self.assertEqual(result["exit_status"], 0)
        self.assertEqual(result["head"], worker_head)
        self.assertEqual(result["result_branch"], f"sandbox/{worker_id}")
        self.assertEqual(result["commit_count"], 2)
        self.assertEqual(
            git("-C", self.source, "rev-parse", f"refs/heads/sandbox/{worker_id}"),
            worker_head,
        )
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


class TestGitQueryCommandHardening(unittest.TestCase):
    """Query containers must not collide with sessions or expose Codex/network."""

    def test_git_query_command_is_isolated(self):
        runtime = plugin.PodmanSandboxRuntime(
            worker_id="TEST-001",
            image="localhost/herdr-codex-worker:latest",
            repo=Path("/tmp/worker-repo"),
            codex_home=Path("/tmp/codex-home"),
            network="online",
            cpus=4,
            memory="8g",
            pids=1024,
        )
        command = plugin.build_worker_git_query_command(runtime, base_commit="a" * 40)
        self.assertNotIn("--name", command)
        self.assertNotIn("herdr-TEST-001", command)
        self.assertIn("--network=none", command)
        self.assertTrue(any(part.endswith(",ro") and "/workspace" in part for part in command if isinstance(part, str)))
        self.assertFalse(any("/codex" in part for part in command if isinstance(part, str)))
        self.assertNotIn("CODEX_HOME=/codex", command)
        joined = " ".join(command)
        self.assertIn("type=bind,src=/tmp/worker-repo,dst=/workspace,ro", joined)


class TestObjectImportFiltering(unittest.TestCase):
    """Only loose objects and pack payloads may enter the trusted temp repo."""

    def test_auxiliary_object_metadata_is_not_copied(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker_objects = root / "worker-objects"
            dest = root / "dest-objects"
            (worker_objects / "ab").mkdir(parents=True)
            loose = worker_objects / "ab" / ("c" * 38)
            loose.write_bytes(b"fake-loose")
            pack_dir = worker_objects / "pack"
            pack_dir.mkdir()
            pack = pack_dir / ("pack-" + ("d" * 40) + ".pack")
            pack.write_bytes(b"PACK")
            (pack_dir / ("pack-" + ("d" * 40) + ".idx")).write_bytes(b"IDX")
            (pack_dir / ("pack-" + ("d" * 40) + ".bitmap")).write_bytes(b"BITMAP")
            (pack_dir / "multi-pack-index").write_bytes(b"MIDX")
            info = worker_objects / "info"
            info.mkdir()
            (info / "commit-graph").write_bytes(b"GRAPH")
            (info / "alternates").write_text("/evil\n")
            graphs = info / "commit-graphs"
            graphs.mkdir()
            (graphs / "graph-1").write_bytes(b"GRAPHDIR")

            copied_packs = plugin._copy_worker_objects(worker_objects, dest)
            self.assertEqual([p.name for p in copied_packs], [pack.name])
            self.assertTrue((dest / "ab" / ("c" * 38)).is_file())
            self.assertTrue((dest / "pack" / pack.name).is_file())
            self.assertFalse((dest / "pack" / (pack.stem + ".idx")).exists())
            self.assertFalse((dest / "pack" / (pack.stem + ".bitmap")).exists())
            self.assertFalse((dest / "pack" / "multi-pack-index").exists())
            self.assertFalse((dest / "info").exists())


class TestCorruptObjectFetch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
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
        self.source = root / "source"
        self.source.mkdir()
        git("-C", self.source, "init", "-q")
        git("-C", self.source, "config", "user.name", "Test")
        git("-C", self.source, "config", "user.email", "test@example.invalid")
        (self.source / "file.txt").write_text("initial\n")
        git("-C", self.source, "add", "file.txt")
        git("-C", self.source, "commit", "-qm", "initial")
        self.source_head = git("-C", self.source, "rev-parse", "HEAD")

    def test_corrupt_loose_object_fails_fetch_without_mutating_source(self):
        with patch.object(plugin, "check", return_value=[]):
            worker = plugin.create_worker("CORRUPT-001", str(self.source))
        repo = worker / "repo"
        git("-C", repo, "config", "user.name", "Worker")
        git("-C", repo, "config", "user.email", "worker@example.invalid")
        (repo / "change.txt").write_text("change\n")
        git("-C", repo, "add", "change.txt")
        git("-C", repo, "commit", "-qm", "worker change")
        tip = git("-C", repo, "rev-parse", "HEAD")
        # Overwrite the tip commit object with garbage while keeping the path.
        tip_path = repo / ".git" / "objects" / tip[:2] / tip[2:]
        tip_path.chmod(0o644)
        tip_path.write_bytes(b"not-a-valid-git-object")

        with self.assertRaises(ValueError):
            plugin.fetch_worker("CORRUPT-001", self.state)

        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)
        self.assertFalse((self.source / ".git" / "refs" / "heads" / "sandbox/CORRUPT-001").exists())
        self.assertFalse((self.source / ".git" / "objects" / "info" / "alternates").exists())


class TestGitCommandAudit(unittest.TestCase):
    """Grep-style test to enforce no vulnerable git -C <worker-repo> calls."""

    def test_no_git_c_worker_repo_in_main_script(self):
        """Verify host worker inspection uses safe helpers and one query container."""
        script = Path(__file__).parent.parent / "bin" / "herdr-sandbox"
        content = script.read_text()

        self.assertNotIn("def _git_read(", content)
        self.assertIn("def _safe_read_worker_head(", content)
        self.assertIn("def _safe_read_worker_branch(", content)
        self.assertIn("def _safe_read_worker_ref(", content)
        self.assertIn("def _safe_fetch_from_worker(", content)
        self.assertIn("def validate_sandbox_result_branch(", content)
        self.assertIn("def _sandbox_branch_checkout_path(", content)
        self.assertIn("def build_worker_git_query_command(", content)
        self.assertIn("def _query_worker_git_snapshot(", content)
        self.assertIn('["git", "bundle", "verify"', content)
        self.assertIn("fetch.fsckObjects=true", content)
        self.assertIn("index-pack", content)
        self.assertIn('"--no-write-fetch-head"', content)
        self.assertIn('"--no-recurse-submodules"', content)
        self.assertIn("+{worker_branch}:refs/heads/{target_branch}", content)
        # Flags must not appear as argv tokens (comments may mention them).
        self.assertNotRegex(content, r'["\']--update-head-ok["\']')
        self.assertNotRegex(content, r'["\']--prune["\']')
        self.assertNotIn('alternates_file = source_repo', content)
        self.assertNotIn('"repack"', content)
        self.assertNotIn("'repack'", content)

        git_reality_start = content.find("def _git_reality(")
        git_reality_end = content.find("\ndef ", git_reality_start + 1)
        git_reality_body = content[git_reality_start:git_reality_end]
        self.assertIn("_safe_read_worker_head", git_reality_body)
        self.assertIn("_safe_read_worker_branch", git_reality_body)
        self.assertIn("_query_worker_git_snapshot", git_reality_body)
        self.assertEqual(git_reality_body.count("_query_worker_git_snapshot"), 1)


if __name__ == "__main__":
    unittest.main()
