import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_profile", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


VALID_AUTH = {"tokens": {"access_token": "old", "refresh_token": "r1", "id_token": "i1"}}
REFRESHED_AUTH = {"tokens": {"access_token": "new", "refresh_token": "r2", "id_token": "i2"}}


class AgentProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = root / "config"
        self.state = root / "state"
        self.config.mkdir()
        self.state.mkdir()
        (self.config / "codex").mkdir()
        (self.config / "codex" / "auth.json").write_text(json.dumps(VALID_AUTH) + "\n")
        (self.config / "codex" / "auth.json").chmod(0o600)
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
            "HOME": str(root / "user-home"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        (root / "user-home" / ".codex").mkdir(parents=True)

    def test_write_back_on_refresh(self):
        home = self.config / "codex"
        session, snaps = plugin.fresh_agent_home(home, "W1", plugin.DEFAULT_PROFILE)
        (session / "auth.json").write_text(json.dumps(REFRESHED_AUTH) + "\n")
        written = plugin.write_back_auth_files(
            home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
        )
        self.assertEqual(written, ["auth.json"])
        stored = json.loads((home / "auth.json").read_text())
        self.assertEqual(stored["tokens"]["access_token"], "new")
        self.assertEqual((home / "auth.json").stat().st_mode & 0o777, 0o600)

    def test_no_write_back_when_unchanged(self):
        home = self.config / "codex"
        before = (home / "auth.json").read_bytes()
        session, snaps = plugin.fresh_agent_home(home, "W1", plugin.DEFAULT_PROFILE)
        written = plugin.write_back_auth_files(
            home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
        )
        self.assertEqual(written, [])
        self.assertEqual((home / "auth.json").read_bytes(), before)

    def test_no_write_back_when_invalid_json_or_shape(self):
        home = self.config / "codex"
        before = (home / "auth.json").read_bytes()
        session, snaps = plugin.fresh_agent_home(home, "W1", plugin.DEFAULT_PROFILE)
        (session / "auth.json").write_text("not-json\n")
        self.assertEqual(
            plugin.write_back_auth_files(
                home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
            ),
            [],
        )
        (session / "auth.json").write_text(json.dumps({"tokens": "bad"}) + "\n")
        self.assertEqual(
            plugin.write_back_auth_files(
                home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
            ),
            [],
        )
        (session / "auth.json").write_text(json.dumps({"unrelated": True}) + "\n")
        self.assertEqual(
            plugin.write_back_auth_files(
                home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
            ),
            [],
        )
        self.assertEqual((home / "auth.json").read_bytes(), before)

    def test_write_back_refuses_symlinks(self):
        home = self.config / "codex"
        before = (home / "auth.json").read_bytes()
        session, snaps = plugin.fresh_agent_home(home, "W1", plugin.DEFAULT_PROFILE)
        (session / "auth.json").unlink()
        (session / "auth.json").symlink_to(self.temp.name + "/elsewhere.json")
        self.assertEqual(
            plugin.write_back_auth_files(
                home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
            ),
            [],
        )
        self.assertEqual((home / "auth.json").read_bytes(), before)

    def test_concurrent_write_backs_do_not_corrupt_auth(self):
        home = self.config / "codex"
        errors = []

        def worker(token):
            try:
                session, snaps = plugin.fresh_agent_home(home, f"W-{token}", plugin.DEFAULT_PROFILE)
                payload = {
                    "tokens": {
                        "access_token": f"tok-{token}",
                        "refresh_token": "r",
                        "id_token": "i",
                    }
                }
                (session / "auth.json").write_text(json.dumps(payload) + "\n")
                plugin.write_back_auth_files(
                    home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        data = json.loads((home / "auth.json").read_text())
        self.assertTrue(plugin.DEFAULT_PROFILE.validate_auth_data(data))
        self.assertEqual((home / "auth.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((home / "auth.json").is_symlink())

    def test_login_refuses_normal_user_home(self):
        user_codex = (Path(self.temp.name) / "user-home" / ".codex").resolve()
        self.assertEqual(plugin.DEFAULT_PROFILE.normal_user_home_path(), user_codex)

        def forged(config_dir, profile, config=None):
            return user_codex

        with patch.object(plugin, "resolve_agent_home", side_effect=forged):
            with self.assertRaisesRegex(ValueError, "Refusing to run login|normal user"):
                plugin.login_agent(plugin.DEFAULT_PROFILE.name)

        (self.config / "config.toml").write_text(
            '[runtime]\nimage = "localhost/herdr-codex-worker:latest"\nnetwork = "online"\n'
            '[resources]\ncpus = 4\nmemory = "8g"\npids = 1024\n'
            '[codex]\nhome = "../user-home/.codex"\n'
        )
        with self.assertRaisesRegex(ValueError, r"relative path|\.\."):
            plugin.load_config(self.config, plugin.DEFAULT_PROFILE)

    def test_login_sets_home_env_to_plugin_home(self):
        calls = []

        def fake_run(argv, env=None, check=False):
            calls.append((argv, env))
            return type("R", (), {"returncode": 0})()

        with patch.object(plugin.subprocess, "run", side_effect=fake_run):
            code = plugin.login_agent(plugin.DEFAULT_PROFILE.name)
        self.assertEqual(code, 0)
        argv, env = calls[0]
        self.assertEqual(argv, list(plugin.DEFAULT_PROFILE.login_argv))
        self.assertEqual(env[plugin.DEFAULT_PROFILE.home_env_var], str((self.config / "codex").resolve()))
        self.assertNotEqual(env[plugin.DEFAULT_PROFILE.home_env_var], str(plugin.DEFAULT_PROFILE.normal_user_home_path()))

    def test_prompt_and_interactive_share_per_launch_home_policy(self):
        worker = self.state / "workers" / "TEST-001"
        (worker / "repo" / ".git").mkdir(parents=True)
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "network": "online",
        }))
        mounted = []

        def capture_run(*args, **kwargs):
            command = args[0]
            mounts = [command[i + 1] for i, value in enumerate(command) if value == "--mount"]
            codex_mount = next(m for m in mounts if ",dst=/codex," in m)
            src = Path(codex_mount.split(",src=", 1)[1].split(",dst=", 1)[0])
            mounted.append(src)
            self.assertTrue(src.is_relative_to((self.config / "codex" / "sessions").resolve()))
            # Simulate token refresh inside the session home.
            (src / "auth.json").write_text(json.dumps(REFRESHED_AUTH) + "\n")
            return type("R", (), {"returncode": 0})()

        with patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin.subprocess, "run", side_effect=capture_run):
            self.assertEqual(
                plugin.open_worker("TEST-001", ["codex"], fresh=True, profile=plugin.DEFAULT_PROFILE),
                0,
            )
        self.assertEqual(len(mounted), 1)
        self.assertFalse(mounted[0].exists())
        self.assertEqual(
            json.loads((self.config / "codex" / "auth.json").read_text())["tokens"]["access_token"],
            "new",
        )

    def test_herdr_agent_set_on_host_env_not_container_argv(self):
        worker = self.state / "workers" / "TEST-001"
        (worker / "repo" / ".git").mkdir(parents=True)
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "network": "online",
        }))
        seen = {}

        def capture_run(*args, **kwargs):
            seen["argv"] = args[0]
            seen["env"] = kwargs.get("env")
            return type("R", (), {"returncode": 0})()

        with patch.object(plugin, "check", return_value=[]), \
                patch.object(plugin.subprocess, "run", side_effect=capture_run):
            plugin.open_worker("TEST-001", ["codex"], fresh=True, profile=plugin.DEFAULT_PROFILE)
        self.assertIsNotNone(seen["env"])
        self.assertEqual(seen["env"]["HERDR_AGENT"], plugin.DEFAULT_PROFILE.herdr_agent)
        # HERDR_AGENT must not be injected as a container --env (host-only).
        env_pairs = [
            seen["argv"][i + 1]
            for i, value in enumerate(seen["argv"])
            if value == "--env" and i + 1 < len(seen["argv"])
        ]
        self.assertNotIn(f"HERDR_AGENT={plugin.DEFAULT_PROFILE.herdr_agent}", env_pairs)

    def test_no_codex_literal_outside_profile_and_image_boilerplate(self):
        source = (ROOT / "bin" / "herdr-sandbox").read_text().splitlines()
        begin = end = None
        for index, line in enumerate(source, 1):
            if line.strip() == "# BEGIN AGENT PROFILE: codex":
                begin = index
            if line.strip() == "# END AGENT PROFILE: codex":
                end = index
        self.assertIsNotNone(begin)
        self.assertIsNotNone(end)
        self.assertLess(begin, end)
        offenders = []
        for index, line in enumerate(source, 1):
            if "codex" in line and not (begin <= index <= end):
                offenders.append(f"{index}:{line}")
        self.assertEqual(offenders, [])
        boilerplate = (ROOT / "Containerfile").read_text()
        self.assertIn("codex", boilerplate)


if __name__ == "__main__":
    unittest.main()
