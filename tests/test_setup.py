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
                self.assertTrue((config / "herdr-sandbox").is_file())
                self.assertEqual((config / "herdr-sandbox").stat().st_mode & 0o777, 0o700)

    def test_launcher_binds_paths_and_forwards_argv_without_shell_evaluation(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            plugin_root = root / "plugin root with spaces"
            fake_script = plugin_root / "bin" / "herdr-sandbox"
            fake_script.parent.mkdir(parents=True)
            (plugin_root / "Containerfile").write_text("FROM scratch\n")
            fake_script.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                "print(json.dumps({\"config\": os.environ[\"HERDR_PLUGIN_CONFIG_DIR\"], "
                "\"state\": os.environ[\"HERDR_PLUGIN_STATE_DIR\"], \"argv\": sys.argv[1:]}))\n"
            )
            fake_script.chmod(0o700)
            config = root / "config dir with spaces"
            state = root / "state dir with spaces"
            environment = {
                "HERDR_PLUGIN_CONFIG_DIR": str(config),
                "HERDR_PLUGIN_STATE_DIR": str(state),
                "HERDR_PLUGIN_ROOT": str(plugin_root),
            }
            with patch.dict(os.environ, environment):
                plugin.setup()
                launcher = config / "herdr-sandbox"
                cases = [
                    ["execute", "--id", "WORKER-001", "--prompt", "keep $HOME; do not evaluate 'this'"],
                    ["cherry-pick", "--id", "WORKER-001", "--commit", "a" * 40],
                    ["destroy", "--id", "WORKER-001"],
                ]
                for args in cases:
                    with self.subTest(args=args):
                        result = subprocess.run([str(launcher), *args], capture_output=True, text=True, check=False)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        payload = json.loads(result.stdout)
                        self.assertEqual(payload["config"], str(config.resolve()))
                        self.assertEqual(payload["state"], str(state.resolve()))
                        self.assertEqual(payload["argv"], args)

            self.assertIn("os.execv", launcher.read_text())

    def test_setup_refreshes_launcher_idempotently_without_overwriting_config(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            state = Path(root) / "state"
            environment = {"HERDR_PLUGIN_CONFIG_DIR": str(config), "HERDR_PLUGIN_STATE_DIR": str(state)}
            with patch.dict(os.environ, environment):
                plugin.setup()
                config_file = config / "config.toml"
                config_file.write_text(config_file.read_text() + '\n[destroy]\nid = "CUSTOM"\n')
                expected_config = config_file.read_bytes()
                launcher = config / "herdr-sandbox"
                expected_launcher = launcher.read_bytes()
                plugin.setup()

            self.assertEqual(config_file.read_bytes(), expected_config)
            self.assertEqual(launcher.read_bytes(), expected_launcher)
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o700)

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

    def test_check_reports_missing_resources(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            state = Path(root) / "state"
            with patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": str(config), "HERDR_PLUGIN_STATE_DIR": str(state)}):
                plugin.setup()
                config.joinpath("config.toml").write_text(
                    '[runtime]\nimage = "localhost/herdr-codex-worker:latest"\nnetwork = "online"\n'
                    '[codex]\nhome = "codex"\n'
                )
                with patch.object(plugin, "host_errors", return_value=[]):
                    errors = plugin.check()

            self.assertTrue(any("[resources]" in error for error in errors))

    def test_check_reports_invalid_resources(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            state = Path(root) / "state"
            with patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": str(config), "HERDR_PLUGIN_STATE_DIR": str(state)}):
                plugin.setup()
                config.joinpath("config.toml").write_text(
                    '[runtime]\nimage = "localhost/herdr-codex-worker:latest"\nnetwork = "online"\n'
                    '[resources]\ncpus = 0\nmemory = "8g"\npids = 1024\n'
                    '[codex]\nhome = "codex"\n'
                )
                with patch.object(plugin, "host_errors", return_value=[]):
                    errors = plugin.check()

            self.assertTrue(any("[resources].cpus" in error for error in errors))

    def test_check_reports_invalid_network(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            state = Path(root) / "state"
            with patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": str(config), "HERDR_PLUGIN_STATE_DIR": str(state)}):
                plugin.setup()
                config.joinpath("config.toml").write_text(
                    '[runtime]\nimage = "localhost/herdr-codex-worker:latest"\nnetwork = "host"\n'
                    '[resources]\ncpus = 4\nmemory = "8g"\npids = 1024\n'
                    '[codex]\nhome = "codex"\n'
                )
                with patch.object(plugin, "host_errors", return_value=[]):
                    errors = plugin.check()

            self.assertTrue(any("network" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
