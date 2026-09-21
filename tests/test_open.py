import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_open", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class OpenWorkerTests(unittest.TestCase):
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
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "network": "online",
        }))
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
            "HERDR_ENV": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _git_repo(self, path):
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        return path.resolve()

    def test_runtime_command_enforces_isolation_and_only_mounts_worker_paths(self):
        command = plugin.open_command("TEST-001", ["codex", "--model", "gpt-5"])

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
        self.assertIn(f"type=bind,src={self.state / 'workers' / 'TEST-001' / 'repo'},dst=/workspace,rw", mounts)
        self.assertIn(f"type=bind,src={self.config / 'codex'},dst=/codex,rw", mounts)
        self.assertEqual(command[-3:], ["codex", "--model", "gpt-5"])

    def test_runtime_command_uses_offline_network_and_configured_limits(self):
        (self.config / "config.toml").write_text(
            '[runtime]\nimage = "localhost/herdr-codex-worker:latest"\nnetwork = "offline"\n'
            '[resources]\ncpus = 2\nmemory = "3g"\npids = 99\n[codex]\nhome = "codex"\n'
        )

        command = plugin.open_command("TEST-001", ["bash"])

        self.assertIn("--network=none", command)
        self.assertIn("--cpus=2", command)
        self.assertIn("--memory=3g", command)
        self.assertIn("--pids-limit=99", command)

    def test_runtime_rejects_forbidden_privilege_namespace_network_and_mount_settings(self):
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
                    plugin.open_command("TEST-001", ["bash"])

    def test_open_worker_checks_readiness_runs_interactively_and_updates_lifecycle(self):
        with patch.object(plugin, "check", return_value=[]) as check, patch.object(plugin.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(plugin.open_worker("TEST-001", ["bash"]), 0)

        check.assert_called_once_with(require_auth=False)
        self.assertEqual(run.call_args.kwargs, {"check": False})
        self.assertIn("-it", run.call_args.args[0])
        metadata = json.loads((self.state / "workers" / "TEST-001" / "worker.json").read_text())
        self.assertEqual(metadata["status"], "stopped")

    def test_open_worker_marks_failed_session(self):
        with patch.object(plugin, "check", return_value=[]), patch.object(plugin.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)):
            self.assertEqual(plugin.open_worker("TEST-001", ["bash"]), 7)

        metadata = json.loads((self.state / "workers" / "TEST-001" / "worker.json").read_text())
        self.assertEqual(metadata["status"], "failed")

    def test_opening_codex_requires_dedicated_authentication(self):
        with patch.object(plugin, "check", return_value=["Codex authentication is missing"]), patch.object(plugin.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "authentication"):
                plugin.open_worker("TEST-001", ["codex", "--model", "gpt-5"])
        run.assert_not_called()

    def test_open_action_uses_configured_worker_and_default_shell(self):
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG + "\n[open]\nid = \"TEST-001\"\n")
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "open"]), patch.object(plugin, "open_worker", return_value=0) as open_worker:
            self.assertEqual(plugin.main(), 0)
        open_worker.assert_called_once_with("TEST-001", ["bash"])

    def test_open_cli_passes_codex_arguments_through(self):
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "open", "TEST-001", "--", "codex", "--model", "gpt-5"]), \
                patch.object(plugin, "open_worker", return_value=0) as open_worker:
            self.assertEqual(plugin.main(), 0)
        open_worker.assert_called_once_with("TEST-001", ["codex", "--model", "gpt-5"], fresh=True)

    def test_start_codex_subcommand_selects_a_fresh_worker_session(self):
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "start-codex", "--id", "TEST-001"]), \
                patch.object(plugin, "open_worker", return_value=0) as open_worker:
            self.assertEqual(plugin.main(), 0)
        open_worker.assert_called_once_with("TEST-001", ["codex"], fresh=True)

    def test_start_codex_uses_selected_worker_not_active_workspace_or_shared_history(self):
        root = Path(self.temp.name)
        landing_page = self._git_repo(root / "landing-page-1")
        plugin_development = self._git_repo(root / "plugin-development")
        worker = self.state / "workers" / "TEST-001"
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "source_repo": str(landing_page),
        }))
        auth = self.config / "codex" / "auth.json"
        auth.write_text('{"token":"test"}\n')
        old_history = self.config / "codex" / "sessions" / "unrelated.jsonl"
        old_history.parent.mkdir()
        old_history.write_text("unrelated task\n")

        real_run = plugin.subprocess.run
        mounted_home = None

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
                codex_mount = next(mount for mount in mounts if ",dst=/codex," in mount)
                mounted_home = Path(codex_mount.split(",src=", 1)[1].split(",dst=", 1)[0])
                self.assertTrue((mounted_home / "auth.json").is_file())
                self.assertFalse((mounted_home / "unrelated.jsonl").exists())
                return subprocess.CompletedProcess(command, 0)
            return real_run(*args, **kwargs)

        with patch.dict(os.environ, {
            "HERDR_ENV": "1",
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_cwd": str(landing_page)}),
        }), patch.object(plugin, "check", return_value=[]), patch.object(plugin.subprocess, "run", side_effect=fake_run):
            self.assertEqual(plugin.open_worker("TEST-001", ["codex"], fresh=True), 0)

        self.assertIsNotNone(mounted_home)
        self.assertFalse(mounted_home.exists())

    def test_start_codex_refuses_a_worker_from_a_different_workspace(self):
        root = Path(self.temp.name)
        landing_page = self._git_repo(root / "landing-page-1")
        plugin_development = self._git_repo(root / "plugin-development")
        worker = self.state / "workers" / "TEST-001"
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "source_repo": str(landing_page),
        }))
        real_run = plugin.subprocess.run
        podman_calls = []

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

        self.assertEqual(podman_calls, [])
        metadata = json.loads((worker / "worker.json").read_text())
        self.assertEqual(metadata["status"], "created")

    def test_open_worker_validates_workspace_for_default_shell(self):
        root = Path(self.temp.name)
        landing_page = self._git_repo(root / "landing-page-1")
        plugin_development = self._git_repo(root / "plugin-development")
        worker = self.state / "workers" / "TEST-001"
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "source_repo": str(landing_page),
        }))
        with patch.dict(os.environ, {
            "HERDR_ENV": "1",
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_cwd": str(plugin_development)}),
        }):
            with self.assertRaisesRegex(ValueError, "mismatched workspace"):
                plugin.open_worker("TEST-001", ["bash"])

    def test_open_action_refuses_an_explicit_worker_from_a_different_workspace(self):
        root = Path(self.temp.name)
        landing_page = self._git_repo(root / "landing-page-1")
        plugin_development = self._git_repo(root / "plugin-development")
        worker = self.state / "workers" / "TEST-001"
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "source_repo": str(landing_page),
        }))
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "open", "--id", "TEST-001"]), \
                patch.object(plugin, "open_worker") as open_worker:
            with patch.dict(os.environ, {
                "HERDR_ENV": "1",
                "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_cwd": str(plugin_development)}),
            }):
                self.assertEqual(plugin.main(), 1)
        open_worker.assert_not_called()

    def test_open_action_selects_sole_worker_in_current_workspace(self):
        root = Path(self.temp.name)
        landing_page = self._git_repo(root / "landing-page-1")
        worker = self.state / "workers" / "TEST-001"
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "source_repo": str(landing_page),
        }))
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "open"]), \
                patch.object(plugin, "open_worker", return_value=0) as open_worker:
            with patch.dict(os.environ, {
                "HERDR_ENV": "1",
                "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_cwd": str(landing_page)}),
            }):
                self.assertEqual(plugin.main(), 0)
        open_worker.assert_called_once_with("TEST-001", ["bash"])

    def test_open_action_requires_id_when_workspace_has_multiple_workers(self):
        root = Path(self.temp.name)
        landing_page = self._git_repo(root / "landing-page-1")
        for worker_id in ("TEST-001", "TEST-002"):
            worker = self.state / "workers" / worker_id
            if worker_id == "TEST-002":
                (worker / "repo" / ".git").mkdir(parents=True)
            (worker / "worker.json").write_text(json.dumps({
                "id": worker_id, "status": "created", "source_repo": str(landing_page),
            }))
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "open"]), \
                patch.object(plugin, "open_worker") as open_worker:
            with patch.dict(os.environ, {
                "HERDR_ENV": "1",
                "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_cwd": str(landing_page)}),
            }):
                self.assertEqual(plugin.main(), 1)
        open_worker.assert_not_called()

    def test_open_action_fails_when_herdr_context_is_missing(self):
        worker = self.state / "workers" / "TEST-001"
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created",
        }))
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "open"]), \
                patch.object(plugin, "open_worker") as open_worker:
            with patch.dict(os.environ, {"HERDR_ENV": "1"}):
                self.assertEqual(plugin.main(), 1)
        open_worker.assert_not_called()

    def test_open_action_does_not_select_worker_from_unrelated_workspace(self):
        root = Path(self.temp.name)
        landing_page = self._git_repo(root / "landing-page-1")
        plugin_development = self._git_repo(root / "plugin-development")
        worker = self.state / "workers" / "TEST-001"
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "source_repo": str(landing_page),
        }))
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "open"]), \
                patch.object(plugin, "open_worker") as open_worker:
            with patch.dict(os.environ, {
                "HERDR_ENV": "1",
                "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_cwd": str(plugin_development)}),
            }):
                self.assertEqual(plugin.main(), 1)
        open_worker.assert_not_called()

    def test_open_action_refuses_configured_worker_from_a_different_workspace(self):
        root = Path(self.temp.name)
        landing_page = self._git_repo(root / "landing-page-1")
        plugin_development = self._git_repo(root / "plugin-development")
        worker = self.state / "workers" / "TEST-001"
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "source_repo": str(landing_page),
        }))
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG + "\n[open]\nid = \"TEST-001\"\n")
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "open"]), \
                patch.object(plugin, "open_worker") as open_worker:
            with patch.dict(os.environ, {
                "HERDR_ENV": "1",
                "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_cwd": str(plugin_development)}),
            }):
                self.assertEqual(plugin.main(), 1)
        open_worker.assert_not_called()

    def test_start_codex_requires_id_when_workspace_has_multiple_workers(self):
        root = Path(self.temp.name)
        landing_page = self._git_repo(root / "landing-page-1")
        for worker_id in ("TEST-001", "TEST-002"):
            worker = self.state / "workers" / worker_id
            if worker_id == "TEST-002":
                (worker / "repo" / ".git").mkdir(parents=True)
            (worker / "worker.json").write_text(json.dumps({
                "id": worker_id, "status": "created", "source_repo": str(landing_page),
            }))
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "start-codex"]), \
                patch.object(plugin, "open_worker") as open_worker:
            with patch.dict(os.environ, {
                "HERDR_ENV": "1",
                "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_cwd": str(landing_page)}),
            }):
                self.assertEqual(plugin.main(), 1)
        open_worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
