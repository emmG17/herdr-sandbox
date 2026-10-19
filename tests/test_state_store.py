import fcntl
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.helpers import git


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_state", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class WorkerIDGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.root / "config"
        self.config.mkdir()
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
        
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_generated_ids_match_worker_id_regex(self):
        """Generated IDs must match WORKER_ID_RE."""
        repo = self.root / "test-repo"
        repo.mkdir()
        
        for _ in range(10):
            worker_id = plugin.generate_worker_id(repo, self.state)
            self.assertIsNotNone(plugin.WORKER_ID_RE.fullmatch(worker_id))
            self.assertNotIn("..", worker_id)

    def test_generated_ids_are_valid_git_refs(self):
        """Generated IDs must be valid under agent/ and sandbox/ namespaces."""
        repo = self.root / "test-repo"
        repo.mkdir()
        
        for _ in range(10):
            worker_id = plugin.generate_worker_id(repo, self.state)
            
            result = subprocess.run(
                ["git", "check-ref-format", "--branch", f"agent/{worker_id}"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, f"agent/{worker_id} is not a valid ref")
            
            result = subprocess.run(
                ["git", "check-ref-format", "--branch", f"sandbox/{worker_id}"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, f"sandbox/{worker_id} is not a valid ref")

    def test_generated_ids_do_not_collide(self):
        """1000 generated IDs should all be unique."""
        repo = self.root / "test-repo"
        repo.mkdir()
        
        ids = set()
        workers_dir = self.state / "workers"
        workers_dir.mkdir()
        
        for _ in range(1000):
            worker_id = plugin.generate_worker_id(repo, self.state)
            self.assertNotIn(worker_id, ids, f"Collision detected: {worker_id}")
            ids.add(worker_id)
            (workers_dir / worker_id).mkdir()

    def test_id_format_is_repo_slug_plus_random(self):
        """IDs should follow the pattern <repo-slug>-<short-random>."""
        repo = self.root / "my-test-repo"
        repo.mkdir()
        
        worker_id = plugin.generate_worker_id(repo, self.state)
        
        self.assertTrue(worker_id.startswith("my-test-repo-"))
        suffix = worker_id.split("-", 3)[-1]
        self.assertEqual(len(suffix), 6)
        self.assertTrue(all(c in "0123456789abcdef" for c in suffix))

    def test_sanitizes_repo_name_for_id(self):
        """Repo names with special characters should be sanitized."""
        repo = self.root / "My Repo! (test)"
        repo.mkdir()
        
        worker_id = plugin.generate_worker_id(repo, self.state)
        
        self.assertIsNotNone(plugin.WORKER_ID_RE.fullmatch(worker_id))
        self.assertTrue(worker_id.startswith("My-Repo-test-"))

    def test_respects_max_worker_id_length(self):
        """Generated IDs should not exceed MAX_WORKER_ID_LENGTH."""
        very_long_name = "a" * 100
        repo = self.root / very_long_name
        repo.mkdir()
        
        worker_id = plugin.generate_worker_id(repo, self.state)
        
        self.assertLessEqual(len(worker_id), plugin.MAX_WORKER_ID_LENGTH)


class ConcurrentWriteTests(unittest.TestCase):
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
        (self.source / "value.txt").write_text("test\n")
        git("-C", self.source, "add", "value.txt")
        git("-C", self.source, "commit", "-qm", "test")
        
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)

    def test_concurrent_worker_creation_produces_distinct_ids(self):
        """Two workers created concurrently should get distinct IDs."""
        results = []
        errors = []
        
        def create_worker():
            try:
                worker_dir = plugin.create_worker(None, str(self.source), "HEAD")
                results.append(worker_dir.name)
            except Exception as e:
                errors.append(str(e))
        
        threads = [threading.Thread(target=create_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        self.assertEqual(len(errors), 0, f"Errors during concurrent creation: {errors}")
        self.assertEqual(len(results), 2)
        self.assertNotEqual(results[0], results[1])

    def test_concurrent_metadata_updates_leave_valid_json(self):
        """Concurrent metadata updates should not corrupt worker.json."""
        worker = plugin.create_worker(None, str(self.source), "HEAD")
        
        def update_status(status):
            try:
                plugin._write_worker_status(worker, status)
            except Exception:
                pass
        
        threads = [
            threading.Thread(target=update_status, args=("running",)),
            threading.Thread(target=update_status, args=("stopped",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        metadata_path = worker / "worker.json"
        with metadata_path.open() as f:
            metadata = json.load(f)
        
        self.assertIsInstance(metadata, dict)
        self.assertIn(metadata["status"], ["running", "stopped"])

    def test_state_lock_prevents_concurrent_access(self):
        """StateLock should prevent concurrent access to critical sections."""
        lock_holder = []
        
        def hold_lock_briefly():
            with plugin.StateLock(self.state):
                lock_holder.append(threading.current_thread().name)
                time.sleep(0.1)
                lock_holder.append(threading.current_thread().name)
        
        threads = [
            threading.Thread(target=hold_lock_briefly, name="T1"),
            threading.Thread(target=hold_lock_briefly, name="T2"),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        self.assertEqual(len(lock_holder), 4)
        self.assertEqual(lock_holder[0], lock_holder[1])
        self.assertEqual(lock_holder[2], lock_holder[3])
        self.assertNotEqual(lock_holder[0], lock_holder[2])


class WorkerReuseTests(unittest.TestCase):
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
        (self.source / "value.txt").write_text("test\n")
        git("-C", self.source, "add", "value.txt")
        git("-C", self.source, "commit", "-qm", "test")
        
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)

    def test_reuse_finds_worker_with_matching_pane_repo_agent(self):
        """find_reusable_worker should return a worker matching all three keys."""
        pane_id = "w1:p1"
        with patch.dict(os.environ, {"HERDR_PANE_ID": pane_id, "HERDR_WORKSPACE_ID": "w1"}):
            worker = plugin.create_worker(None, str(self.source), "HEAD", agent="codex")
        
        with patch.dict(os.environ, {"HERDR_PANE_ID": pane_id, "HERDR_WORKSPACE_ID": "w1"}):
            found = plugin.find_reusable_worker(str(self.source), "codex", self.state)
        
        self.assertEqual(found, worker.name)

    def test_reuse_returns_none_when_pane_id_differs(self):
        """Different pane ID should not match."""
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1", "HERDR_WORKSPACE_ID": "w1"}):
            plugin.create_worker(None, str(self.source), "HEAD", agent="codex")
        
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2", "HERDR_WORKSPACE_ID": "w1"}):
            found = plugin.find_reusable_worker(str(self.source), "codex", self.state)
        
        self.assertIsNone(found)

    def test_reuse_returns_none_when_repo_differs(self):
        """Different source repo should not match."""
        other_source = self.root / "other"
        other_source.mkdir()
        git("init", "-q", other_source)
        git("-C", other_source, "config", "user.name", "Test")
        git("-C", other_source, "config", "user.email", "test@example.invalid")
        (other_source / "value.txt").write_text("test\n")
        git("-C", other_source, "add", "value.txt")
        git("-C", other_source, "commit", "-qm", "test")
        
        pane_id = "w1:p1"
        with patch.dict(os.environ, {"HERDR_PANE_ID": pane_id, "HERDR_WORKSPACE_ID": "w1"}):
            plugin.create_worker(None, str(self.source), "HEAD", agent="codex")
        
        with patch.dict(os.environ, {"HERDR_PANE_ID": pane_id, "HERDR_WORKSPACE_ID": "w1"}):
            found = plugin.find_reusable_worker(str(other_source), "codex", self.state)
        
        self.assertIsNone(found)

    def test_reuse_returns_none_when_agent_differs(self):
        """Different agent should not match."""
        pane_id = "w1:p1"
        with patch.dict(os.environ, {"HERDR_PANE_ID": pane_id, "HERDR_WORKSPACE_ID": "w1"}):
            plugin.create_worker(None, str(self.source), "HEAD", agent="codex")
        
        with patch.dict(os.environ, {"HERDR_PANE_ID": pane_id, "HERDR_WORKSPACE_ID": "w1"}):
            found = plugin.find_reusable_worker(str(self.source), "claude", self.state)
        
        self.assertIsNone(found)

    def test_reuse_returns_none_when_pane_id_is_unset(self):
        """Missing HERDR_PANE_ID should always return None."""
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1", "HERDR_WORKSPACE_ID": "w1"}):
            plugin.create_worker(None, str(self.source), "HEAD", agent="codex")
        
        with patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}, clear=True):
            found = plugin.find_reusable_worker(str(self.source), "codex", self.state)
        
        self.assertIsNone(found)

    def test_reuse_returns_none_when_pane_id_is_empty_string(self):
        """Empty HERDR_PANE_ID should be treated as missing."""
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1", "HERDR_WORKSPACE_ID": "w1"}):
            plugin.create_worker(None, str(self.source), "HEAD", agent="codex")
        
        with patch.dict(os.environ, {"HERDR_PANE_ID": "", "HERDR_WORKSPACE_ID": "w1"}):
            found = plugin.find_reusable_worker(str(self.source), "codex", self.state)
        
        self.assertIsNone(found)


class WorkerMetadataTests(unittest.TestCase):
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
        (self.source / "value.txt").write_text("test\n")
        git("-C", self.source, "add", "value.txt")
        git("-C", self.source, "commit", "-qm", "test")
        
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_WORKSPACE_ID": "w1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)

    def test_worker_records_pane_and_workspace_ids(self):
        """Worker metadata should include pane_id and workspace_id."""
        worker = plugin.create_worker(None, str(self.source), "HEAD", agent="codex")
        
        with (worker / "worker.json").open() as f:
            metadata = json.load(f)
        
        self.assertEqual(metadata["pane_id"], "w1:p1")
        self.assertEqual(metadata["workspace_id"], "w1")

    def test_worker_records_agent(self):
        """Worker metadata should include the agent name."""
        worker = plugin.create_worker(None, str(self.source), "HEAD", agent="claude")
        
        with (worker / "worker.json").open() as f:
            metadata = json.load(f)
        
        self.assertEqual(metadata["agent"], "claude")

    def test_worker_records_null_pane_when_missing(self):
        """Workers created without HERDR_PANE_ID should record null."""
        env_without_pane = {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        }
        with patch.dict(os.environ, env_without_pane, clear=True):
            worker = plugin.create_worker(None, str(self.source), "HEAD", agent="codex")
        
        with (worker / "worker.json").open() as f:
            metadata = json.load(f)
        
        self.assertIsNone(metadata["pane_id"])
        self.assertIsNone(metadata["workspace_id"])

    def test_worker_metadata_includes_result_branch_and_fetched_commit(self):
        """Worker metadata should have result_branch and fetched_commit fields."""
        worker = plugin.create_worker(None, str(self.source), "HEAD", agent="codex")
        
        with (worker / "worker.json").open() as f:
            metadata = json.load(f)
        
        self.assertIn("result_branch", metadata)
        self.assertIn("fetched_commit", metadata)
        self.assertIsNone(metadata["result_branch"])
        self.assertIsNone(metadata["fetched_commit"])

    def test_fetched_is_valid_lifecycle_state(self):
        """The 'fetched' state should be recognized as valid."""
        self.assertIn("fetched", plugin.LIFECYCLE_STATES)


class HeadMovedBehaviorTests(unittest.TestCase):
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
        self.base_commit = git("-C", self.source, "rev-parse", "HEAD")
        
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)

    def test_fetch_notices_when_head_has_moved(self):
        """fetch_worker should detect when source HEAD has moved."""
        worker = plugin.create_worker(None, str(self.source), "HEAD")
        worker_id = worker.name
        
        (self.source / "value.txt").write_text("second\n")
        git("-C", self.source, "commit", "-qam", "second")
        new_head = git("-C", self.source, "rev-parse", "HEAD")
        
        result = plugin.fetch_worker(worker_id, self.state)
        
        self.assertTrue(result["head_moved"])
        self.assertEqual(result["base_commit"], self.base_commit)
        self.assertEqual(result["current_head"], new_head)
        self.assertIn("head_moved_notice", result)
        self.assertIn(self.base_commit[:12], result["head_moved_notice"])
        self.assertIn(new_head[:12], result["head_moved_notice"])

    def test_fetch_proceeds_without_rebase_when_head_moved(self):
        """fetch_worker should fetch as-is without auto-rebasing."""
        worker = plugin.create_worker(None, str(self.source), "HEAD")
        worker_id = worker.name
        
        clone = worker / "repo"
        git("-C", clone, "config", "user.name", "Worker")
        git("-C", clone, "config", "user.email", "worker@example.invalid")
        (clone / "worker.txt").write_text("worker change\n")
        git("-C", clone, "add", "worker.txt")
        git("-C", clone, "commit", "-qm", "worker change")
        worker_commit = git("-C", clone, "rev-parse", "HEAD")
        
        (self.source / "value.txt").write_text("second\n")
        git("-C", self.source, "commit", "-qam", "second")
        
        result = plugin.fetch_worker(worker_id, self.state)
        
        fetched_commit = git("-C", self.source, "rev-parse", f"refs/heads/{result['branch']}")
        self.assertEqual(fetched_commit, worker_commit)
        
        parent = git("-C", self.source, "rev-parse", f"refs/heads/{result['branch']}^")
        self.assertEqual(parent, self.base_commit)

    def test_fetch_marks_worker_as_fetched(self):
        """fetch_worker should update status to 'fetched'."""
        worker = plugin.create_worker(None, str(self.source), "HEAD")
        worker_id = worker.name
        
        plugin.fetch_worker(worker_id, self.state)
        
        with (worker / "worker.json").open() as f:
            metadata = json.load(f)
        
        self.assertEqual(metadata["status"], "fetched")
        self.assertIsNotNone(metadata["result_branch"])
        self.assertIsNotNone(metadata["fetched_commit"])


if __name__ == "__main__":
    unittest.main()
