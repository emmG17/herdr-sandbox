import importlib.machinery
import importlib.util
import io
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
T1_AUTH = {"tokens": {"access_token": "T1", "refresh_token": "rT1", "id_token": "iT1"}}
STALE_AUTH = {"tokens": {"access_token": "stale", "refresh_token": "rS", "id_token": "iS"}}

# Profile marker block must stay compact; raise if Codex-specific surface grows.
MAX_CODEX_PROFILE_LINES = 50


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

    def test_stale_session_does_not_overwrite_newer_auth(self):
        """A launches, B writes T1, A's stale write-back must leave T1 in place."""
        home = self.config / "codex"
        session_a, snaps_a = plugin.fresh_agent_home(home, "A", plugin.DEFAULT_PROFILE)
        session_b, snaps_b = plugin.fresh_agent_home(home, "B", plugin.DEFAULT_PROFILE)
        (session_b / "auth.json").write_text(json.dumps(T1_AUTH) + "\n")
        written_b = plugin.write_back_auth_files(
            home, session_b, plugin.DEFAULT_PROFILE, snaps_b, state_dir=self.state,
        )
        self.assertEqual(written_b, ["auth.json"])
        self.assertEqual(json.loads((home / "auth.json").read_text())["tokens"]["access_token"], "T1")

        (session_a / "auth.json").write_text(json.dumps(STALE_AUTH) + "\n")
        stderr = io.StringIO()
        with patch.object(plugin.sys, "stderr", stderr):
            written_a = plugin.write_back_auth_files(
                home, session_a, plugin.DEFAULT_PROFILE, snaps_a, state_dir=self.state,
            )
        self.assertEqual(written_a, [])
        self.assertEqual(json.loads((home / "auth.json").read_text())["tokens"]["access_token"], "T1")
        self.assertIn("already advanced auth", stderr.getvalue())

    def test_mtime_only_change_does_not_block_legitimate_refresh(self):
        """Same dest bytes with a newer mtime must not block a changed session payload."""
        home = self.config / "codex"
        session, snaps = plugin.fresh_agent_home(home, "W1", plugin.DEFAULT_PROFILE)
        auth = home / "auth.json"
        # Touch the dedicated auth file without changing bytes.
        os.utime(auth, (auth.stat().st_atime + 100, auth.stat().st_mtime + 100))
        self.assertEqual(auth.read_bytes(), snaps["auth.json"]["data"])
        (session / "auth.json").write_text(json.dumps(REFRESHED_AUTH) + "\n")
        written = plugin.write_back_auth_files(
            home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
        )
        self.assertEqual(written, ["auth.json"])
        self.assertEqual(json.loads(auth.read_text())["tokens"]["access_token"], "new")

    def test_symlinked_agent_home_is_refused_for_reads_and_write_back(self):
        """Replacing the dedicated home with a symlink must fail reads and write-back."""
        home = self.config / "codex"
        session, snaps = plugin.fresh_agent_home(home, "W1", plugin.DEFAULT_PROFILE)
        (session / "auth.json").write_text(json.dumps(REFRESHED_AUTH) + "\n")

        real_home = self.config / "codex-real"
        home.rename(real_home)
        home.symlink_to(real_home)

        with self.assertRaisesRegex(ValueError, "symlink|Trusted root|authentication is missing"):
            plugin.fresh_agent_home(home, "W2", plugin.DEFAULT_PROFILE)
        with self.assertRaisesRegex(ValueError, "symlink|Trusted root"):
            plugin._read_regular_file_nofollow(home / "auth.json", home)
        # write-back must refuse rather than follow the symlinked home.
        written = plugin.write_back_auth_files(
            home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
        )
        self.assertEqual(written, [])
        self.assertEqual(
            json.loads((real_home / "auth.json").read_text())["tokens"]["access_token"],
            "old",
        )

    def test_locked_write_backs_leave_valid_auth_file(self):
        """Concurrent write-backs under the state lock leave a valid auth.json.

        Sessions are prepared sequentially so only write-back contends on the lock.
        """
        home = self.config / "codex"
        prepared = []
        for token in range(8):
            session, snaps = plugin.fresh_agent_home(home, f"W-{token}", plugin.DEFAULT_PROFILE)
            payload = {
                "tokens": {
                    "access_token": f"tok-{token}",
                    "refresh_token": "r",
                    "id_token": "i",
                }
            }
            (session / "auth.json").write_text(json.dumps(payload) + "\n")
            prepared.append((session, snaps))

        errors = []

        def worker(session, snaps):
            try:
                plugin.write_back_auth_files(
                    home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=item) for item in prepared]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        data = json.loads((home / "auth.json").read_text())
        self.assertTrue(plugin.DEFAULT_PROFILE.validate_auth_data(data))
        self.assertEqual((home / "auth.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((home / "auth.json").is_symlink())

    def test_auth_read_refuses_symlink_swapped_between_check_and_read(self):
        """O_NOFOLLOW must refuse a path swapped to a symlink after an earlier check."""
        home = self.config / "codex"
        auth = home / "auth.json"
        # Caller-side check (the old TOCTOU window): path looks like a regular file.
        self.assertTrue(auth.is_file())
        self.assertFalse(auth.is_symlink())
        # Swap in a symlink before the nofollow read.
        auth.unlink()
        auth.symlink_to("/tmp/evil-auth.json")
        with self.assertRaisesRegex(ValueError, r"(?i)symlink|regular file|Cannot open|not allowed"):
            plugin._read_regular_file_nofollow(auth, home)

        # fresh_agent_home must refuse a symlinked auth path the same way.
        with self.assertRaisesRegex(ValueError, r"(?i)authentication is missing|symlink|Cannot open|not allowed"):
            plugin.fresh_agent_home(home, "W1", plugin.DEFAULT_PROFILE)

        # write-back must refuse a session auth path swapped to a symlink.
        auth.unlink()
        auth.write_text(json.dumps(VALID_AUTH) + "\n")
        auth.chmod(0o600)
        session, snaps = plugin.fresh_agent_home(home, "W1", plugin.DEFAULT_PROFILE)
        session_auth = session / "auth.json"
        self.assertTrue(session_auth.is_file())
        session_auth.unlink()
        session_auth.symlink_to("/tmp/evil-session-auth.json")
        self.assertEqual(
            plugin.write_back_auth_files(
                home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
            ),
            [],
        )

    def test_login_refuses_normal_user_home_via_resolve(self):
        user_codex = (Path(self.temp.name) / "user-home" / ".codex").resolve()
        self.assertEqual(plugin.DEFAULT_PROFILE.normal_user_home_path(), user_codex)

        # Symlink under config that resolves to ~/.codex.
        link = self.config / "link-home"
        link.symlink_to(user_codex)
        (self.config / "config.toml").write_text(
            '[runtime]\nimage = "localhost/herdr-codex-worker:latest"\nnetwork = "online"\n'
            '[resources]\ncpus = 4\nmemory = "8g"\npids = 1024\n'
            '[codex]\nhome = "link-home"\n'
        )
        with self.assertRaisesRegex(ValueError, r"normal user|escapes plugin config"):
            plugin.resolve_agent_home(self.config, plugin.DEFAULT_PROFILE)

        # `..` escape.
        (self.config / "config.toml").write_text(
            '[runtime]\nimage = "localhost/herdr-codex-worker:latest"\nnetwork = "online"\n'
            '[resources]\ncpus = 4\nmemory = "8g"\npids = 1024\n'
            '[codex]\nhome = "../user-home/.codex"\n'
        )
        with self.assertRaisesRegex(ValueError, r"relative path|\.\."):
            plugin.resolve_agent_home(self.config, plugin.DEFAULT_PROFILE)

        # Trailing slash on a `..` escape.
        (self.config / "config.toml").write_text(
            '[runtime]\nimage = "localhost/herdr-codex-worker:latest"\nnetwork = "online"\n'
            '[resources]\ncpus = 4\nmemory = "8g"\npids = 1024\n'
            '[codex]\nhome = "../user-home/.codex/"\n'
        )
        with self.assertRaisesRegex(ValueError, r"relative path|\.\.|normal user|escapes"):
            plugin.resolve_agent_home(self.config, plugin.DEFAULT_PROFILE)

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
        env_pairs = [
            seen["argv"][i + 1]
            for i, value in enumerate(seen["argv"])
            if value == "--env" and i + 1 < len(seen["argv"])
        ]
        self.assertNotIn(f"HERDR_AGENT={plugin.DEFAULT_PROFILE.herdr_agent}", env_pairs)

    def test_glued_short_workspace_flag_is_forbidden(self):
        with self.assertRaisesRegex(ValueError, "worker workspace"):
            plugin.DEFAULT_PROFILE.build_interactive_argv(
                [plugin.DEFAULT_PROFILE.interactive_bin, "-C/workspace"]
            )

    def test_ordered_podman_argv_for_interactive_and_prompt(self):
        worker = self.state / "workers" / "TEST-001"
        repo = worker / "repo"
        (repo / ".git").mkdir(parents=True)
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "network": "online",
        }))
        home = self.config / "codex"
        session, _snaps = plugin.fresh_agent_home(home, "TEST-001", plugin.DEFAULT_PROFILE)
        image, _ = plugin.load_config(self.config, plugin.DEFAULT_PROFILE)
        repo_s = str(repo.resolve())
        session_s = str(session.resolve())

        def normalize(argv):
            out = []
            for arg in argv:
                out.append(
                    arg.replace(repo_s, "<REPO>")
                    .replace(session_s, "<SESSION>")
                    .replace(f"herdr-TEST-001", "herdr-<ID>")
                    .replace(image, "<IMAGE>")
                )
            return out

        interactive = plugin.open_command(
            "TEST-001",
            [plugin.DEFAULT_PROFILE.interactive_bin, "--model", "gpt-5"],
            fresh=True,
            agent_home=session,
            profile=plugin.DEFAULT_PROFILE,
        )
        expected_interactive = [
            "podman", "run", "--rm", "-it", "--name", "herdr-<ID>",
            "--userns=keep-id", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
            "--pids-limit=1024", "--memory=8g", "--cpus=4",
            "--env", "HOME=/tmp/home",
            "--env", "CODEX_HOME=/codex",
            "--env", "XDG_CACHE_HOME=/tmp/cache",
            "--mount", "type=bind,src=<REPO>,dst=/workspace,rw",
            "--mount", "type=bind,src=<SESSION>,dst=/codex,rw",
            "--workdir", "/workspace",
            "<IMAGE>",
            "codex", "--cd", "/workspace", "--model", "gpt-5",
        ]
        self.assertEqual(normalize(interactive), expected_interactive)

        prompt = plugin.execute_command(
            "TEST-001",
            "do the task",
            agent_home=session,
            profile=plugin.DEFAULT_PROFILE,
        )
        expected_prompt = [
            "podman", "run", "--rm", "--name", "herdr-<ID>",
            "--userns=keep-id", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
            "--pids-limit=1024", "--memory=8g", "--cpus=4",
            "--env", "HOME=/tmp/home",
            "--env", "CODEX_HOME=/codex",
            "--env", "XDG_CACHE_HOME=/tmp/cache",
            "--mount", "type=bind,src=<REPO>,dst=/workspace,rw",
            "--mount", "type=bind,src=<SESSION>,dst=/codex,rw",
            "--workdir", "/workspace",
            "<IMAGE>",
            "codex", "exec", "--sandbox", "danger-full-access", "do the task",
        ]
        self.assertEqual(normalize(prompt), expected_prompt)
        # HERDR_AGENT is host env, not argv.
        self.assertNotIn("HERDR_AGENT=codex", interactive)
        self.assertNotIn("HERDR_AGENT=codex", prompt)

    def test_execute_command_requires_explicit_agent_home(self):
        with self.assertRaisesRegex(ValueError, "explicit per-launch agent_home"):
            plugin.execute_command("TEST-001", "task", agent_home=None)

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
        self.assertLessEqual(end - begin + 1, MAX_CODEX_PROFILE_LINES)
        offenders = []
        for index, line in enumerate(source, 1):
            if "codex" in line.lower() and not (begin <= index <= end):
                # Allow the marker lines themselves only inside the block.
                offenders.append(f"{index}:{line}")
        self.assertEqual(offenders, [])
        boilerplate = (ROOT / "Containerfile").read_text()
        self.assertIn("codex", boilerplate.lower())


if __name__ == "__main__":
    unittest.main()
