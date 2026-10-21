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
VALID_CLAUDE_AUTH = {
    "claudeAiOauth": {"accessToken": "old", "refreshToken": "r1", "expiresAt": 1},
}
REFRESHED_CLAUDE_AUTH = {
    "claudeAiOauth": {"accessToken": "new", "refreshToken": "r2", "expiresAt": 2},
}

# Profile marker blocks must stay compact; raise if agent-specific surface grows.
MAX_PROFILE_LINES = 55


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
        (self.config / "claude").mkdir()
        (self.config / "claude" / ".credentials.json").write_text(
            json.dumps(VALID_CLAUDE_AUTH) + "\n"
        )
        (self.config / "claude" / ".credentials.json").chmod(0o600)
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
            "HOME": str(root / "user-home"),
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        (root / "user-home" / ".codex").mkdir(parents=True)
        (root / "user-home" / ".claude").mkdir(parents=True)
        (root / "user-home" / ".claude.json").write_text("{}\n")

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
        stderr = io.StringIO()
        with patch.object(plugin.sys, "stderr", stderr):
            self.assertEqual(
                plugin.write_back_auth_files(
                    home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
                ),
                [],
            )
        self.assertIn("unreadable or invalid JSON", stderr.getvalue())
        (session / "auth.json").write_text(json.dumps({"tokens": "bad"}) + "\n")
        stderr = io.StringIO()
        with patch.object(plugin.sys, "stderr", stderr):
            self.assertEqual(
                plugin.write_back_auth_files(
                    home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
                ),
                [],
            )
        self.assertIn("invalid auth JSON shape", stderr.getvalue())
        (session / "auth.json").write_text(json.dumps({"unrelated": True}) + "\n")
        stderr = io.StringIO()
        with patch.object(plugin.sys, "stderr", stderr):
            self.assertEqual(
                plugin.write_back_auth_files(
                    home, session, plugin.DEFAULT_PROFILE, snaps, state_dir=self.state,
                ),
                [],
            )
        self.assertIn("invalid auth JSON shape", stderr.getvalue())
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

    def test_agent_literals_only_inside_profile_blocks_and_image_boilerplate(self):
        source = (ROOT / "bin" / "herdr-sandbox").read_text().splitlines()
        boilerplate = (ROOT / "Containerfile").read_text()
        for agent in ("codex", "claude"):
            begin = end = None
            for index, line in enumerate(source, 1):
                if line.strip() == f"# BEGIN AGENT PROFILE: {agent}":
                    begin = index
                if line.strip() == f"# END AGENT PROFILE: {agent}":
                    end = index
            self.assertIsNotNone(begin, agent)
            self.assertIsNotNone(end, agent)
            self.assertLess(begin, end)
            self.assertLessEqual(end - begin + 1, MAX_PROFILE_LINES, agent)
            offenders = [
                f"{index}:{line}"
                for index, line in enumerate(source, 1)
                if agent in line.lower() and not (begin <= index <= end)
            ]
            self.assertEqual(offenders, [], agent)
            for package in plugin.AGENT_PROFILES[agent].image_npm_packages:
                self.assertIn(package, boilerplate)

    def test_claude_write_back_and_login_env(self):
        home = self.config / "claude"
        session, snaps = plugin.fresh_agent_home(home, "W1", plugin.CLAUDE_PROFILE)
        (session / ".credentials.json").write_text(json.dumps(REFRESHED_CLAUDE_AUTH) + "\n")
        written = plugin.write_back_auth_files(
            home, session, plugin.CLAUDE_PROFILE, snaps, state_dir=self.state,
        )
        self.assertEqual(written, [".credentials.json"])
        stored = json.loads((home / ".credentials.json").read_text())
        self.assertEqual(stored["claudeAiOauth"]["accessToken"], "new")
        self.assertEqual((home / ".credentials.json").stat().st_mode & 0o777, 0o600)

        calls = []

        def fake_run(argv, env=None, check=False):
            calls.append((argv, env))
            return type("R", (), {"returncode": 0})()

        with patch.object(plugin.subprocess, "run", side_effect=fake_run):
            code = plugin.login_agent(plugin.CLAUDE_PROFILE.name)
        self.assertEqual(code, 0)
        argv, env = calls[0]
        self.assertEqual(argv, list(plugin.CLAUDE_PROFILE.login_argv))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(home.resolve()))

    def test_login_refuses_claude_banned_host_paths(self):
        user_home = Path(self.temp.name) / "user-home"
        for banned_rel, home_value in (
            (".claude", "link-claude"),
            (".claude.json", "link-claude-json"),
        ):
            target = (user_home / banned_rel).resolve()
            link = self.config / home_value
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(target)
            (self.config / "config.toml").write_text(
                '[runtime]\nimage = "localhost/herdr-codex-worker:latest"\nnetwork = "online"\n'
                '[resources]\ncpus = 4\nmemory = "8g"\npids = 1024\n'
                '[codex]\nhome = "codex"\n'
                f'[claude]\nhome = "{home_value}"\n'
            )
            with self.assertRaisesRegex(ValueError, r"normal user|banned|escapes plugin config"):
                plugin.resolve_agent_home(self.config, plugin.CLAUDE_PROFILE)

    def test_never_mounts_or_copies_host_agent_homes(self):
        worker = self.state / "workers" / "TEST-001"
        repo = worker / "repo"
        (repo / ".git").mkdir(parents=True)
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "network": "online",
        }))
        host_home = Path(self.temp.name) / "user-home"
        banned_snippets = [
            str((host_home / ".codex").resolve()),
            str((host_home / ".claude").resolve()),
            str((host_home / ".claude.json").resolve()),
            str(host_home.resolve()),
        ]
        for profile in (plugin.CODEX_PROFILE, plugin.CLAUDE_PROFILE):
            home = self.config / profile.home_dirname
            session, _ = plugin.fresh_agent_home(home, "TEST-001", profile)
            for builder in (
                lambda: plugin.open_command(
                    "TEST-001", [profile.interactive_bin], fresh=True,
                    agent_home=session, profile=profile,
                ),
                lambda: plugin.execute_command(
                    "TEST-001", "task", agent_home=session, profile=profile,
                ),
            ):
                argv = builder()
                joined = "\n".join(argv)
                for banned in banned_snippets:
                    self.assertNotIn(banned, joined, profile.name)
                mount_srcs = [
                    arg.split(",src=", 1)[1].split(",", 1)[0]
                    for arg in argv if arg.startswith("type=bind,src=")
                ]
                for src in mount_srcs:
                    self.assertFalse(
                        Path(src).resolve().is_relative_to(host_home.resolve()),
                        f"{profile.name} mounted under host home: {src}",
                    )

    def test_podman_argv_claude_vs_codex_differs_only_in_profile_fields(self):
        """Isolation flags are identical; only profile-owned fields may differ."""
        worker = self.state / "workers" / "TEST-001"
        repo = worker / "repo"
        (repo / ".git").mkdir(parents=True)
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "network": "online",
        }))
        image, _ = plugin.load_config(self.config, plugin.CODEX_PROFILE)
        repo_s = str(repo.resolve())

        def build(profile):
            home = self.config / profile.home_dirname
            session, _ = plugin.fresh_agent_home(home, "TEST-001", profile)
            session_s = str(session.resolve())
            interactive = plugin.open_command(
                "TEST-001", [profile.interactive_bin, "--extra"], fresh=True,
                agent_home=session, profile=profile,
            )
            prompt = plugin.execute_command(
                "TEST-001", "do the task", agent_home=session, profile=profile,
            )

            def normalize(argv):
                out = []
                for arg in argv:
                    out.append(
                        arg.replace(repo_s, "<REPO>")
                        .replace(session_s, "<SESSION>")
                        .replace("herdr-TEST-001", "herdr-<ID>")
                        .replace(image, "<IMAGE>")
                    )
                return out

            return normalize(interactive), normalize(prompt), session_s

        codex_i, codex_p, _ = build(plugin.CODEX_PROFILE)
        claude_i, claude_p, _ = build(plugin.CLAUDE_PROFILE)

        expected_codex_i = [
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
            "codex", "--cd", "/workspace", "--extra",
        ]
        expected_claude_i = [
            "podman", "run", "--rm", "-it", "--name", "herdr-<ID>",
            "--userns=keep-id", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
            "--pids-limit=1024", "--memory=8g", "--cpus=4",
            "--env", "HOME=/tmp/home",
            "--env", "CLAUDE_CONFIG_DIR=/claude",
            "--env", "XDG_CACHE_HOME=/tmp/cache",
            "--mount", "type=bind,src=<REPO>,dst=/workspace,rw",
            "--mount", "type=bind,src=<SESSION>,dst=/claude,rw",
            "--workdir", "/workspace",
            "<IMAGE>",
            "claude", "--dangerously-skip-permissions", "--extra",
        ]
        self.assertEqual(codex_i, expected_codex_i)
        self.assertEqual(claude_i, expected_claude_i)

        expected_codex_p = [
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
        expected_claude_p = [
            "podman", "run", "--rm", "--name", "herdr-<ID>",
            "--userns=keep-id", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
            "--pids-limit=1024", "--memory=8g", "--cpus=4",
            "--env", "HOME=/tmp/home",
            "--env", "CLAUDE_CONFIG_DIR=/claude",
            "--env", "XDG_CACHE_HOME=/tmp/cache",
            "--mount", "type=bind,src=<REPO>,dst=/workspace,rw",
            "--mount", "type=bind,src=<SESSION>,dst=/claude,rw",
            "--workdir", "/workspace",
            "<IMAGE>",
            "claude", "--dangerously-skip-permissions", "-p", "do the task",
        ]
        self.assertEqual(codex_p, expected_codex_p)
        self.assertEqual(claude_p, expected_claude_p)

        def isolation_core(argv):
            """Drop profile-owned env/mount/trailing argv; keep isolation flags."""
            out = []
            i = 0
            while i < len(argv):
                arg = argv[i]
                if arg == "--env" and i + 1 < len(argv):
                    value = argv[i + 1]
                    # Shared isolation env stays; profile home env is profile-owned.
                    if value.startswith("HOME=") or value.startswith("XDG_CACHE_HOME="):
                        out.extend([arg, value])
                    i += 2
                    continue
                if arg == "--mount" and i + 1 < len(argv):
                    mount = argv[i + 1]
                    if ",dst=/workspace," in mount:
                        out.extend([arg, mount])
                    i += 2
                    continue
                if arg == "<IMAGE>":
                    out.append(arg)
                    # Trailing agent argv is entirely profile-owned.
                    break
                out.append(arg)
                i += 1
            return out

        self.assertEqual(isolation_core(codex_i), isolation_core(claude_i))
        self.assertEqual(isolation_core(codex_p), isolation_core(claude_p))

    def test_api_key_opt_in_forwards_only_declared_env(self):
        (self.config / "config.toml").write_text(
            plugin.DEFAULT_CONFIG.replace(
                "[claude]\nhome = \"claude\"\n",
                "[claude]\nhome = \"claude\"\nforward_api_key = true\n",
            )
        )
        # Auth file absent; API key path must satisfy check and launch.
        (self.config / "claude" / ".credentials.json").unlink()
        worker = self.state / "workers" / "TEST-001"
        (worker / "repo" / ".git").mkdir(parents=True)
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "network": "online",
        }))
        secret = "sk-test-key-must-never-appear-on-argv"
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret, "OTHER_SECRET": "nope"}):
            errors = plugin.check(require_auth=True, profile=plugin.CLAUDE_PROFILE)
            self.assertEqual([e for e in errors if "authentication" in e], [])
            home = self.config / "claude"
            session, snaps = plugin.fresh_agent_home(
                home, "TEST-001", plugin.CLAUDE_PROFILE, allow_missing_auth=True,
            )
            self.assertEqual(snaps, {})
            argv = plugin.open_command(
                "TEST-001", ["claude"], fresh=True, agent_home=session,
                profile=plugin.CLAUDE_PROFILE,
            )
        # Inherit form: --env NAME with no value. Secret must not appear anywhere.
        joined = "\0".join(argv)
        self.assertNotIn(secret, joined)
        for arg in argv:
            self.assertNotIn(secret, arg)
        env_pairs = [
            argv[i + 1]
            for i, value in enumerate(argv)
            if value == "--env" and i + 1 < len(argv)
        ]
        self.assertIn("ANTHROPIC_API_KEY", env_pairs)
        self.assertNotIn(f"ANTHROPIC_API_KEY={secret}", env_pairs)
        self.assertNotIn("OTHER_SECRET=nope", env_pairs)
        self.assertNotIn("OTHER_SECRET", env_pairs)
        # Runtime-forced env still present and after the inherit name.
        self.assertIn("HOME=/tmp/home", env_pairs)
        self.assertLess(
            env_pairs.index("ANTHROPIC_API_KEY"),
            env_pairs.index("HOME=/tmp/home"),
        )
        # Without opt-in, host key must not appear (name or value).
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
        (self.config / "claude" / ".credentials.json").write_text(
            json.dumps(VALID_CLAUDE_AUTH) + "\n"
        )
        (self.config / "claude" / ".credentials.json").chmod(0o600)
        session2, _ = plugin.fresh_agent_home(
            self.config / "claude", "TEST-001", plugin.CLAUDE_PROFILE,
        )
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}):
            argv2 = plugin.open_command(
                "TEST-001", ["claude"], fresh=True, agent_home=session2,
                profile=plugin.CLAUDE_PROFILE,
            )
        self.assertNotIn(secret, "\0".join(argv2))
        env_pairs2 = [
            argv2[i + 1]
            for i, value in enumerate(argv2)
            if value == "--env" and i + 1 < len(argv2)
        ]
        self.assertNotIn("ANTHROPIC_API_KEY", env_pairs2)
        self.assertNotIn(f"ANTHROPIC_API_KEY={secret}", env_pairs2)

    def test_container_env_rejects_reserved_and_unsafe_values(self):
        base = dict(
            name="toy",
            home_dirname="toy",
            mount_point="/toy",
            home_env_var="TOY_HOME",
            auth_files=(),
            login_argv=("toy",),
            interactive_bin="toy",
            interactive_prefix=(),
            forbidden_user_args=(),
            herdr_agent="toy",
            normal_user_home=".toy",
            noninteractive_prefix=(),
        )

        class _ToyProfile(plugin.AgentProfile):
            def validate_auth_data(self, data):
                return False

        cases = [
            (("HOME", "/evil"), r"reserved"),
            (("XDG_CACHE_HOME", "/evil"), r"reserved"),
            (("GIT_OPTIONAL_LOCKS", "1"), r"reserved"),
            (("TOY_HOME", "/evil"), r"reserved"),
            (("HERDR_AGENT", "toy"), r"reserved"),
            (("HERDR_PANE_ID", "w1:p1"), r"reserved"),
            (("BAD-KEY", "x"), r"match|container_env key"),
            (("1BAD", "x"), r"match|container_env key"),
            (("OK", "line\nbreak"), r"CR, LF, or NUL"),
            (("OK", "line\rbreak"), r"CR, LF, or NUL"),
            (("OK", "nul\0byte"), r"CR, LF, or NUL"),
        ]
        for container_env, pattern in cases:
            with self.subTest(container_env=container_env):
                with self.assertRaisesRegex(ValueError, pattern):
                    _ToyProfile(**base, container_env=(container_env,))

        with self.assertRaisesRegex(ValueError, r"reserved|api_key_env"):
            _ToyProfile(**base, api_key_env="HOME")
        with self.assertRaisesRegex(ValueError, r"reserved|api_key_env"):
            _ToyProfile(**base, api_key_env="HERDR_SECRET")
        # Valid extras are accepted; runtime still emits forced env last.
        profile = _ToyProfile(**base, container_env=(("TOY_FLAG", "1"),))
        worker = self.state / "workers" / "TEST-001"
        (worker / "repo" / ".git").mkdir(parents=True)
        (worker / "worker.json").write_text(json.dumps({
            "id": "TEST-001", "status": "created", "network": "online",
        }))
        # Use a throwaway session dir under config for mount validation.
        session = self.config / "toy-session"
        session.mkdir()
        runtime = plugin.PodmanSandboxRuntime(
            "TEST-001",
            "localhost/herdr-codex-worker:latest",
            worker / "repo",
            session,
            "online",
            4,
            "8g",
            1024,
            profile,
        )
        argv = runtime._base_command(interactive=False)
        env_pairs = [
            argv[i + 1]
            for i, value in enumerate(argv)
            if value == "--env" and i + 1 < len(argv)
        ]
        self.assertEqual(env_pairs[0], "TOY_FLAG=1")
        self.assertEqual(env_pairs[-3:], [
            "HOME=/tmp/home",
            "TOY_HOME=/toy",
            "XDG_CACHE_HOME=/tmp/cache",
        ])

    def test_claude_auth_requires_oauth_credential_shape(self):
        self.assertTrue(plugin.CLAUDE_PROFILE.validate_auth_data(VALID_CLAUDE_AUTH))
        self.assertFalse(plugin.CLAUDE_PROFILE.validate_auth_data({}))
        self.assertFalse(plugin.CLAUDE_PROFILE.validate_auth_data({"unrelated": True}))
        self.assertFalse(plugin.CLAUDE_PROFILE.validate_auth_data({
            "claudeAiOauth": {"accessToken": "only"},
        }))
        self.assertFalse(plugin.CLAUDE_PROFILE.validate_auth_data({
            "claudeAiOauth": {"refreshToken": "only"},
        }))
        self.assertFalse(plugin.CLAUDE_PROFILE.validate_auth_data({
            "claudeAiOauth": {"accessToken": "", "refreshToken": "r"},
        }))
        self.assertFalse(plugin.CLAUDE_PROFILE.validate_auth_data({
            "apiKey": "sk-not-enough",
        }))


if __name__ == "__main__":
    unittest.main()
