import importlib.machinery
import importlib.util
from pathlib import Path
import tomllib
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_developer_experience", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class DeveloperExperienceTests(unittest.TestCase):
    def test_manifest_exposes_bootstrap_check_and_hs_panes(self):
        manifest = tomllib.loads((SCRIPT.parent.parent / "herdr-plugin.toml").read_text())
        action_ids = [action["id"] for action in manifest["actions"]]
        self.assertIn("bootstrap", action_ids)
        self.assertIn("check", action_ids)
        for removed in ("create", "open", "execute", "fetch", "rm"):
            self.assertNotIn(removed, action_ids)
        self.assertNotIn("start-codex", action_ids)

        panes = {pane["id"]: pane for pane in manifest["panes"]}
        self.assertEqual(panes["start-codex"]["command"], ["python3", "bin/hs", "codex"])
        self.assertEqual(panes["start-codex"]["placement"], "overlay")
        self.assertEqual(panes["start-claude"]["command"], ["python3", "bin/hs", "claude"])

        action_ids_set = set(action_ids)
        self.assertNotIn("destroy", action_ids_set)
        for action_id, hs_args in (
            ("list", ["ls"]),
            ("inspect", ["inspect"]),
            ("gc", ["gc"]),
        ):
            action = next(a for a in manifest["actions"] if a["id"] == action_id)
            self.assertEqual(action["command"], ["python3", "bin/hs", *hs_args])

        self.assertNotIn("sole_worker_id", dir(plugin))

    def test_manifest_readiness_check_requires_execution_authentication(self):
        manifest = (SCRIPT.parent.parent / "herdr-plugin.toml").read_text()
        self.assertIn('command = ["python3", "bin/herdr-sandbox", "check", "--execution"]', manifest)


if __name__ == "__main__":
    unittest.main()
