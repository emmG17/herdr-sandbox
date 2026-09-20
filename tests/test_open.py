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
        })
        self.env.start()
        self.addCleanup(self.env.stop)

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
        open_worker.assert_called_once_with("TEST-001", ["codex", "--model", "gpt-5"])


if __name__ == "__main__":
    unittest.main()
