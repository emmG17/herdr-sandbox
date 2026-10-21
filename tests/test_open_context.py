"""validate_open_context: pane-run env vs mismatched workspace_cwd."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.helpers import git


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_open_context", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class OpenContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "source"
        self.source.mkdir()
        git("init", "-q", self.source)
        git("-C", self.source, "config", "user.name", "Test")
        git("-C", self.source, "config", "user.email", "test@example.invalid")
        (self.source / "f.txt").write_text("f\n")
        git("-C", self.source, "add", "f.txt")
        git("-C", self.source, "commit", "-qm", "init")
        self.other = root / "other"
        self.other.mkdir()
        git("init", "-q", self.other)
        git("-C", self.other, "config", "user.name", "Test")
        git("-C", self.other, "config", "user.email", "test@example.invalid")
        (self.other / "o.txt").write_text("o\n")
        git("-C", self.other, "add", "o.txt")
        git("-C", self.other, "commit", "-qm", "other")
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
        with patch.object(plugin, "check", return_value=[]):
            worker = plugin.create_worker("TEST-001", str(self.source))
        self.worker = worker

    def test_pane_run_env_without_context_json_is_accepted(self):
        """herdr pane run sets HERDR_ENV=1 + HERDR_PANE_ID, no context JSON."""
        with patch.dict(os.environ, {
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p1",
        }, clear=False):
            os.environ.pop("HERDR_PLUGIN_CONTEXT_JSON", None)
            plugin.validate_open_context("TEST-001")

    def test_herdr_env_without_pane_or_workspace_still_refused(self):
        with patch.dict(os.environ, {"HERDR_ENV": "1"}, clear=False):
            os.environ.pop("HERDR_PANE_ID", None)
            os.environ.pop("HERDR_PLUGIN_CONTEXT_JSON", None)
            with self.assertRaisesRegex(ValueError, "workspace context is missing"):
                plugin.validate_open_context("TEST-001")

    def test_mismatched_workspace_cwd_still_refused(self):
        with patch.dict(os.environ, {
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({
                "workspace_cwd": str(self.other.resolve()),
            }),
        }, clear=False):
            with self.assertRaisesRegex(ValueError, "mismatched workspace"):
                plugin.validate_open_context("TEST-001")

    def test_hs_agent_forwards_agent_args_to_open_worker(self):
        with patch.object(plugin, "prepare_hs_worker", return_value=("TEST-001", self.state)), \
                patch.object(plugin, "open_worker", return_value=0) as open_worker, \
                patch.object(plugin, "fetch_worker", return_value={
                    "commit": "a" * 40,
                    "branch": "sandbox/TEST-001",
                    "commit_count": 0,
                    "log_oneline": "",
                    "changed_files": [],
                    "diff_stat": "",
                    "base_commit": "b" * 40,
                    "source_repo": str(self.source),
                    "worker_id": "TEST-001",
                }), \
                patch.object(plugin, "format_fetch_result", return_value="ok"):
            self.assertEqual(
                plugin.hs_agent(plugin.DEFAULT_PROFILE, ["--model", "gpt-5"]),
                0,
            )
        open_worker.assert_called_once()
        args, kwargs = open_worker.call_args
        self.assertEqual(args[0], "TEST-001")
        self.assertEqual(args[1], ["codex", "--model", "gpt-5"])
        self.assertTrue(kwargs.get("fresh"))
        self.assertIs(kwargs.get("profile"), plugin.DEFAULT_PROFILE)


if __name__ == "__main__":
    unittest.main()
