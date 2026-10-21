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

from tests.helpers import git


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
        self.state.mkdir()
        (self.config / "codex").mkdir()
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)

        self.source = root / "source"
        self.source.mkdir()
        git("-C", self.source, "init", "-q")
        git("-C", self.source, "config", "user.name", "Source")
        git("-C", self.source, "config", "user.email", "source@example.invalid")
        (self.source / "source.txt").write_text("source\n")
        git("-C", self.source, "add", "source.txt")
        git("-C", self.source, "commit", "-qm", "initial")

        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

        with patch.object(plugin, "check", return_value=[]):
            self.worker_dir = plugin.create_worker("TEST-001", str(self.source))
        self.worker_repo = self.worker_dir / "repo"

    def _commit_worker_change(self, relative_path, content, message):
        path = self.worker_repo / relative_path
        path.write_text(content)
        safe_env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        subprocess.run(
            ["git", "-C", str(self.worker_repo), "config", "user.name", "Worker"],
            env=safe_env, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.worker_repo), "config", "user.email", "worker@example.invalid"],
            env=safe_env, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.worker_repo), "add", relative_path],
            env=safe_env, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.worker_repo), "commit", "-qm", message],
            env=safe_env, check=True, capture_output=True,
        )
        return git("-C", self.worker_repo, "rev-parse", "HEAD")

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
        # Shared dedicated home (configured plugin home), not a per-launch temp path.
        mount = next(arg for arg in command if arg.startswith("type=bind,src=") and ",dst=/codex," in arg)
        self.assertIn(str((self.config / "codex").resolve()), mount)

    def test_execute_streams_output_and_persists_result_and_log(self):
        worker_head = self._commit_worker_change("persisted.txt", "saved by Codex\n", "worker change")
        code = "print('task output', flush=True)"

        with patch.object(plugin, "check", return_value=[]) as check, \
                patch.object(plugin, "execute_command", return_value=[sys.executable, "-c", code]), \
                patch.object(plugin, "_query_worker_git_snapshot", return_value={
                    "status_porcelain": "",
                    "log": "",
                    "status_short": "",
                    "diff": "",
                    "error": None,
                }), \
                patch.object(plugin.sys, "stdout", new_callable=io.StringIO) as stdout, \
                patch.object(plugin.sys, "stderr", new_callable=io.StringIO) as stderr:
            result = plugin.execute_worker("TEST-001", "write persisted.txt")

        check.assert_called_once_with(require_auth=True)
        self.assertEqual(result["exit_status"], 0)
        self.assertTrue(result["run_id"].startswith("run-"))
        self.assertEqual(result["head"], worker_head)
        self.assertEqual(result["result_branch"], "sandbox/TEST-001")
        self.assertEqual(result["commit_count"], 1)
        self.assertFalse(result["dirty"])
        self.assertEqual(stdout.getvalue(), "task output\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(result["output_location"], result["log_location"])
        self.assertTrue(Path(result["log_location"]).is_file())
        self.assertTrue(Path(result["result_location"]).is_file())
        self.assertEqual(Path(result["log_location"]).read_text(), "task output\n")
        durable = json.loads(Path(result["result_location"]).read_text())
        self.assertEqual(durable["worker_id"], "TEST-001")
        self.assertEqual(durable["exit_status"], 0)
        self.assertEqual(durable["head"], worker_head)
        self.assertEqual(durable["result_branch"], "sandbox/TEST-001")
        self.assertEqual(durable["commit_count"], 1)
        self.assertEqual(durable["log_location"], result["log_location"])
        self.assertEqual(
            git("-C", self.source, "rev-parse", "refs/heads/sandbox/TEST-001"),
            worker_head,
        )
        metadata = json.loads((self.state / "workers" / "TEST-001" / "worker.json").read_text())
        self.assertEqual(metadata["status"], "fetched")
        self.assertEqual(metadata["latest_run_id"], result["run_id"])
        self.assertEqual(metadata["result_branch"], "sandbox/TEST-001")

    def test_execute_failure_is_actionable_and_marks_worker_failed_when_unfetched(self):
        with patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin, "execute_command", return_value=[
                    sys.executable, "-c",
                    "import sys; print('task failed', file=sys.stderr, flush=True); sys.exit(9)",
                ]), \
                patch.object(plugin, "fetch_worker", side_effect=ValueError("fetch blocked")), \
                patch.object(plugin, "_query_worker_git_snapshot", return_value={
                    "status_porcelain": "",
                    "log": "",
                    "status_short": "",
                    "diff": "",
                    "error": None,
                }), \
                patch.object(plugin.sys, "stdout", new_callable=io.StringIO), \
                patch.object(plugin.sys, "stderr", new_callable=io.StringIO) as stderr:
            result = plugin.execute_worker("TEST-001", "fail")

        self.assertEqual(result["exit_status"], 9)
        self.assertEqual(result["stderr"], "task failed\n")
        self.assertIn("task failed\n", stderr.getvalue())
        self.assertEqual(result["result_branch"], None)
        self.assertEqual(result["commit_count"], None)
        self.assertEqual(result["head"], "unavailable")
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
            "result_branch": "sandbox/TEST-001",
            "commit_count": 1,
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
            listed = plugin.format_workers(plugin.list_workers())
            inspected = plugin.format_worker(record)

        self.assertEqual(record["latest_result"]["run_id"], "run-test")
        # hs ls stays limited to RFC columns; latest-run detail is inspect-only.
        self.assertIn("TEST-001", listed)
        self.assertNotIn("run-test exit=0", listed)
        self.assertIn("Latest result:", inspected)
        self.assertIn(str(run_dir / "output.log"), inspected)

    def test_execute_config_table_is_ignored_with_deprecation_notice(self):
        (self.config / "config.toml").write_text(
            plugin.DEFAULT_CONFIG + '\n[execute]\nid = "STALE"\nprompt = "stale prompt"\n'
        )
        stderr = io.StringIO()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "execute", "--prompt", "one-shot prompt"]), \
                patch.object(plugin.sys, "stderr", stderr), \
                patch.object(plugin, "hs_codex", return_value=0) as hs_codex:
            self.assertEqual(plugin.main(), 0)
        hs_codex.assert_called_once_with([], prompt="one-shot prompt")
        self.assertIn("[execute] is deprecated and ignored", stderr.getvalue())

    def test_execute_without_prompt_does_not_read_execute_config(self):
        (self.config / "config.toml").write_text(
            plugin.DEFAULT_CONFIG + '\n[execute]\nid = "STALE"\nprompt = "stale prompt"\n'
        )
        stderr = io.StringIO()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "execute"]), \
                patch.object(plugin.sys, "stderr", stderr), \
                patch.object(plugin, "hs_codex", side_effect=AssertionError("should not launch")):
            self.assertEqual(plugin.main(), 1)
        message = stderr.getvalue()
        self.assertIn("[execute] is deprecated and ignored", message)
        self.assertIn("Task prompt is required", message)
        self.assertNotIn("stale prompt", message)

    def test_codex_prompt_reads_stdin_when_dash(self):
        stderr = io.StringIO()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "codex", "--prompt", "-"]), \
                patch.object(plugin.sys, "stdin", io.StringIO("stdin task\n")), \
                patch.object(plugin.sys, "stderr", stderr), \
                patch.object(plugin, "hs_codex", return_value=7) as hs_codex:
            self.assertEqual(plugin.main(), 7)
        hs_codex.assert_called_once_with([], prompt="-")

    def test_resolve_prompt_argument_reads_stdin_for_dash(self):
        with patch.object(plugin.sys, "stdin", io.StringIO("from stdin\n")):
            self.assertEqual(plugin.resolve_prompt_argument("-"), "from stdin\n")

    def test_hs_codex_prompt_propagates_exit_status(self):
        with patch.object(plugin, "prepare_hs_worker", return_value=("TEST-001", self.state)), \
                patch.object(plugin, "execute_worker", return_value={
                    "worker_id": "TEST-001",
                    "run_id": "run-test",
                    "exit_status": 12,
                    "head": "abc",
                    "dirty": False,
                    "git_status": "",
                    "result_branch": "sandbox/TEST-001",
                    "commit_count": 0,
                    "result_location": "/tmp/result.json",
                    "log_location": "/tmp/output.log",
                }) as execute, \
                patch.object(plugin, "print_execution_result") as printed:
            self.assertEqual(plugin.hs_codex([], prompt="do the task"), 12)
        execute.assert_called_once_with("TEST-001", "do the task")
        printed.assert_called_once()

    def test_hs_codex_prompt_rejects_extra_codex_args(self):
        stderr = io.StringIO()
        with patch.object(plugin, "prepare_hs_worker", return_value=("TEST-001", self.state)), \
                patch.object(plugin.sys, "stderr", stderr), \
                patch.object(plugin, "execute_worker", side_effect=AssertionError("should not run")):
            self.assertEqual(plugin.hs_codex(["--foo"], prompt="task"), 1)
        self.assertIn("Do not pass extra Codex arguments with --prompt", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
