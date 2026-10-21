import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from tests.helpers import git


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "herdr-sandbox"
loader = importlib.machinery.SourceFileLoader("herdr_sandbox_fetch", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


def source_refs(source):
    """Return sorted refname\\tsha lines from for-each-ref (heads only)."""
    output = git("-C", source, "for-each-ref", "--format=%(refname)%09%(objectname)", "refs/heads")
    return [line for line in output.splitlines() if line]


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.root / "config"
        self.config.mkdir()
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG)
        git("init", "-q", self.source)
        git("-C", self.source, "config", "user.name", "Test")
        git("-C", self.source, "config", "user.email", "test@example.invalid")
        (self.source / "value.txt").write_text("base\n")
        git("-C", self.source, "add", "value.txt")
        git("-C", self.source, "commit", "-qm", "initial")
        self.source_head = git("-C", self.source, "rev-parse", "HEAD")
        # Default branch name varies by git version; keep a stable main for review commands.
        current = git("-C", self.source, "branch", "--show-current")
        if current and current != "main":
            git("-C", self.source, "branch", "-m", current, "main")
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        ready = patch.object(plugin, "check", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)
        # Avoid Podman in unit tests when probing dirty workers.
        snapshot = patch.object(plugin, "_query_worker_git_snapshot", return_value={
            "status_porcelain": "",
            "log": "",
            "status_short": "",
            "diff": "",
            "error": None,
        })
        snapshot.start()
        self.addCleanup(snapshot.stop)

    def create_committed_worker(self, worker_id="TEST-001", contents="worker change\n"):
        worker = plugin.create_worker(worker_id, str(self.source))
        repo = worker / "repo"
        git("-C", repo, "config", "user.name", "Worker")
        git("-C", repo, "config", "user.email", "worker@example.invalid")
        (repo / "value.txt").write_text(contents)
        (repo / "added.txt").write_text("added\n")
        git("-C", repo, "add", "value.txt", "added.txt")
        git("-C", repo, "commit", "-qm", "worker change")
        return worker, git("-C", repo, "rev-parse", "HEAD")

    def test_fetches_into_sandbox_ref_without_changing_other_refs(self):
        worker, worker_head = self.create_committed_worker()
        refs_before = source_refs(self.source)
        branch_before = git("-C", self.source, "branch", "--show-current")
        status_before = git("-C", self.source, "status", "--porcelain")

        result = plugin.fetch_worker("TEST-001")

        self.assertEqual(result["commit"], worker_head)
        self.assertEqual(result["branch"], "sandbox/TEST-001")
        self.assertEqual(result["commit_count"], 1)
        self.assertEqual(
            git("-C", self.source, "rev-parse", "refs/heads/sandbox/TEST-001"),
            worker_head,
        )
        self.assertIn("worker change", result["log_oneline"])
        self.assertEqual(result["changed_files"], ["added.txt", "value.txt"])
        self.assertIn("added.txt", result["diff_stat"])
        self.assertEqual(git("-C", self.source, "rev-parse", "HEAD"), self.source_head)
        self.assertEqual(git("-C", self.source, "branch", "--show-current"), branch_before)
        self.assertEqual(git("-C", self.source, "status", "--porcelain"), status_before)

        refs_after = source_refs(self.source)
        added = set(refs_after) - set(refs_before)
        removed = set(refs_before) - set(refs_after)
        self.assertEqual(removed, set())
        self.assertEqual(added, {f"refs/heads/sandbox/TEST-001\t{worker_head}"})

        # FETCH_HEAD must not be required; --no-write-fetch-head is intentional.
        fetch_head = self.source / ".git" / "FETCH_HEAD"
        if fetch_head.exists():
            self.assertNotIn(worker_head, fetch_head.read_text())

        review = plugin.format_fetch_result(result)
        self.assertIn("Branch: sandbox/TEST-001", review)
        self.assertIn("Commits: 1", review)
        quoted_source = shlex.quote(str(self.source))
        self.assertIn(f"git -C {quoted_source} log ", review)
        self.assertIn("..sandbox/TEST-001", review)
        self.assertIn(f"git -C {quoted_source} diff ", review)
        self.assertIn(f"git -C {quoted_source} merge sandbox/TEST-001", review)

        with patch.object(plugin, "remove_worker_container"):
            self.assertTrue(plugin.destroy_worker("TEST-001"))
        self.assertFalse(worker.exists())
        self.assertEqual(
            git("-C", self.source, "rev-parse", "refs/heads/sandbox/TEST-001"),
            worker_head,
        )
        self.assertEqual(
            git("-C", self.source, "show", "--format=%s", "--no-patch", "sandbox/TEST-001"),
            "worker change",
        )
        self.assertEqual(
            git("-C", self.source, "rev-list", "--count", "main..sandbox/TEST-001"),
            "1",
        )

    def test_refetch_force_updates_only_sandbox_ref(self):
        worker, first_head = self.create_committed_worker()
        plugin.fetch_worker("TEST-001")
        refs_mid = source_refs(self.source)

        repo = worker / "repo"
        (repo / "value.txt").write_text("second change\n")
        git("-C", repo, "commit", "-qam", "second change")
        second_head = git("-C", repo, "rev-parse", "HEAD")

        result = plugin.fetch_worker("TEST-001")
        self.assertEqual(result["commit"], second_head)
        self.assertEqual(result["commit_count"], 2)
        self.assertEqual(
            git("-C", self.source, "rev-parse", "refs/heads/sandbox/TEST-001"),
            second_head,
        )

        refs_after = source_refs(self.source)
        changed = set(refs_after) - set(refs_mid)
        removed = set(refs_mid) - set(refs_after)
        # Old sandbox tip line removed; new tip line added; nothing else.
        self.assertEqual(removed, {f"refs/heads/sandbox/TEST-001\t{first_head}"})
        self.assertEqual(changed, {f"refs/heads/sandbox/TEST-001\t{second_head}"})

    def test_checked_out_sandbox_branch_fails_cleanly(self):
        _, worker_head = self.create_committed_worker()
        plugin.fetch_worker("TEST-001")
        git("-C", self.source, "switch", "-q", "sandbox/TEST-001")
        # Advance worker so a successful fetch would move the tip.
        worker_repo = self.state / "workers" / "TEST-001" / "repo"
        (worker_repo / "value.txt").write_text("after checkout\n")
        git("-C", worker_repo, "commit", "-qam", "after checkout")
        new_head = git("-C", worker_repo, "rev-parse", "HEAD")

        with self.assertRaises(ValueError) as raised:
            plugin.fetch_worker("TEST-001")
        message = str(raised.exception)
        self.assertIn("sandbox/TEST-001 is checked out", message)
        self.assertIn("hs fetch TEST-001", message)

        self.assertEqual(
            git("-C", self.source, "rev-parse", "refs/heads/sandbox/TEST-001"),
            worker_head,
        )
        self.assertNotEqual(worker_head, new_head)
        with (self.state / "workers" / "TEST-001" / "worker.json").open() as stream:
            metadata = json.load(stream)
        self.assertEqual(metadata["fetched_commit"], worker_head)

    def test_zero_commits_still_creates_sandbox_ref(self):
        plugin.create_worker("EMPTY-001", str(self.source))
        result = plugin.fetch_worker("EMPTY-001")
        self.assertEqual(result["commit_count"], 0)
        self.assertEqual(result["commit"], self.source_head)
        self.assertEqual(
            git("-C", self.source, "rev-parse", "refs/heads/sandbox/EMPTY-001"),
            self.source_head,
        )
        review = plugin.format_fetch_result(result)
        self.assertIn("no commits beyond base", review)

    def test_dirty_worker_prints_warning(self):
        self.create_committed_worker()
        with patch.object(plugin, "_query_worker_git_snapshot", return_value={
            "status_porcelain": " M value.txt",
            "log": "",
            "status_short": " M value.txt",
            "diff": "",
            "error": None,
        }):
            result = plugin.fetch_worker("TEST-001")
        self.assertIn("uncommitted changes", result["dirty_warning"])
        self.assertIn("uncommitted changes", plugin.format_fetch_result(result))

    def test_counts_and_diffs_use_only_source_repo(self):
        self.create_committed_worker()
        observed = []
        real_run = subprocess.run

        def tracking_run(command, **kwargs):
            command = list(command)
            if command and command[0] == "git":
                observed.append(command)
            return real_run(command, **kwargs)

        with patch.object(plugin.subprocess, "run", side_effect=tracking_run):
            result = plugin.fetch_worker("TEST-001")

        worker_repo = str((self.state / "workers" / "TEST-001" / "repo").resolve())
        source = str(self.source.resolve())
        for command in observed:
            joined = " ".join(command)
            if "-C" in command:
                index = command.index("-C")
                cwd_target = str(Path(command[index + 1]).resolve())
                self.assertNotEqual(cwd_target, worker_repo, joined)
                if any(token in command for token in ("rev-list", "diff", "log", "rev-parse")):
                    # Diff/count/log against the fetched ref must be source-only.
                    if "bundle" not in command and "index-pack" not in command:
                        self.assertEqual(cwd_target, source, joined)
            self.assertNotIn(worker_repo, command, joined)

        self.assertEqual(result["commit_count"], 1)
        # Explicit acceptance shape: rev-list count in the source against sandbox ref.
        self.assertEqual(
            git("-C", self.source, "rev-list", "--count", f"{self.source_head}..refs/heads/sandbox/TEST-001"),
            "1",
        )

    def test_rejects_non_sandbox_destination(self):
        with self.assertRaises(ValueError):
            plugin.validate_sandbox_result_branch("main")
        with self.assertRaises(ValueError):
            plugin.validate_sandbox_result_branch("sandbox/*")
        with self.assertRaises(ValueError):
            plugin.validate_sandbox_result_branch("sandbox/../evil")
        self.assertEqual(plugin.validate_sandbox_result_branch("sandbox/ok-1"), "sandbox/ok-1")

    def test_fetch_action_accepts_positional_id_and_reports_review_details(self):
        result = {
            "commit": "a" * 40,
            "branch": "sandbox/configured",
            "commit_count": 2,
            "log_oneline": "aaaa worker",
            "changed_files": ["one.py"],
            "diff_stat": " one.py | 1 +\n",
            "base_commit": "b" * 40,
            "source_repo": str(self.source),
            "worker_id": "configured",
        }
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "fetch", "configured"]), \
                patch.object(plugin, "fetch_worker", return_value=result) as fetch, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(plugin.main(), 0)
        fetch.assert_called_once_with("configured")
        text = output.getvalue()
        self.assertIn("Branch: sandbox/configured", text)
        self.assertIn("Commits: 2", text)
        self.assertIn("Commit: " + "a" * 40, text)
        self.assertIn("Changed files:\none.py", text)
        quoted_source = shlex.quote(str(self.source))
        self.assertIn(f"git -C {quoted_source} log ", text)

    def test_legacy_fetch_table_is_ignored_with_single_notice(self):
        (self.config / "config.toml").write_text(plugin.DEFAULT_CONFIG + '\n[fetch]\nid = "configured"\n')
        stderr = io.StringIO()
        with patch.object(plugin.sys, "argv", [str(SCRIPT), "list"]), \
                patch.object(plugin.sys, "stderr", stderr):
            self.assertEqual(plugin.main(), 0)
        message = stderr.getvalue()
        self.assertEqual(message.count("legacy per-action config tables"), 1)
        self.assertIn("[fetch]", message)
        self.assertNotIn("fetch_options", dir(plugin))

    def test_fetch_without_id_resolves_pane_worker(self):
        with patch.dict(os.environ, {
            "HERDR_PANE_ID": "w1:p9",
            "HERDR_WORKSPACE_ID": "w1",
            "HERDR_SOCKET_PATH": "/tmp/herdr.sock",
        }):
            worker, worker_head = self.create_committed_worker("PANE-001")
        result = {
            "commit": worker_head,
            "branch": "sandbox/PANE-001",
            "commit_count": 1,
            "log_oneline": "",
            "changed_files": [],
            "diff_stat": "",
            "base_commit": "b" * 40,
            "source_repo": str(self.source),
            "worker_id": "PANE-001",
        }
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p9"}), \
                contextlib.chdir(self.source), \
                patch.object(plugin.sys, "argv", [str(SCRIPT), "fetch"]), \
                patch.object(plugin, "fetch_worker", return_value=result) as fetch, \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(plugin.main(), 0)
        fetch.assert_called_once_with("PANE-001")

    def test_hs_codex_fetches_after_agent_exit(self):
        worker = plugin.create_worker("AUTO-001", str(self.source))
        repo = worker / "repo"
        git("-C", repo, "config", "user.name", "Worker")
        git("-C", repo, "config", "user.email", "worker@example.invalid")
        (repo / "auto.txt").write_text("auto\n")
        git("-C", repo, "add", "auto.txt")
        git("-C", repo, "commit", "-qm", "auto")
        worker_head = git("-C", repo, "rev-parse", "HEAD")

        with patch.object(plugin, "check_root"), \
                patch.object(plugin, "resolve_source_repo_from_cwd", return_value=self.source), \
                patch.object(plugin, "check_dirty_tree", return_value=False), \
                patch.object(plugin, "ensure_image_exists"), \
                patch.object(plugin, "find_reusable_worker", return_value=("AUTO-001", None)), \
                patch.object(plugin, "open_worker", return_value=0) as opened, \
                patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = plugin.hs_agent(plugin.DEFAULT_PROFILE, [])
        self.assertEqual(code, 0)
        opened.assert_called_once()
        self.assertEqual(
            git("-C", self.source, "rev-parse", "refs/heads/sandbox/AUTO-001"),
            worker_head,
        )
        self.assertIn("Branch: sandbox/AUTO-001", stdout.getvalue())

    def test_hs_agent_keeps_herdr_agent_until_after_fetch(self):
        """Interactive path must not clear HERDR_AGENT before auto-fetch.

        open_worker restores the HERDR_AGENT value it saw on entry. If hs_agent
        clears identity before fetch, herdr agent wait --until done can return
        before refs/heads/sandbox/<id> exists.
        """
        plugin.create_worker("AUTO-002", str(self.source))
        seen = {"during_open": None, "during_fetch": None}
        os.environ.pop("HERDR_AGENT", None)

        def fake_open(worker_id, command=None, fresh=False, profile=None):
            seen["during_open"] = os.environ.get("HERDR_AGENT")
            # Match open_worker's finally: restore whatever was set on entry.
            previous = os.environ.get("HERDR_AGENT")
            os.environ["HERDR_AGENT"] = plugin.DEFAULT_PROFILE.herdr_agent
            if previous is None:
                os.environ.pop("HERDR_AGENT", None)
            else:
                os.environ["HERDR_AGENT"] = previous
            return 0

        def fake_fetch(worker_id, state_dir=None):
            seen["during_fetch"] = os.environ.get("HERDR_AGENT")
            return {
                "commit": "a" * 40,
                "branch": f"sandbox/{worker_id}",
                "commit_count": 1,
                "log_oneline": "aaaa worker",
                "changed_files": ["one.py"],
                "diff_stat": " one.py | 1 +\n",
                "base_commit": "b" * 40,
                "source_repo": str(self.source),
                "worker_id": worker_id,
            }

        with patch.object(plugin, "check_root"), \
                patch.object(plugin, "resolve_source_repo_from_cwd", return_value=self.source), \
                patch.object(plugin, "check_dirty_tree", return_value=False), \
                patch.object(plugin, "ensure_image_exists"), \
                patch.object(plugin, "find_reusable_worker", return_value=("AUTO-002", None)), \
                patch.object(plugin, "open_worker", side_effect=fake_open), \
                patch.object(plugin, "fetch_worker", side_effect=fake_fetch), \
                patch("sys.stdout", new_callable=io.StringIO):
            code = plugin.hs_agent(plugin.DEFAULT_PROFILE, [])

        self.assertEqual(code, 0)
        self.assertEqual(seen["during_open"], plugin.DEFAULT_PROFILE.herdr_agent)
        self.assertEqual(seen["during_fetch"], plugin.DEFAULT_PROFILE.herdr_agent)
        self.assertNotIn("HERDR_AGENT", os.environ)

    def test_manifest_exposes_fetch_action(self):
        with (SCRIPT.parent.parent / "herdr-plugin.toml").open("rb") as stream:
            manifest = tomllib.load(stream)
        action = next(action for action in manifest["actions"] if action["id"] == "fetch")
        self.assertEqual(action["command"], ["python3", "bin/hs", "fetch"])


if __name__ == "__main__":
    unittest.main()
