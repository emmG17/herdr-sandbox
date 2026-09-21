import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_image_boilerplate", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class ImageBoilerplateTests(unittest.TestCase):
    def test_setup_creates_an_editable_image_boilerplate(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            state = Path(root) / "state"
            with patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": str(config), "HERDR_PLUGIN_STATE_DIR": str(state)}):
                plugin.setup()
            boilerplate = config / "image" / "Containerfile"
            self.assertTrue(boilerplate.is_file())
            self.assertIn("ARG BASE_IMAGE", boilerplate.read_text())

    def test_build_uses_configured_base_and_plugin_owned_boilerplate(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            state = Path(root) / "state"
            with patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": str(config), "HERDR_PLUGIN_STATE_DIR": str(state)}):
                plugin.setup()
                (config / "config.toml").write_text(plugin.DEFAULT_CONFIG.replace(
                    'base_image = "node:22-bookworm-slim"', 'base_image = "python:3.12-slim-bookworm"'
                ))
                with patch.object(plugin, "host_errors", return_value=[]), patch.object(plugin.subprocess, "run") as run:
                    run.return_value.returncode = 0
                    plugin.build_image()
            command = run.call_args.args[0]
            self.assertIn("BASE_IMAGE=python:3.12-slim-bookworm", command)
            self.assertEqual(command[-1], str(config / "image"))


if __name__ == "__main__":
    unittest.main()
