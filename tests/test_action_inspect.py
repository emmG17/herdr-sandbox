"""Menu inspect action uses workspace_cwd, not process cwd / focused pane."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.helpers import git


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_action_inspect", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class ActionInspectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.plugin_dir = root / "plugin-root"
        self.plugin_dir.mkdir()
        self.source = root / "source"
        self.source.mkdir()
        git("init", "-q", self.source)
        git("-C", self.source, "config", "user.name", "Test")
        git("-C", self.source, "config", "user.email", "test@example.invalid")
        (self.source / "file.txt").write_text("ok\n")
        git("-C", self.source, "add", "file.txt")
        git("-C", self.source, "commit", "-qm", "init")
        self.config = root / "config"
        self.config.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)

    def _write_worker(self, worker_id, pane_id, source=None):
        source = source or self.source
        worker = plugin.create_worker(worker_id, str(source))
        metadata = json.loads((worker / "worker.json").read_text())
        metadata["pane_id"] = pane_id
        metadata["source_repo"] = str(source.resolve())
        (worker / "worker.json").write_text(json.dumps(metadata))
        return worker

    def test_menu_inspect_lists_all_workspace_workers_not_focused_pane(self):
        """Action env: plugin-dir cwd, focused pane != worker pane, workspace_cwd set."""
        self._write_worker("W-A", "w1:p-worker-a")
        self._write_worker("W-B", "w1:p-worker-b")
        other = Path(self.temp.name) / "other-source"
        other.mkdir()
        git("init", "-q", other)
        git("-C", other, "config", "user.name", "Test")
        git("-C", other, "config", "user.email", "test@example.invalid")
        (other / "x.txt").write_text("x\n")
        git("-C", other, "add", "x.txt")
        git("-C", other, "commit", "-qm", "other")
        self._write_worker("W-OTHER", "w1:p-other", source=other)

        action_env = {
            "HERDR_ENV": "1",
            "HERDR_PLUGIN_ACTION_ID": "inspect",
            "HERDR_PANE_ID": "w1:p-focused-not-worker",
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({
                "workspace_cwd": str(self.source.resolve()),
                "focused_pane_id": "w1:p-focused-not-worker",
            }),
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        }
        stdout = io.StringIO()
        with patch.dict(os.environ, action_env, clear=False), \
                patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect"]), \
                patch.object(plugin.os, "getcwd", return_value=str(self.plugin_dir)), \
                patch.object(Path, "cwd", return_value=self.plugin_dir), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(plugin.main(), 0)
        text = stdout.getvalue()
        self.assertIn("W-A", text)
        self.assertIn("W-B", text)
        self.assertNotIn("W-OTHER", text)
        self.assertIn("PANE", text)
        self.assertIn("STATUS", text)
        self.assertIn("RESULT", text)
        self.assertIn("Review (W-A):", text)
        self.assertIn("Review (W-B):", text)
        self.assertIn("sandbox/W-A", text)
        self.assertIn("sandbox/W-B", text)
        # Must not collapse to a single guessed worker from the focused pane.
        self.assertNotIn("No worker found for pane w1:p-focused-not-worker", text)

    def test_cli_inspect_without_id_still_uses_pane_and_cwd(self):
        self._write_worker("PANE-001", "w1:p1")
        with patch.dict(os.environ, {
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        }, clear=False), \
                patch.object(plugin, "cwd_repo_root", return_value=self.source.resolve()), \
                patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect"]), \
                patch.object(plugin, "inspect_worker", wraps=plugin.inspect_worker) as inspect_worker, \
                contextlib.redirect_stdout(io.StringIO()):
            # Ensure we are not in action mode.
            os.environ.pop("HERDR_PLUGIN_ACTION_ID", None)
            self.assertEqual(plugin.main(), 0)
        self.assertEqual(inspect_worker.call_args.args[0], "PANE-001")

    def test_action_id_inspect_does_not_change_hs_agent_or_other_commands(self):
        """HERDR_PLUGIN_ACTION_ID=inspect must only affect the inspect command."""
        self._write_worker("KEEP-001", "w1:p1")
        action_env = {
            "HERDR_PLUGIN_ACTION_ID": "inspect",
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({
                "workspace_cwd": str(self.source.resolve()),
            }),
        }
        with patch.dict(os.environ, action_env, clear=False):
            self.assertTrue(plugin.inspect_from_plugin_action())
            with patch.object(plugin, "prepare_hs_worker", return_value=("KEEP-001", self.state)) as prepare, \
                    patch.object(plugin, "open_worker", return_value=0) as opened, \
                    patch.object(plugin, "fetch_worker", return_value={
                        "commit": "a" * 40,
                        "branch": "sandbox/KEEP-001",
                        "commit_count": 0,
                        "log_oneline": "",
                        "changed_files": [],
                        "diff_stat": "",
                        "base_commit": "b" * 40,
                        "source_repo": str(self.source),
                        "worker_id": "KEEP-001",
                    }), \
                    patch("sys.stdout", new_callable=io.StringIO):
                code = plugin.hs_agent(plugin.DEFAULT_PROFILE, [])
            self.assertEqual(code, 0)
            prepare.assert_called_once()
            opened.assert_called_once()

            with patch.object(plugin.sys, "argv", [str(SCRIPT), "ls"]), \
                    patch.object(plugin, "list_workers", return_value=[]) as listed, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(plugin.main(), 0)
            listed.assert_called_once()

            with patch.object(plugin.sys, "argv", [str(SCRIPT), "inspect"]), \
                    patch.object(plugin, "inspect_action_workspace", return_value=[]) as action_inspect, \
                    patch.object(plugin, "resolve_inspect_worker") as resolve_cli, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(plugin.main(), 0)
            action_inspect.assert_called_once()
            resolve_cli.assert_not_called()


if __name__ == "__main__":
    unittest.main()
