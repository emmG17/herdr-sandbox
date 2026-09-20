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

    def test_readme_covers_the_install_and_safe_worker_workflow(self):
        readme = (ROOT / "README.md").read_text()
        required = (
            "## Threat model and isolation",
            "containers share the host Linux kernel",
            "danger-full-access",
            "## Arch Linux and rootless Podman",
            "podman info --format",
            "## Install or link the plugin",
            "herdr plugin install OWNER/herdr-sandbox",
            "herdr plugin link /path/to/herdr-sandbox",
            "herdr plugin list --plugin dev.herdr.sandbox",
            "herdr plugin action list --plugin dev.herdr.sandbox",
            "## Online and offline profiles",
            "network = \"offline\"",
            "## End-to-end worker workflow",
            "## Integrate a reviewed change",
            "## Destroy a worker",
            "## Security test",
            "## Troubleshooting",
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
