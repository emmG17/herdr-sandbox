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

## Orchestrator loop

Prefer a normal tiled pane (so `HERDR_PANE_ID` is set and the same pane can reuse one worker). Split a sibling if needed, keep focus, then spawn:

```sh
herdr pane split --current --direction right --cwd "$PWD" --no-focus
# Read the new pane ID from the JSON response (.result.pane.pane_id), then:
herdr pane run <pane> "hs claude"
# or: herdr pane run <pane> "hs codex"
herdr agent wait <pane> --until done
```

For non-interactive work (no TTY agent session), run the prompt form in the pane or from a shell that has the source repo as cwd:

```sh
herdr pane run <pane> "hs claude --prompt \"Implement the change and commit in the worker clone.\""
# or: hs claude --prompt "..."
# or: hs codex --prompt "..."
herdr agent wait <pane> --until done
```

`hs <agent>` creates or reuses a worker for the current pane, launches the agent in the sandbox, and on agent exit auto-fetches the worker tip into `refs/heads/sandbox/<id>` in the source repo. `--prompt` closes agent stdin, streams output, persists `runs/<run-id>/result.json` and `output.log` under the worker directory, then auto-fetches the same way. The source tree must be clean (no uncommitted changes).

### Popup-pane caveat

Plugin popup panes do not receive `HERDR_PANE_ID`. Without it, every `hs` launch creates a **new** worker; there is no per-pane reuse. Prefer `herdr pane split` / `herdr pane run` on ordinary tiled panes for orchestrated work.

## Read results

In the **source** repository:

```sh
hs ls
hs inspect          # this pane's worker when HERDR_PANE_ID is set
hs inspect <id>     # when the pane is ambiguous or unset

git log <base>..sandbox/<id>
git diff <base>...sandbox/<id>
```

`hs ls` lists id, agent, source repo, pane, status, result branch (`sandbox/<id>` or `-`), and created time. `hs inspect` adds the latest run summary, container-side dirty status, and review commands that run in the source repo against `sandbox/<id>`.

## Integrate with plain git

Review first. Then integrate only what you or the user approve—merge, rebase, or apply selected commits. The plugin never merges into the current branch.

```sh
git log main..sandbox/<id>
git diff main...sandbox/<id>
git merge sandbox/<id>
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

They do not mount the host home, normal `~/.codex` / `~/.claude` / `~/.claude.json`, SSH/GPG/cloud credentials, the source repository as writable, or Podman/Docker sockets. Capabilities are dropped, `no-new-privileges` is set, the container root is read-only, and `--userns=keep-id` is used. Host environment variables are never forwarded into the container implicitly.

Caveat (tracked in issue #29): a non-fresh bash `open` (interactive shell into an existing worker) still bind-mounts the **whole** configured plugin agent home at `/codex` or `/claude`, not a session home. Prefer `hs <agent>` / `--prompt` for delegated work; do not treat a bash `open` session as auth-only.

Do not use this for untrusted or hostile workloads. Before relying on a new environment, run the repository's disposable-worker security test. If any access expected to be denied succeeds, preserve the worker, do not fetch or integrate it, and escalate the finding.
