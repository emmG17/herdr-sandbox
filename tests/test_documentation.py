from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DocumentationTests(unittest.TestCase):
    def test_root_manifest_packages_the_repository_for_herdr(self):
        manifest_path = ROOT / "herdr-plugin.toml"
        self.assertTrue(manifest_path.is_file())
        manifest = tomllib.loads(manifest_path.read_text())
        self.assertEqual(manifest["id"], "dev.herdr.sandbox")
        self.assertIn("min_herdr_version", manifest)

    def test_readme_covers_public_installation_workflow_and_boundaries(self):
        readme = (ROOT / "README.md").read_text()
        required = (
            "## What it does—and does not do",
            "containers share the host Linux kernel",
            "danger-full-access",
            "## Requirements",
            "podman info --format",
            "## Install",
            "herdr plugin install emmG17/herdr-sandbox",
            "herdr plugin link /path/to/herdr-sandbox",
            "herdr plugin action list --plugin dev.herdr.sandbox",
            "## Quick start",
            "herdr plugin action invoke bootstrap --plugin dev.herdr.sandbox",
            "herdr plugin pane open --plugin dev.herdr.sandbox --entrypoint start-codex --placement overlay",
            "## Herdr 0.9.0 integration limits",
            "plugin action invoke",
            "workspace_cwd",
            "temporary plugin-owned `CODEX_HOME`",
            "FETCH_HEAD",
            "Herdr 0.9.0 plugin actions have fixed command arguments",
            "does not forward arguments after the action ID",
            "SANDBOX_LAUNCHER=\"$(herdr plugin config-dir dev.herdr.sandbox)/herdr-sandbox\"",
            "\"$SANDBOX_LAUNCHER\" execute --id WORKER-001",
            "\"$SANDBOX_LAUNCHER\" cherry-pick --id WORKER-001",
            "\"$SANDBOX_LAUNCHER\" destroy --id WORKER-001",
            "forwards arguments directly, without shell evaluation",
            "## Customize the worker image",
            "network = \"offline\"",
            "## Help, feedback, and changes",
            "https://github.com/emmG17/herdr-sandbox/issues",
            "emmanuel@emm-g.com",
        )
        for item in required:
            with self.subTest(item=item):
                self.assertIn(item, readme)

    def test_security_procedure_tests_disposable_workspace_deletion(self):
        procedure = (ROOT / "docs" / "manual-worker-security-test.md").read_text()
        self.assertIn("rm -rf /workspace", procedure)
        self.assertIn("worker repo is deleted", procedure)


if __name__ == "__main__":
    unittest.main()
