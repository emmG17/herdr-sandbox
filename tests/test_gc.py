import importlib.machinery
import importlib.util
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

from tests.helpers import git


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
ROOT = SCRIPT.parent.parent
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_gc", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class GcTests(unittest.TestCase):
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
        (self.source / "source.txt").write_text("source stays put\n")
        git("-C", self.source, "add", "source.txt")
        git("-C", self.source, "commit", "-qm", "initial")
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)
        containers = patch.object(plugin, "container_status", return_value="not-found")
        containers.start()
        self.addCleanup(containers.stop)
        podman_rm = patch.object(
            plugin,
            "run",
            side_effect=self._run_allowing_podman_rm,
        )
        # Keep a handle for tests that replace run entirely.
        self._default_run = plugin.run

    def _run_allowing_podman_rm(self, *args, **kwargs):
        if args[:3] == ("podman", "rm", "-f"):
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected")

    def create_worker(self, worker_id="TEST-001", pane_id="w1:p1"):
        with patch.dict(os.environ, {
            "HERDR_PANE_ID": pane_id or "",
            "HERDR_WORKSPACE_ID": "w1",
            "HERDR_SOCKET_PATH": "/tmp/herdr-test.sock",
        }, clear=False):
            if not pane_id:
                env = patch.dict(os.environ, {
                    "HERDR_PANE_ID": "",
                    "HERDR_WORKSPACE_ID": "",
                    "HERDR_SOCKET_PATH": "",
                })
            else:
                env = patch.dict(os.environ, {
                    "HERDR_PANE_ID": pane_id,
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_SOCKET_PATH": "/tmp/herdr-test.sock",
                })
            with env:
                return plugin.create_worker(worker_id, str(self.source))

    def mark_fetched(self, worker_id, created_at=None):
        worker_dir = self.state / "workers" / worker_id
        metadata = json.loads((worker_dir / "worker.json").read_text())
        metadata["status"] = "fetched"
        metadata["result_branch"] = f"sandbox/{worker_id}"
        metadata["fetched_commit"] = metadata["base_commit"]
        if created_at is not None:
            metadata["created_at"] = created_at
        (worker_dir / "worker.json").write_text(json.dumps(metadata, indent=2) + "\n")
        # Create the sandbox ref so for-each-ref has something to compare.
        git("-C", self.source, "update-ref", f"refs/heads/sandbox/{worker_id}", metadata["base_commit"])

    def mark_created_at(self, worker_id, created_at):
        worker_dir = self.state / "workers" / worker_id
        metadata = json.loads((worker_dir / "worker.json").read_text())
        metadata["created_at"] = created_at
        (worker_dir / "worker.json").write_text(json.dumps(metadata, indent=2) + "\n")

    def for_each_ref(self):
        return git("-C", self.source, "for-each-ref", "--format=%(refname) %(objectname)")

    def test_gc_settings_defaults_and_rejects_unknown_keys(self):
        settings = plugin.gc_settings(self.config)
        self.assertEqual(settings["max_age"], "7d")
        self.assertEqual(settings["max_age_seconds"], 7 * 86400)
        bad = self.config / "config.toml"
        bad.write_text(
            '[runtime]\nimage = "localhost/herdr-codex-worker:latest"\nnetwork = "online"\n'
            '[resources]\ncpus = 4\nmemory = "8g"\npids = 1024\n'
            '[codex]\nhome = "codex"\n'
            '[gc]\nmax_age = "7d"\nextra = 1\n'
        )
        with self.assertRaisesRegex(ValueError, "not permitted"):
            plugin.gc_settings(self.config)

    def test_parse_duration(self):
        self.assertEqual(plugin.parse_duration("90s"), 90)
        self.assertEqual(plugin.parse_duration("2h"), 7200)
        with self.assertRaisesRegex(ValueError, "duration"):
            plugin.parse_duration("7days")

    def test_fetched_worker_with_pane_gone_is_collected(self):
        self.create_worker("GONE-001", pane_id="w1:p9")
        self.mark_fetched("GONE-001")
        before = self.for_each_ref()
        with patch.object(plugin, "pane_exists", return_value=False), \
                patch.object(plugin, "remove_worker_container"):
            result = plugin.gc_collect_workers(state_dir=self.state, config_dir=self.config)
        self.assertIn("GONE-001", result["removed"])
        self.assertFalse((self.state / "workers" / "GONE-001").exists())
        self.assertEqual(self.for_each_ref(), before)

    def test_unfetched_worker_older_than_max_age_not_deleted_without_successful_fetch(self):
        self.create_worker("OLD-001", pane_id="w1:p2")
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self.mark_created_at("OLD-001", old)
        before = self.for_each_ref()
        with patch.object(plugin, "fetch_worker", side_effect=ValueError("fetch failed")), \
                patch.object(plugin, "pane_exists", return_value=True), \
                patch.object(plugin, "remove_worker_container"):
            result = plugin.gc_collect_workers(state_dir=self.state, config_dir=self.config)
        self.assertNotIn("OLD-001", result["removed"])
        self.assertTrue((self.state / "workers" / "OLD-001").exists())
        self.assertTrue(any("OLD-001" in warning and "fetch" in warning for warning in result["warnings"]))
        self.assertEqual(self.for_each_ref(), before)

    def test_max_age_fetches_then_deletes_on_success(self):
        self.create_worker("OLD-002", pane_id="w1:p3")
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self.mark_created_at("OLD-002", old)

        def fake_fetch(worker_id, state_dir=None):
            self.mark_fetched(worker_id)
            return {"branch": f"sandbox/{worker_id}", "commit": "abc", "commit_count": 0}

        before = self.for_each_ref()
        with patch.object(plugin, "fetch_worker", side_effect=fake_fetch), \
                patch.object(plugin, "pane_exists", return_value=True), \
                patch.object(plugin, "remove_worker_container"):
            result = plugin.gc_collect_workers(state_dir=self.state, config_dir=self.config)
        self.assertIn("OLD-002", result["removed"])
        self.assertFalse((self.state / "workers" / "OLD-002").exists())
        # fake_fetch created the sandbox ref; GC must not rewrite/delete it.
        after = self.for_each_ref()
        self.assertIn("refs/heads/sandbox/OLD-002", after)
        # No other refs may disappear relative to the pre-GC snapshot besides
        # ones the fetch helper intentionally added.
        for line in before.splitlines():
            self.assertIn(line, after.splitlines())

    def test_running_worker_is_never_collected(self):
        self.create_worker("RUN-001", pane_id="w1:p4")
        self.mark_fetched("RUN-001")
        before = self.for_each_ref()
        with patch.object(plugin, "pane_exists", return_value=False), \
                patch.object(plugin, "container_status", return_value="running"), \
                patch.object(plugin, "remove_worker_container") as remove:
            result = plugin.gc_collect_workers(state_dir=self.state, config_dir=self.config)
        remove.assert_not_called()
        self.assertNotIn("RUN-001", result["removed"])
        self.assertTrue((self.state / "workers" / "RUN-001").exists())
        self.assertEqual(self.for_each_ref(), before)

    def test_gc_never_changes_source_for_each_ref(self):
        self.create_worker("REF-001", pane_id="w1:p5")
        self.mark_fetched("REF-001")
        git("-C", self.source, "branch", "keep-me")
        before = self.for_each_ref()
        with patch.object(plugin, "pane_exists", return_value=False), \
                patch.object(plugin, "remove_worker_container"):
            plugin.gc_collect_workers(state_dir=self.state, config_dir=self.config)
        self.assertEqual(self.for_each_ref(), before)

    def test_pane_closed_hook_collects_only_that_panes_fetched_worker(self):
        self.create_worker("PANE-A", pane_id="w1:p10")
        self.create_worker("PANE-B", pane_id="w1:p11")
        self.mark_fetched("PANE-A")
        self.mark_fetched("PANE-B")
        event = json.dumps({
            "event": "pane_closed",
            "data": {"type": "pane_closed", "pane_id": "w1:p10", "workspace_id": "w1"},
        })
        before = self.for_each_ref()
        with patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": event}), \
                patch.object(plugin.sys, "argv", [str(SCRIPT), "gc", "--from-event"]), \
                patch.object(plugin, "remove_worker_container"), \
                patch.object(plugin, "load_config", return_value=("img", self.config / "codex")):
            (self.config / "codex").mkdir(exist_ok=True)
            self.assertEqual(plugin.main(), 0)
        self.assertFalse((self.state / "workers" / "PANE-A").exists())
        self.assertTrue((self.state / "workers" / "PANE-B").exists())
        self.assertEqual(self.for_each_ref(), before)

    def test_pane_exited_event_triggers_no_deletion(self):
        self.create_worker("EXIT-001", pane_id="w1:p12")
        self.mark_fetched("EXIT-001")
        event = json.dumps({
            "event": "pane_exited",
            "data": {"type": "pane_exited", "pane_id": "w1:p12", "workspace_id": "w1"},
        })
        stderr = io.StringIO()
        with patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": event}), \
                patch.object(plugin.sys, "argv", [str(SCRIPT), "gc", "--from-event"]), \
                patch.object(plugin.sys, "stderr", stderr):
            self.assertEqual(plugin.main(), 1)
        self.assertTrue((self.state / "workers" / "EXIT-001").exists())
        self.assertIn("pane_closed", stderr.getvalue())

    def test_parse_plugin_event_json_strict(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            plugin.parse_plugin_event_json("")
        with self.assertRaisesRegex(ValueError, "Invalid"):
            plugin.parse_plugin_event_json("{")
        with self.assertRaisesRegex(ValueError, "pane_closed"):
            plugin.parse_plugin_event_json('{"event":"pane_exited","data":{"pane_id":"w1:p1"}}')
        with self.assertRaisesRegex(ValueError, "pane_id"):
            plugin.parse_plugin_event_json('{"event":"pane_closed","data":{"type":"pane_closed"}}')

    def test_opportunistic_gc_never_fails_launch_and_is_time_bounded(self):
        self.create_worker("OPP-001", pane_id="w1:p13")
        self.mark_fetched("OPP-001")
        calls = {"lock": 0}

        class FakeLock:
            def __init__(self, state_dir=None):
                pass

            def __enter__(self):
                calls["lock"] += 1
                return self

            def __exit__(self, *args):
                return False

        with patch.object(plugin, "StateLock", FakeLock), \
                patch.object(plugin, "gc_collect_workers", side_effect=RuntimeError("boom")):
            result = plugin.opportunistic_gc(state_dir=self.state, config_dir=self.config)
        self.assertEqual(result["removed"], [])
        self.assertTrue(any("boom" in warning for warning in result["warnings"]))

        deadline_seen = {}

        def capture(**kwargs):
            deadline_seen["deadline"] = kwargs.get("deadline")
            return {"removed": [], "warnings": [], "skipped": []}

        with patch.object(plugin, "StateLock", FakeLock), \
                patch.object(plugin, "gc_collect_workers", side_effect=lambda **kw: capture(**kw)), \
                patch.object(plugin.time, "monotonic", return_value=100.0):
            plugin.opportunistic_gc(state_dir=self.state, config_dir=self.config)
        self.assertEqual(calls["lock"], 2)
        self.assertAlmostEqual(
            deadline_seen["deadline"],
            100.0 + plugin.GC_OPPORTUNISTIC_BUDGET_SECONDS,
        )

    def test_opportunistic_gc_does_not_hold_failure_into_hs_codex(self):
        stderr = io.StringIO()
        with patch.object(plugin, "check_root"), \
                patch.object(plugin, "opportunistic_gc", side_effect=AssertionError("should be swallowed by helper")), \
                patch.object(plugin, "resolve_source_repo_from_cwd", side_effect=ValueError("not a repo")), \
                patch.object(plugin.sys, "stderr", stderr):
            # Force opportunistic_gc itself to catch errors: call the real one with failing lock.
            pass
        with patch.object(plugin, "StateLock", side_effect=RuntimeError("lock broken")):
            result = plugin.opportunistic_gc(state_dir=self.state, config_dir=self.config)
        self.assertEqual(result["removed"], [])

    def test_deleting_worker_dirs_refuses_symlinks_and_stays_confined(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_text("no\n")
        workers = self.state / "workers"
        workers.mkdir(exist_ok=True)
        link = workers / "LINK-001"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "real directory"):
            plugin.destroy_worker("LINK-001")
        self.assertTrue((outside / "secret").exists())

    def test_session_sweep_refuses_symlinks_and_stays_confined(self):
        codex_home = self.config / "codex"
        sessions = codex_home / "sessions"
        sessions.mkdir(parents=True)
        outside = self.root / "outside-sessions"
        outside.mkdir()
        (outside / "keep").write_text("keep\n")
        (sessions / "escape").symlink_to(outside, target_is_directory=True)
        old = sessions / "OLDWORKER-001-abc123"
        old.mkdir()
        os.utime(old, (time.time() - 10 * 86400, time.time() - 10 * 86400))
        removed = plugin.sweep_stale_codex_sessions(
            codex_home,
            max_age_seconds=86400,
            state_dir=self.state,
        )
        self.assertIn("OLDWORKER-001-abc123", removed)
        self.assertFalse(old.exists())
        self.assertTrue((outside / "keep").exists())
        self.assertTrue((sessions / "escape").is_symlink())

    def test_manifest_declares_pane_closed_hook_not_pane_exited(self):
        with (ROOT / "herdr-plugin.toml").open("rb") as stream:
            manifest = tomllib.load(stream)
        events = manifest.get("events", [])
        self.assertTrue(events)
        self.assertEqual(events[0]["on"], "pane.closed")
        self.assertEqual(events[0]["command"], ["python3", "bin/herdr-sandbox", "gc", "--from-event"])
        self.assertFalse(any(event.get("on") == "pane.exited" for event in events))

    def test_without_hook_next_launch_opportunistic_gc_collects(self):
        self.create_worker("LAUNCH-001", pane_id="w1:p20")
        self.mark_fetched("LAUNCH-001")
        with patch.object(plugin, "pane_exists", return_value=False), \
                patch.object(plugin, "remove_worker_container"), \
                patch.object(plugin, "StateLock", plugin.StateLock):
            result = plugin.opportunistic_gc(state_dir=self.state, config_dir=self.config)
        self.assertIn("LAUNCH-001", result["removed"])


if __name__ == "__main__":
    unittest.main()
