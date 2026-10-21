"""Runtime isolation and issue #29: never mount the configured agent home."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_runtime_isolation", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)

AGENT_MOUNT_DSTS = frozenset({
    plugin.DEFAULT_PROFILE.mount_point,
    plugin.CLAUDE_PROFILE.mount_point,
})


def _mount_specs(command):
    specs = []
    for index, value in enumerate(command):
        if value == "--mount" and index + 1 < len(command):
            raw = command[index + 1]
            parts = {}
            for piece in raw.split(","):
                if "=" in piece:
                    key, val = piece.split("=", 1)
                    parts[key] = val
            specs.append(parts)
    return specs


def _assert_agent_mounts_are_sessions(test, command, configured_home):
    """Every agent-home mount must be under configured_home/sessions, never the home itself."""
    configured = Path(configured_home).resolve()
    sessions = (configured / "sessions").resolve()
    agent_mounts = [spec for spec in _mount_specs(command) if spec.get("dst") in AGENT_MOUNT_DSTS]
    for spec in agent_mounts:
        src = Path(spec["src"]).resolve()
        test.assertNotEqual(src, configured)
        test.assertTrue(
            src.is_relative_to(sessions),
            f"agent mount src {src} must be under {sessions}",
        )
    return agent_mounts


class RuntimeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = root / "config"
        self.config.mkdir()
        self.state = root / "state"
        worker = self.state / "workers" / "TEST-001"
        (worker / "repo" / ".git").mkdir(parents=True)
        (self.config / "codex").mkdir()
        (self.config / "codex" / "auth.json").write_text(
            '{"tokens":{"access_token":"test","refresh_token":"test","id_token":"t"}}\n'
        )
        (self.config / "codex" / "auth.json").chmod(0o600)
        (self.config / "claude").mkdir()
        (self.config / "claude" / ".credentials.json").write_text(
            json.dumps({
                "claudeAiOauth": {
                    "accessToken": "old", "refreshToken": "r1", "expiresAt": 1,
                },
            }) + "\n"
        )
        (self.config / "claude" / ".credentials.json").chmod(0o600)
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "network": "online",
        }))
        self.configured_codex = (self.config / "codex").resolve()
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
            "HERDR_ENV": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _session(self, worker_id="TEST-001", profile=None):
        profile = plugin.DEFAULT_PROFILE if profile is None else profile
        _, configured = plugin.load_config(self.config, profile)
        return plugin.fresh_agent_home(configured, worker_id, profile)

    def _git_repo(self, path):
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        return path.resolve()

    def test_runtime_command_enforces_isolation_and_only_mounts_worker_and_session(self):
        session, _ = self._session()
        command = plugin.open_command(
            "TEST-001", ["codex", "--model", "gpt-5"], agent_home=session,
        )

        self.assertEqual(command[:4], ["podman", "run", "--rm", "-it"])
        for required in (
            "--userns=keep-id", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--read-only", "--pids-limit=1024", "--memory=8g", "--cpus=4",
        ):
            self.assertIn(required, command)
        self.assertNotIn("--privileged", command)
        self.assertNotIn("--network=host", command)
        self.assertNotIn("--pid=host", command)
        self.assertNotIn("--userns=host", command)
        mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertIn(
            f"type=bind,src={self.state / 'workers' / 'TEST-001' / 'repo'},dst=/workspace,rw",
            mounts,
        )
        agent_mounts = _assert_agent_mounts_are_sessions(self, command, self.configured_codex)
        self.assertEqual(len(agent_mounts), 1)
        self.assertEqual(Path(agent_mounts[0]["src"]).resolve(), session.resolve())
        self.assertEqual(command[-3:], ["codex", "--model", "gpt-5"])

    def test_runtime_command_uses_offline_network_and_configured_limits(self):
        (self.config / "config.toml").write_text(
            '[runtime]\nimage = "localhost/herdr-codex-worker:latest"\nnetwork = "offline"\n'
            '[resources]\ncpus = 2\nmemory = "3g"\npids = 99\n[codex]\nhome = "codex"\n'
        )
        session, _ = self._session()
        command = plugin.open_command("TEST-001", ["bash"], agent_home=session)

        self.assertIn("--network=none", command)
        self.assertIn("--cpus=2", command)
        self.assertIn("--memory=3g", command)
        self.assertIn("--pids-limit=99", command)
        _assert_agent_mounts_are_sessions(self, command, self.configured_codex)

    def test_runtime_rejects_forbidden_privilege_namespace_network_and_mount_settings(self):
        session, _ = self._session()
        forbidden_settings = (
            'privileged = true',
            'pid = "host"',
            'network = "host"',
            'userns = "host"',
            'mounts = ["/"]',
            'mounts = ["/home/tester"]',
            'mounts = ["/home/tester/.ssh"]',
            'mounts = ["/home/tester/.gnupg"]',
            'mounts = ["/home/tester/.aws"]',
            'mounts = ["/home/tester/.kube"]',
            'mounts = ["/home/tester/.config"]',
            'mounts = ["/run/podman/podman.sock"]',
            'mounts = ["/var/run/docker.sock"]',
        )
        for setting in forbidden_settings:
            with self.subTest(setting=setting):
                self.config.joinpath("config.toml").write_text(
                    plugin.DEFAULT_CONFIG.replace('network = "online"', setting)
                    if setting.startswith("network") else plugin.DEFAULT_CONFIG.replace(
                        'image = "localhost/herdr-codex-worker:latest"',
                        'image = "localhost/herdr-codex-worker:latest"\n' + setting,
                    )
                )
                with self.assertRaisesRegex(ValueError, "not permitted|network"):
                    plugin.open_command("TEST-001", ["bash"], agent_home=session)

    def test_open_command_and_podman_runtime_refuse_configured_home(self):
        with self.assertRaisesRegex(ValueError, "explicit per-launch"):
            plugin.open_command("TEST-001", ["bash"])
        with self.assertRaisesRegex(ValueError, "explicit per-launch|never mount"):
            plugin.podman_runtime("TEST-001", agent_home=None)
        with self.assertRaisesRegex(ValueError, "inside the dedicated plugin home"):
            plugin.open_command("TEST-001", ["bash"], agent_home=self.configured_codex)

    def test_open_worker_checks_readiness_runs_interactively_and_updates_lifecycle(self):
        with patch.object(plugin, "check", return_value=[]) as check, \
                patch.object(plugin.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(plugin.open_worker("TEST-001", ["bash"]), 0)

        check.assert_called_once_with(require_auth=False, profile=plugin.DEFAULT_PROFILE)
        self.assertEqual(run.call_args.kwargs, {"check": False})
        self.assertIn("-it", run.call_args.args[0])
        _assert_agent_mounts_are_sessions(self, run.call_args.args[0], self.configured_codex)
        metadata = json.loads((self.state / "workers" / "TEST-001" / "worker.json").read_text())
        self.assertEqual(metadata["status"], "stopped")

    def test_open_worker_marks_failed_session(self):
        with patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)):
            self.assertEqual(plugin.open_worker("TEST-001", ["bash"]), 7)

        metadata = json.loads((self.state / "workers" / "TEST-001" / "worker.json").read_text())
        self.assertEqual(metadata["status"], "failed")

    def test_opening_codex_requires_dedicated_authentication(self):
        (self.config / "codex" / "auth.json").unlink()
        with patch.object(plugin, "check", return_value=["Codex authentication is missing"]), \
                patch.object(plugin.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "authentication"):
                plugin.open_worker("TEST-001", ["codex", "--model", "gpt-5"])
        run.assert_not_called()

    def test_open_worker_fresh_session_home_has_auth_only_no_history(self):
        root = Path(self.temp.name)
        landing_page = self._git_repo(root / "landing-page-1")
        plugin_development = self._git_repo(root / "plugin-development")
        worker = self.state / "workers" / "TEST-001"
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "source_repo": str(landing_page),
        }))
        old_history = self.config / "codex" / "sessions" / "unrelated.jsonl"
        old_history.parent.mkdir(exist_ok=True)
        old_history.write_text("unrelated task\n")

        mounted_home = None
        real_run = plugin.subprocess.run

        def fake_run(*args, **kwargs):
            nonlocal mounted_home
            command = args[0]
            if command and command[0] == "podman":
                mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
                worker_mount = f"type=bind,src={(worker / 'repo').resolve()},dst=/workspace,rw"
                self.assertIn(worker_mount, mounts)
                self.assertNotIn(str(plugin_development), " ".join(command))
                self.assertEqual(command[command.index("--workdir") + 1], "/workspace")
                self.assertEqual(command[-3:], ["codex", "--cd", "/workspace"])
                agent_mounts = _assert_agent_mounts_are_sessions(
                    self, command, self.configured_codex,
                )
                self.assertEqual(len(agent_mounts), 1)
                mounted_home = Path(agent_mounts[0]["src"])
                self.assertTrue((mounted_home / "auth.json").is_file())
                self.assertFalse((mounted_home / "unrelated.jsonl").exists())
                return subprocess.CompletedProcess(command, 0)
            return real_run(*args, **kwargs)

        with patch.dict(os.environ, {
            "HERDR_ENV": "1",
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_cwd": str(landing_page)}),
        }), patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin.subprocess, "run", side_effect=fake_run):
            self.assertEqual(plugin.open_worker("TEST-001", ["codex"], fresh=True), 0)

        self.assertIsNotNone(mounted_home)
        self.assertFalse(mounted_home.exists())

    def test_open_worker_refuses_mismatched_workspace(self):
        root = Path(self.temp.name)
        landing_page = self._git_repo(root / "landing-page-1")
        plugin_development = self._git_repo(root / "plugin-development")
        worker = self.state / "workers" / "TEST-001"
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "source_repo": str(landing_page),
        }))
        podman_calls = []
        real_run = plugin.subprocess.run

        def fake_run(*args, **kwargs):
            if args[0] and args[0][0] == "podman":
                podman_calls.append(args[0])
            return real_run(*args, **kwargs)

        with patch.dict(os.environ, {
            "HERDR_ENV": "1",
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_cwd": str(plugin_development)}),
        }), patch.object(plugin.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(ValueError, "mismatched workspace"):
                plugin.open_worker("TEST-001", ["codex"], fresh=True)
            with self.assertRaisesRegex(ValueError, "mismatched workspace"):
                plugin.open_worker("TEST-001", ["bash"])

        self.assertEqual(podman_calls, [])
        metadata = json.loads((worker / "worker.json").read_text())
        self.assertEqual(metadata["status"], "created")

    def test_issue29_no_entry_point_mounts_configured_agent_home(self):
        """Acceptance (#29): podman argv never bind-mounts configured agent home."""
        session, _ = self._session()
        configured = self.configured_codex
        captured = []

        def capture_run(*args, **kwargs):
            command = args[0]
            if command and command[0] == "podman":
                captured.append(list(command))
            return subprocess.CompletedProcess(command, 0)

        # open_command / execute_command (explicit session)
        interactive = plugin.open_command("TEST-001", ["bash"], agent_home=session)
        _assert_agent_mounts_are_sessions(self, interactive, configured)
        prompt_cmd = plugin.execute_command("TEST-001", "do work", agent_home=session)
        _assert_agent_mounts_are_sessions(self, prompt_cmd, configured)

        # open_worker (interactive agent and bash) — always fresh_agent_home
        with patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin.subprocess, "run", side_effect=capture_run):
            self.assertEqual(plugin.open_worker("TEST-001", ["bash"]), 0)
            self.assertEqual(plugin.open_worker("TEST-001", ["codex"], fresh=True), 0)

        # execute_worker path
        with patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin.subprocess, "Popen") as popen, \
                patch.object(plugin, "fetch_worker", return_value={
                    "commit": "a" * 40,
                    "branch": "sandbox/TEST-001",
                    "commit_count": 0,
                    "git_snapshot": {
                        "status_porcelain": "", "log": "", "status_short": "",
                        "diff": "", "error": None,
                    },
                }), \
                patch.object(plugin, "_stream_process", return_value=(0, {"stdout": "", "stderr": ""})):
            def fake_popen(argv, **kwargs):
                if argv and argv[0] == "podman":
                    captured.append(list(argv))
                proc = mock.Mock()
                proc.poll.return_value = 0
                return proc
            popen.side_effect = fake_popen
            plugin.execute_worker("TEST-001", "prompt task")

        # hs_agent interactive and --prompt
        with patch.object(plugin, "prepare_hs_worker", return_value=("TEST-001", self.state)), \
                patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin.subprocess, "run", side_effect=capture_run), \
                patch.object(plugin, "fetch_worker", return_value={
                    "commit": "a" * 40,
                    "branch": "sandbox/TEST-001",
                    "commit_count": 0,
                    "log_oneline": "",
                    "changed_files": [],
                    "diff_stat": "",
                    "base_commit": "b" * 40,
                    "source_repo": "/tmp",
                    "worker_id": "TEST-001",
                }), \
                patch.object(plugin, "format_fetch_result", return_value="ok"):
            self.assertEqual(plugin.hs_agent(plugin.DEFAULT_PROFILE, []), 0)

        with patch.object(plugin, "prepare_hs_worker", return_value=("TEST-001", self.state)), \
                patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin.subprocess, "Popen") as popen, \
                patch.object(plugin, "fetch_worker", return_value={
                    "commit": "a" * 40,
                    "branch": "sandbox/TEST-001",
                    "commit_count": 0,
                    "git_snapshot": {
                        "status_porcelain": "", "log": "", "status_short": "",
                        "diff": "", "error": None,
                    },
                }), \
                patch.object(plugin, "_stream_process", return_value=(0, {"stdout": "", "stderr": ""})), \
                patch.object(plugin, "print_execution_result"):
            def fake_popen_prompt(argv, **kwargs):
                if argv and argv[0] == "podman":
                    captured.append(list(argv))
                proc = mock.Mock()
                proc.poll.return_value = 0
                return proc
            popen.side_effect = fake_popen_prompt
            self.assertEqual(plugin.hs_agent(plugin.DEFAULT_PROFILE, [], prompt="task"), 0)

        # git query path builds podman argv without agent mounts
        runtime = plugin.podman_runtime("TEST-001", agent_home=session)
        query = plugin.build_worker_git_query_command(runtime, base_commit="a" * 40)
        captured.append(query)
        query_agent = [
            spec for spec in _mount_specs(query) if spec.get("dst") in AGENT_MOUNT_DSTS
        ]
        self.assertEqual(query_agent, [])

        self.assertGreaterEqual(len(captured), 4)
        for command in captured:
            with self.subTest(command=command[:6]):
                agent_mounts = [
                    spec for spec in _mount_specs(command)
                    if spec.get("dst") in AGENT_MOUNT_DSTS
                ]
                for spec in agent_mounts:
                    src = Path(spec["src"]).resolve()
                    self.assertNotEqual(src, configured)
                    self.assertTrue(src.is_relative_to((configured / "sessions").resolve()))


if __name__ == "__main__":
    unittest.main()
