import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import tomllib
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_developer_experience", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class DeveloperExperienceTests(unittest.TestCase):
    def test_sole_worker_selection_requires_exactly_one_real_worker_directory(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            workers = state / "workers"
            workers.mkdir(parents=True)
            self.assertIsNone(plugin.sole_worker_id(state))
            (workers / "WORKER-001").mkdir()
            self.assertEqual(plugin.sole_worker_id(state), "WORKER-001")
            (workers / "WORKER-002").mkdir()
            self.assertIsNone(plugin.sole_worker_id(state))

    def test_manifest_exposes_the_low_configuration_entrypoints(self):
        manifest = tomllib.loads((SCRIPT.parent.parent / "herdr-plugin.toml").read_text())
        self.assertIn("bootstrap", [action["id"] for action in manifest["actions"]])
        self.assertNotIn("start-codex", [action["id"] for action in manifest["actions"]])
        start_panes = [pane for pane in manifest["panes"] if pane["id"] == "start-codex"]
        self.assertEqual(len(start_panes), 1)
        self.assertEqual(start_panes[0]["placement"], "overlay")

    def test_manifest_readiness_check_requires_execution_authentication(self):
        manifest = (SCRIPT.parent.parent / "herdr-plugin.toml").read_text()
        self.assertIn('command = ["python3", "bin/herdr-sandbox", "check", "--execution"]', manifest)


if __name__ == "__main__":
    unittest.main()
