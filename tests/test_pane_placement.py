import importlib.machinery
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_pane_placement", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class PanePlacementValidationTests(unittest.TestCase):
    def test_overlay_is_default_and_targets_active_pane(self):
        placement, target, direction = plugin.resolve_pane_placement(None, None, None)
        self.assertEqual(placement, "overlay")
        self.assertIsNone(target)
        self.assertIsNone(direction)

    def test_overlay_rejects_target_pane(self):
        with self.assertRaisesRegex(ValueError, "overlay placement does not accept --target-pane"):
            plugin.resolve_pane_placement("overlay", "w1:p1", None)

    def test_overlay_rejects_direction(self):
        with self.assertRaisesRegex(ValueError, "overlay placement does not accept --direction"):
            plugin.resolve_pane_placement("overlay", None, "right")

    def test_split_requires_target_pane(self):
        with self.assertRaisesRegex(ValueError, "split placement requires a target pane"):
            plugin.resolve_pane_placement("split", None, "right")

    def test_split_requires_direction(self):
        with self.assertRaisesRegex(ValueError, "split placement requires a direction"):
            plugin.resolve_pane_placement("split", "w1:p1", None)

    def test_split_requires_supported_direction(self):
        with self.assertRaisesRegex(ValueError, "Unsupported split direction"):
            plugin.resolve_pane_placement("split", "w1:p1", "up")

    def test_split_accepts_right_and_down(self):
        for direction in ("right", "down"):
            with self.subTest(direction=direction):
                placement, target, result_direction = plugin.resolve_pane_placement("split", "w1:p1", direction)
                self.assertEqual(placement, "split")
                self.assertEqual(target, "w1:p1")
                self.assertEqual(result_direction, direction)

    def test_tab_rejects_target_pane(self):
        with self.assertRaisesRegex(ValueError, "tab placement does not accept --target-pane"):
            plugin.resolve_pane_placement("tab", "w1:p1", None)

    def test_tab_rejects_direction(self):
        with self.assertRaisesRegex(ValueError, "tab placement does not accept --direction"):
            plugin.resolve_pane_placement("tab", None, "right")

    def test_unsupported_placement_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported placement.*popup"):
            plugin.resolve_pane_placement("popup", None, None)


class PaneOpenCommandTests(unittest.TestCase):
    def test_overlay_command_uses_active_pane(self):
        with patch.dict(os.environ, {"HERDR_BIN_PATH": "herdr"}, clear=False):
            command = plugin.build_herdr_pane_open_command("overlay", None, None)
        self.assertEqual(command, [
            "herdr", "plugin", "pane", "open",
            "--plugin", plugin.PLUGIN_ID,
            "--entrypoint", "start-codex",
            "--placement", "overlay",
        ])

    def test_split_command_includes_target_pane_and_direction(self):
        command = plugin.build_herdr_pane_open_command("split", "w1:p1", "right")
        self.assertEqual(command, [
            os.environ.get("HERDR_BIN_PATH", "herdr"), "plugin", "pane", "open",
            "--plugin", plugin.PLUGIN_ID,
            "--entrypoint", "start-codex",
            "--placement", "split",
            "--target-pane", "w1:p1",
            "--direction", "right",
        ])

    def test_tab_command_opens_new_tab(self):
        command = plugin.build_herdr_pane_open_command("tab", None, None)
        self.assertEqual(command, [
            os.environ.get("HERDR_BIN_PATH", "herdr"), "plugin", "pane", "open",
            "--plugin", plugin.PLUGIN_ID,
            "--entrypoint", "start-codex",
            "--placement", "tab",
        ])

    def test_herdr_bin_path_default_falls_back_to_herdr(self):
        with patch.dict(os.environ, {}, clear=True):
            command = plugin.build_herdr_pane_open_command("overlay", None, None)
        self.assertEqual(command[0], "herdr")


class PaneOpenCliTests(unittest.TestCase):
    def test_cli_rejects_invalid_placement(self):
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "pane-open", "--placement", "popup"]):
            self.assertEqual(plugin.main(), 1)

    def test_cli_rejects_split_without_target(self):
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "pane-open", "--placement", "split", "--direction", "right"]):
            self.assertEqual(plugin.main(), 1)

    def test_cli_rejects_split_without_direction(self):
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "pane-open", "--placement", "split", "--target-pane", "w1:p1"]):
            self.assertEqual(plugin.main(), 1)

    def test_cli_rejects_overlay_with_target(self):
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "pane-open", "--placement", "overlay", "--target-pane", "w1:p1"]):
            self.assertEqual(plugin.main(), 1)

    def test_cli_builds_command_for_valid_split(self):
        called = []
        def fake_run(command, check=False):
            called.append(command)
            return Mock(returncode=0)
        with patch.object(plugin.subprocess, "run", side_effect=fake_run), \
                patch.object(plugin.sys, "argv", [str(SCRIPT), "pane-open", "--placement", "split", "--target-pane", "w1:p1", "--direction", "down"]):
            self.assertEqual(plugin.main(), 0)
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0], plugin.build_herdr_pane_open_command("split", "w1:p1", "down"))


if __name__ == "__main__":
    unittest.main()
