---
name: herdr-sandbox-worker
description: Spawns sandboxed Codex or Claude Code workers through the Herdr Sandbox `hs` launcher, waits for them in Herdr panes, and reviews results on `sandbox/<id>` with plain git. Use when an orchestrator on the host should delegate work into a rootless-Podman worker instead of running a bare agent CLI in a pane.
---

# Herdr Sandbox Worker

Use `dev.herdr.sandbox` when a task should run in an isolated Codex or Claude Code worker. Herdr is the control plane on the host. The worker only owns its disposable clone. Results land as `sandbox/<id>` branches; integration is ordinary git chosen by you or the user—never automatic.

**Never** run a bare agent CLI (`codex`, `claude`) in a pane for delegated work. Always go through `hs` (`hs codex`, `hs claude`, or `hs <agent> --prompt "..."`). A bare CLI bypasses the sandbox.

## Preconditions

Work from the target Herdr Git workspace. Confirm the host is Linux with rootless Podman, Herdr 0.9+, Git, Python 3.11+, and the Codex and/or Claude Code CLI for host-side login. The plugin runs as the ordinary user, never via `sudo`.

Install or link the plugin if absent:

```sh
herdr plugin install emmG17/herdr-sandbox
# For development of this plugin itself:
herdr plugin link /path/to/herdr-sandbox
```

## One-time setup and login

```sh
herdr plugin action invoke bootstrap --plugin dev.herdr.sandbox
hs login codex     # and/or:
hs login claude
herdr plugin action invoke check --plugin dev.herdr.sandbox
```

`bootstrap` creates plugin-owned config/state, dedicated agent homes, the worker image, and an `hs` shim under `~/.local/bin` (ensure that directory is on `PATH`). `check` must succeed before spawning workers.

`hs login codex` runs device auth with `CODEX_HOME` pointed at the plugin-owned Codex home. `hs login claude` runs `claude auth login` with `CLAUDE_CONFIG_DIR` on the plugin-owned Claude home and sets `HOME` to an isolated directory under plugin state (`login-homes/claude`) so the host CLI cannot touch the real `~/.claude.json`. Never copy credentials from `~/.codex` or `~/.claude`.

Optional Claude API-key auth (instead of or alongside login): set `[claude].forward_api_key = true` in the plugin `config.toml` and export `ANTHROPIC_API_KEY` on the host. Prefer `hs login claude` so credentials live under the plugin-owned home.

## Orchestrator loop

Prefer a normal tiled pane (so `HERDR_PANE_ID` is set and the same pane can reuse one worker). Split a sibling if needed, keep focus, then spawn and wait:

```sh
herdr pane split --current --direction right --cwd "$PWD" --no-focus
# Read the new pane ID from the JSON response (.result.pane.pane_id), then:
herdr pane run <pane> "hs claude"
# or: herdr pane run <pane> "hs codex"
herdr agent wait <pane> --until done
```

### Discover the worker (do not use bare `hs inspect`)

`hs inspect` without an ID resolves against **your** `HERDR_PANE_ID` and cwd, not the worker pane. After wait, from the **source** repository:

1. Run `hs ls`.
2. Find the row whose `PANE` equals `<pane>`.
3. Take that row's `ID` and `RESULT`.
4. Require `STATUS=fetched` and `RESULT=sandbox/<id>` before reading git. If status is not yet `fetched`, wait briefly and re-run `hs ls` (interactive auto-fetch keeps `HERDR_AGENT` set until the sandbox ref exists; still verify before integrating).
5. Run `hs inspect <id>` (always with the explicit ID). Use its `Base:` line and the printed `Review (run in source repo ...):` git commands. Auto-fetch / `hs fetch` also print ready-made `Review:` lines—you may use those directly.

```sh
hs ls
# PANE matches <pane> → note ID and RESULT=sandbox/<id>; STATUS must be fetched
hs inspect <id>
# Base: <base>   and Review lines, e.g.:
#   git -C <source> log <base>..sandbox/<id>
#   git -C <source> diff <base>...sandbox/<id>
```

Only then run git (or paste the Review lines from fetch / `hs inspect <id>`):

```sh
git log <base>..sandbox/<id>
git diff <base>...sandbox/<id>
```

### Non-interactive `--prompt`

Prefer running `hs <agent> --prompt` **directly** (same source-repo cwd) when the orchestrator needs the process exit code. `herdr pane run` only acknowledges that the command was queued; it does not return the agent/fetch exit status—use `herdr pane read` or the worker's `runs/<run-id>/result.json` if you must go through a pane.

```sh
hs claude --prompt "Implement the change and commit in the worker clone."
# or: hs codex --prompt "..."
```

On success the launcher prints a summary (`print_execution_result`): Worker, Run, Exit status, HEAD, Result branch, Commit count, Dirty, Git status, Result path, Log path. Base for review still comes from `hs inspect <id>` (or the Review lines printed at fetch).

Exit rules (`hs_agent` with `--prompt`):

- Setup / argument errors (no worker, extra args with `--prompt`, bad prompt, readiness failure) → exit `1`.
- Otherwise → the agent's exit status.
- If auto-fetch failed (`result_branch` missing) → that exit status, or `1` when the agent exited `0`.

After `--prompt`, still run `hs ls`, confirm `STATUS=fetched` and `RESULT=sandbox/<id>`, then `hs inspect <id>` before integrating.

`hs <agent>` creates or reuses a worker for the current pane, launches the agent in the sandbox, and on agent exit auto-fetches into `refs/heads/sandbox/<id>`. The source tree must be clean (no uncommitted changes).

### Popup-pane caveat

Plugin popup panes do not receive `HERDR_PANE_ID`. Without it, every `hs` launch creates a **new** worker; there is no per-pane reuse. Prefer `herdr pane split` / `herdr pane run` on ordinary tiled panes for orchestrated work.

## Integrate with plain git

Review first. Then integrate only what you or the user approve—`git merge`, `git rebase`, or `git cherry-pick` selected commits. The plugin never merges into the current branch and has no action that applies worker commits for you.

```sh
git merge sandbox/<id>
# or: git cherry-pick <sha> … / git rebase …
```

## Cleanup

GC is mostly implicit: closing a Herdr pane runs GC for that pane's fetched workers (`pane.closed`), and each `hs <agent>` launch runs a short opportunistic GC. GC never deletes or rewrites `sandbox/*` (or other) refs in the source repo.

Remove a worker explicitly when needed:

```sh
hs rm <id>
hs gc
```

## Safety boundary

Rootless Podman is the isolation boundary (not a VM). Agent launches (`hs <agent>`, `hs <agent> --prompt`, and the `start-*` pane entrypoints) mount only:

- the worker clone at `/workspace` (read-write), and
- a **per-launch session home** at `/codex` or `/claude` that contains only authentication artifacts copied from the dedicated plugin agent home.

They do not mount the host home, normal `~/.codex` / `~/.claude` / `~/.claude.json`, SSH/GPG/cloud credentials, the source repository as writable, or Podman/Docker sockets. Capabilities are dropped, `no-new-privileges` is set, the container root is read-only, and `--userns=keep-id` is used. Host environment variables are never forwarded into the container implicitly (except the optional explicit `ANTHROPIC_API_KEY` inherit when `[claude].forward_api_key` is enabled).

Caveat (tracked in issue #29): a non-fresh bash `open` (interactive shell into an existing worker) still bind-mounts the **whole** configured plugin agent home at `/codex` or `/claude`, not a session home. Prefer `hs <agent>` / `--prompt` for delegated work; do not treat a bash `open` session as auth-only.

Do not use this for untrusted or hostile workloads. Before relying on a new environment, run the repository's disposable-worker security test. If any access expected to be denied succeeds, preserve the worker, do not fetch or integrate it, and escalate the finding.
