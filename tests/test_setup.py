import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class SetupTests(unittest.TestCase):
    def test_setup_uses_only_plugin_directories(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            state = Path(root) / "state"
            with patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": str(config), "HERDR_PLUGIN_STATE_DIR": str(state)}):
                plugin.setup()
                self.assertTrue((config / "config.toml").is_file())
                self.assertTrue((config / "codex").is_dir())
                self.assertTrue(state.is_dir())
                self.assertTrue((config / "codex").resolve().is_relative_to(config.resolve()))

    def test_rejects_codex_home_outside_plugin_config(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            config.mkdir()
            (config / "config.toml").write_text('[runtime]\nimage = "test"\n[codex]\nhome = "../outside"\n')
            with self.assertRaisesRegex(ValueError, "relative path"):
                plugin.load_config(config)

    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            config.mkdir()
            (config / "codex").symlink_to(Path(root) / "outside")
            (config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
            with self.assertRaisesRegex(ValueError, "escapes plugin config"):
                plugin.load_config(config)

    def test_rejects_config_directory_as_codex_home(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root)
            (config / "config.toml").write_text('[runtime]\nimage = "test"\n[codex]\nhome = "."\n')
            with self.assertRaisesRegex(ValueError, "relative path"):
                plugin.load_config(config)

    def test_rejects_non_table_config_sections(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root)
            (config / "config.toml").write_text('runtime = "bad"\n[codex]\nhome = "codex"\n')
            with self.assertRaisesRegex(ValueError, "tables"):
                plugin.load_config(config)

    def test_uses_herdr_workspace_context_for_source(self):
        with patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": '{"workspace_cwd":"/tmp/source"}'}):
            self.assertEqual(plugin.context_repo(), "/tmp/source")

    def test_check_reports_missing_image_and_codex_auth(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            state = Path(root) / "state"
            with patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": str(config), "HERDR_PLUGIN_STATE_DIR": str(state)}):
                plugin.setup()
                with patch.object(plugin, "host_errors", return_value=[]), patch.object(plugin.shutil, "which", return_value="/usr/bin/podman"), patch.object(plugin.platform, "system", return_value="Linux"), patch.object(plugin, "run", return_value=plugin.subprocess.CompletedProcess([], 1, "", "")):
                    errors = plugin.check(require_auth=True)
            self.assertTrue(any("authentication" in error for error in errors))
            self.assertTrue(any("Worker image" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
