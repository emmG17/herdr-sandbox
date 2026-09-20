import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_execute", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class ExecuteWorkerTests(unittest.TestCase):
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
        (worker / "worker.json").write_text(json.dumps({"id": "TEST-001", "status": "created"}))
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_execution_command_uses_codex_exec_without_tty(self):
        command = plugin.execute_command("TEST-001", "write a file")

        self.assertEqual(command[:3], ["podman", "run", "--rm"])
        self.assertNotIn("-it", command)
        self.assertNotIn("-i", command)
        self.assertNotIn("-t", command)
        for required in (
            "--userns=keep-id", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
            "--pids-limit=1024", "--memory=8g", "--cpus=4",
        ):
            self.assertIn(required, command)
        self.assertEqual(command[-5:], ["codex", "exec", "--sandbox", "danger-full-access", "write a file"])

    def test_execute_closes_stdin_and_reports_persisted_worker_changes(self):
        git_results = iter([
            (0, "abc123", ""),
            (0, " M persisted.txt", ""),
        ])
        persisted_file = self.state / "workers" / "TEST-001" / "repo" / "persisted.txt"

        def run_task(*args, **kwargs):
            persisted_file.write_text("saved by Codex\n")
            return subprocess.CompletedProcess([], 0, "task output\n", "")

        with patch.object(plugin, "check", return_value=[]) as check, \
                patch.object(plugin, "_git_read", side_effect=lambda *args: next(git_results)), \
                patch.object(plugin.subprocess, "run", side_effect=run_task) as run:
            result = plugin.execute_worker("TEST-001", "write persisted.txt")

        self.assertEqual(run.call_args.kwargs, {
            "stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "check": False,
        })
        check.assert_called_once_with(require_auth=True)
        self.assertEqual(result["exit_status"], 0)
        self.assertEqual(result["head"], "abc123")
        self.assertEqual(result["git_status"], " M persisted.txt")
        self.assertTrue(result["dirty"])
        self.assertEqual(persisted_file.read_text(), "saved by Codex\n")
        self.assertEqual(result["output_location"], "Herdr action output (stdout/stderr)")
        metadata = json.loads((self.state / "workers" / "TEST-001" / "worker.json").read_text())
        self.assertEqual(metadata["status"], "completed")

    def test_execute_failure_is_actionable_and_marks_worker_failed(self):
        with patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin, "_git_read", return_value=(0, "", "")), \
                patch.object(plugin.subprocess, "run", return_value=subprocess.CompletedProcess([], 9, "", "task failed\n")):
            result = plugin.execute_worker("TEST-001", "fail")

        self.assertEqual(result["exit_status"], 9)
        self.assertEqual(result["stderr"], "task failed\n")
        metadata = json.loads((self.state / "workers" / "TEST-001" / "worker.json").read_text())
        self.assertEqual(metadata["status"], "failed")

    def test_execute_action_reads_fixed_worker_and_prompt_from_config(self):
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG + '\n[execute]\nid = "TEST-001"\nprompt = "write a file"\n')
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "execute"]), \
                patch.object(plugin, "execute_worker", return_value={
                    "exit_status": 0, "stdout": "", "stderr": "", "worker_id": "TEST-001",
                    "head": "abc123", "dirty": False, "git_status": "", "output_location": "Herdr action output (stdout/stderr)",
                }) as execute:
            self.assertEqual(plugin.main(), 0)
        execute.assert_called_once_with("TEST-001", "write a file")

    def test_execute_action_does_not_read_stdin_for_prompt(self):
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "execute", "TEST-001", "write a file"]), \
                patch.object(plugin.sys, "stdin", io.StringIO("unexpected input")), \
                patch.object(plugin, "execute_worker", return_value={
                    "exit_status": 0, "stdout": "", "stderr": "", "worker_id": "TEST-001",
                    "head": "abc123", "dirty": False, "git_status": "", "output_location": "Herdr action output (stdout/stderr)",
                }) as execute:
            self.assertEqual(plugin.main(), 0)
        execute.assert_called_once_with("TEST-001", "write a file")


if __name__ == "__main__":
    unittest.main()
