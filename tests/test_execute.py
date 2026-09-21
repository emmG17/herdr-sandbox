import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
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

    def test_execute_streams_output_and_persists_result_and_log(self):
        git_results = iter([
            (0, "abc123", ""),
            (0, " M persisted.txt", ""),
        ])
        persisted_file = self.state / "workers" / "TEST-001" / "repo" / "persisted.txt"
        code = (
            "from pathlib import Path; "
            f"Path({str(persisted_file)!r}).write_text('saved by Codex\\n'); "
            "print('task output', flush=True)"
        )

        with patch.object(plugin, "check", return_value=[]) as check, \
                patch.object(plugin, "execute_command", return_value=[sys.executable, "-c", code]), \
                patch.object(plugin, "_git_read", side_effect=lambda *args: next(git_results)), \
                patch.object(plugin.sys, "stdout", new_callable=io.StringIO) as stdout, \
                patch.object(plugin.sys, "stderr", new_callable=io.StringIO) as stderr:
            result = plugin.execute_worker("TEST-001", "write persisted.txt")

        check.assert_called_once_with(require_auth=True)
        self.assertEqual(result["exit_status"], 0)
        self.assertTrue(result["run_id"].startswith("run-"))
        self.assertEqual(result["head"], "abc123")
        self.assertEqual(result["git_status"], " M persisted.txt")
        self.assertTrue(result["dirty"])
        self.assertEqual(stdout.getvalue(), "task output\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(persisted_file.read_text(), "saved by Codex\n")
        self.assertEqual(result["output_location"], result["log_location"])
        self.assertTrue(Path(result["log_location"]).is_file())
        self.assertTrue(Path(result["result_location"]).is_file())
        self.assertEqual(Path(result["log_location"]).read_text(), "task output\n")
        durable = json.loads(Path(result["result_location"]).read_text())
        self.assertEqual(durable["worker_id"], "TEST-001")
        self.assertEqual(durable["exit_status"], 0)
        self.assertEqual(durable["head"], "abc123")
        self.assertTrue(durable["dirty"])
        self.assertEqual(durable["log_location"], result["log_location"])
        metadata = json.loads((self.state / "workers" / "TEST-001" / "worker.json").read_text())
        self.assertEqual(metadata["status"], "completed")
        self.assertEqual(metadata["latest_run_id"], result["run_id"])

    def test_execute_failure_is_actionable_and_marks_worker_failed(self):
        with patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin, "execute_command", return_value=[sys.executable, "-c", "import sys; print('task failed', file=sys.stderr, flush=True); sys.exit(9)"]), \
                patch.object(plugin, "_git_read", return_value=(0, "", "")), \
                patch.object(plugin.sys, "stdout", new_callable=io.StringIO), \
                patch.object(plugin.sys, "stderr", new_callable=io.StringIO) as stderr:
            result = plugin.execute_worker("TEST-001", "fail")

        self.assertEqual(result["exit_status"], 9)
        self.assertEqual(result["stderr"], "task failed\n")
        self.assertEqual(stderr.getvalue(), "task failed\n")
        self.assertEqual(json.loads(Path(result["result_location"]).read_text())["status"], "failed")
        metadata = json.loads((self.state / "workers" / "TEST-001" / "worker.json").read_text())
        self.assertEqual(metadata["status"], "failed")

    def test_inspect_and_list_expose_latest_result(self):
        result = {
            "run_id": "run-test",
            "worker_id": "TEST-001",
            "status": "completed",
            "exit_status": 0,
            "head": "abc123",
            "dirty": False,
            "result_location": str(self.state / "workers" / "TEST-001" / "runs" / "run-test" / "result.json"),
            "log_location": str(self.state / "workers" / "TEST-001" / "runs" / "run-test" / "output.log"),
        }
        run_dir = self.state / "workers" / "TEST-001" / "runs" / "run-test"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(json.dumps(result))
        (run_dir / "output.log").write_text("done\n")
        metadata_path = self.state / "workers" / "TEST-001" / "worker.json"
        metadata = json.loads(metadata_path.read_text())
        metadata.update({"latest_run_id": "run-test", "latest_result": "runs/run-test/result.json"})
        metadata_path.write_text(json.dumps(metadata))

        with patch.object(plugin, "container_status", return_value="not-found"):
            record = plugin.inspect_worker("TEST-001")
            rendered = plugin.format_workers(plugin.list_workers())
            inspected = plugin.format_worker(record)

        self.assertEqual(record["latest_result"]["run_id"], "run-test")
        self.assertIn("run-test exit=0 HEAD=abc123 dirty=no", rendered)
        self.assertIn("Latest result:", inspected)
        self.assertIn(str(run_dir / "output.log"), inspected)

    def test_execute_action_reads_fixed_worker_and_prompt_from_config(self):
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG + '\n[execute]\nid = "TEST-001"\nprompt = "write a file"\n')
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "execute"]), \
                patch.object(plugin, "execute_worker", return_value={
                "exit_status": 0, "stdout": "", "stderr": "", "worker_id": "TEST-001",
                    "run_id": "run-test", "head": "abc123", "dirty": False, "git_status": "",
                    "result_location": "/tmp/result.json", "log_location": "/tmp/output.log", "output_location": "/tmp/output.log",
                }) as execute:
            self.assertEqual(plugin.main(), 0)
        execute.assert_called_once_with("TEST-001", "write a file")

    def test_direct_inputs_do_not_read_execute_config_or_mix_with_stale_values(self):
        (self.config / "config.toml").write_text(
            plugin.DEFAULT_CONFIG + '\n[execute]\nid = "STALE"\nprompt = "stale prompt"\n'
        )
        config_before = (self.config / "config.toml").read_bytes()
        with patch.object(plugin, "execute_options", side_effect=AssertionError("direct input read config")), \
                patch.object(plugin.sys, "argv", [
                    str(SCRIPT), "execute", "--id", "TEST-001", "--prompt", "one-shot prompt",
                ]), \
                patch.object(plugin, "execute_worker", return_value={
                    "exit_status": 0, "stdout": "", "stderr": "", "worker_id": "TEST-001",
                    "head": "abc123", "dirty": False, "git_status": "", "output_location": "Herdr action output (stdout/stderr)",
                }) as execute:
            self.assertEqual(plugin.main(), 0)

        execute.assert_called_once_with("TEST-001", "one-shot prompt")
        self.assertEqual((self.config / "config.toml").read_bytes(), config_before)

    def test_partial_direct_inputs_do_not_fall_back_to_execute_config(self):
        (self.config / "config.toml").write_text(
            plugin.DEFAULT_CONFIG + '\n[execute]\nid = "STALE"\nprompt = "stale prompt"\n'
        )
        stderr = io.StringIO()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "execute", "--id", "TEST-001"]), \
                patch.object(plugin.sys, "stderr", stderr), \
                patch.object(plugin, "execute_options", side_effect=AssertionError("partial input read config")):
            self.assertEqual(plugin.main(), 1)
        self.assertIn("Task prompt is required with a direct worker ID", stderr.getvalue())

    def test_execute_action_does_not_read_stdin_for_prompt(self):
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "execute", "TEST-001", "write a file"]), \
                patch.object(plugin.sys, "stdin", io.StringIO("unexpected input")), \
                patch.object(plugin, "execute_worker", return_value={
                "exit_status": 0, "stdout": "", "stderr": "", "worker_id": "TEST-001",
                    "run_id": "run-test", "head": "abc123", "dirty": False, "git_status": "",
                    "result_location": "/tmp/result.json", "log_location": "/tmp/output.log", "output_location": "/tmp/output.log",
                }) as execute:
            self.assertEqual(plugin.main(), 0)
        execute.assert_called_once_with("TEST-001", "write a file")


if __name__ == "__main__":
    unittest.main()
