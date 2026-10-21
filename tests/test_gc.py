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

    def test_stale_fetched_status_with_new_worker_commit_survives_gc(self):
        """Fetch, add a worker commit, close pane, run gc — worker must survive."""
        self.create_worker("STALE-001", pane_id="w1:p30")
        self.mark_fetched("STALE-001")
        worker_repo = self.state / "workers" / "STALE-001" / "repo"
        (worker_repo / "extra.txt").write_text("more work\n")
        git("-C", worker_repo, "add", "extra.txt")
        git("-C", worker_repo, "commit", "-qm", "post-fetch work")
        tip = git("-C", worker_repo, "rev-parse", "HEAD")
        metadata = json.loads((self.state / "workers" / "STALE-001" / "worker.json").read_text())
        self.assertNotEqual(tip, metadata["fetched_commit"])
        before = self.for_each_ref()
        with patch.object(plugin, "pane_exists", return_value=False), \
                patch.object(plugin, "remove_worker_container") as remove:
            result = plugin.gc_collect_workers(
                state_dir=self.state,
                config_dir=self.config,
                pane_id="w1:p30",
                allow_fetch=False,
            )
        remove.assert_not_called()
        self.assertNotIn("STALE-001", result["removed"])
        self.assertTrue((self.state / "workers" / "STALE-001").exists())
        self.assertTrue(any("tip moved" in warning for warning in result["warnings"]))
        self.assertEqual(self.for_each_ref(), before)

    def test_pane_exists_non_json_not_found_keeps_worker(self):
        self.create_worker("NF-001", pane_id="w1:p31")
        self.mark_fetched("NF-001")
        herdr = self.root / "fake-herdr"
        herdr.write_text("#!/bin/sh\necho 'herdr: command not found' >&2\nexit 127\n")
        herdr.chmod(0o755)
        with patch.object(plugin, "_herdr_bin", return_value=str(herdr)), \
                patch.object(plugin, "remove_worker_container") as remove:
            self.assertIsNone(plugin.pane_exists("w1:p31"))
            result = plugin.gc_collect_workers(state_dir=self.state, config_dir=self.config)
        remove.assert_not_called()
        self.assertNotIn("NF-001", result["removed"])
        self.assertTrue((self.state / "workers" / "NF-001").exists())

    def test_pane_exists_server_down_keeps_worker(self):
        self.create_worker("SD-001", pane_id="w1:p32")
        self.mark_fetched("SD-001")
        herdr = self.root / "fake-herdr-down"
        herdr.write_text("#!/bin/sh\necho 'error: server not found' >&2\nexit 1\n")
        herdr.chmod(0o755)
        with patch.object(plugin, "_herdr_bin", return_value=str(herdr)), \
                patch.object(plugin, "remove_worker_container") as remove:
            self.assertIsNone(plugin.pane_exists("w1:p32"))
            result = plugin.gc_collect_workers(state_dir=self.state, config_dir=self.config)
        remove.assert_not_called()
        self.assertNotIn("SD-001", result["removed"])
        self.assertTrue((self.state / "workers" / "SD-001").exists())

    def test_pane_exists_only_structured_pane_not_found_is_gone(self):
        herdr = self.root / "fake-herdr-json"
        herdr.write_text(
            "#!/bin/sh\n"
            "echo '{\"error\":{\"code\":\"pane_not_found\",\"message\":\"pane w1:p1 not found\"}}'\n"
            "exit 1\n"
        )
        herdr.chmod(0o755)
        with patch.object(plugin, "_herdr_bin", return_value=str(herdr)):
            self.assertIs(plugin.pane_exists("w1:p1"), False)

    def test_unavailable_podman_never_collected(self):
        self.create_worker("UNAV-001", pane_id="w1:p33")
        self.mark_fetched("UNAV-001")
        with patch.object(plugin, "pane_exists", return_value=False), \
                patch.object(plugin, "container_status", return_value="unavailable"), \
                patch.object(plugin, "remove_worker_container") as remove:
            result = plugin.gc_collect_workers(state_dir=self.state, config_dir=self.config)
        remove.assert_not_called()
        self.assertNotIn("UNAV-001", result["removed"])
        self.assertTrue(any("not safely collectible" in warning for warning in result["warnings"]))

    def test_error_container_status_never_collected(self):
        self.create_worker("ERR-001", pane_id="w1:p34")
        self.mark_fetched("ERR-001")
        with patch.object(plugin, "pane_exists", return_value=False), \
                patch.object(plugin, "container_status", return_value="error: cannot connect"), \
                patch.object(plugin, "remove_worker_container") as remove:
            result = plugin.gc_collect_workers(state_dir=self.state, config_dir=self.config)
        remove.assert_not_called()
        self.assertNotIn("ERR-001", result["removed"])
        self.assertTrue((self.state / "workers" / "ERR-001").exists())

    def test_opportunistic_gc_never_fails_launch_and_is_time_bounded(self):
        self.create_worker("OPP-001", pane_id="w1:p13")
        self.mark_fetched("OPP-001")
        calls = {"lock": 0, "blocking": []}

        class FakeLock:
            def __init__(self, state_dir=None, blocking=True):
                calls["blocking"].append(blocking)

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
            deadline_seen.update(kwargs)
            return {"removed": [], "warnings": [], "skipped": []}

        with patch.object(plugin, "StateLock", FakeLock), \
                patch.object(plugin, "gc_collect_workers", side_effect=lambda **kw: capture(**kw)), \
                patch.object(plugin.time, "monotonic", return_value=100.0):
            plugin.opportunistic_gc(state_dir=self.state, config_dir=self.config)
        self.assertEqual(calls["lock"], 2)
        self.assertEqual(calls["blocking"], [False, False])
        self.assertIs(deadline_seen.get("allow_fetch"), False)
        self.assertAlmostEqual(
            deadline_seen["deadline"],
            100.0 + plugin.GC_OPPORTUNISTIC_BUDGET_SECONDS,
        )

    def test_opportunistic_gc_skips_when_lock_held(self):
        self.create_worker("LOCK-001", pane_id="w1:p35")
        self.mark_fetched("LOCK-001")
        started = time.monotonic()
        with plugin.StateLock(self.state, blocking=True):
            result = plugin.opportunistic_gc(state_dir=self.state, config_dir=self.config)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result["removed"], [])
        self.assertTrue(any("state lock busy" in warning for warning in result["warnings"]))
        self.assertTrue((self.state / "workers" / "LOCK-001").exists())

    def test_opportunistic_gc_does_not_fetch_before_delete(self):
        self.create_worker("NOFETCH-001", pane_id="w1:p36")
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self.mark_created_at("NOFETCH-001", old)
        with patch.object(plugin, "fetch_worker", side_effect=AssertionError("must not fetch")) as fetch, \
                patch.object(plugin, "remove_worker_container"):
            result = plugin.opportunistic_gc(state_dir=self.state, config_dir=self.config)
        fetch.assert_not_called()
        self.assertNotIn("NOFETCH-001", result["removed"])
        self.assertTrue((self.state / "workers" / "NOFETCH-001").exists())

    def test_opportunistic_gc_does_not_hold_failure_into_hs_codex(self):
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
