# Herdr Sandbox Workers

`dev.herdr.sandbox` is a Linux plugin for Herdr. It runs Codex or Claude Code in
a disposable Git clone inside a hardened, rootless Podman container; Herdr
remains the control plane.

## What it does—and does not do

Each worker gets an independent local clone. The agent may read, write, delete,
install dependencies, run commands, and commit **inside that clone**. It does
not operate directly in your source checkout.

The plugin does not replace Herdr, automatically merge worker changes, or give
a worker host credentials, container-engine sockets, or source-repository
write access. `fetch` force-updates only `refs/heads/sandbox/<id>` in the
source repo. Integration is ordinary git (`merge`, rebase, or applying
selected commits) after you review that branch.

Agents use full in-container permissions (Codex: `danger-full-access`; Claude
Code: `--dangerously-skip-permissions`). Rootless Podman is the security
boundary: agent launches (`hs <agent>`, `--prompt`, `start-*` panes) mount only
the worker repository at `/workspace` and a per-launch session home at `/codex`
or `/claude` (auth artifacts only). They never mount the configured plugin
agent home wholesale, nor your normal `~/.codex`, `~/.claude`, `~/.claude.json`,
host home, SSH/GPG/cloud credentials, or Podman/Docker sockets. Isolation uses
`--userns=keep-id`, drops capabilities, enables `no-new-privileges`, and a
read-only container root. Host environment variables are never forwarded into
the container implicitly.

**Host-side Git operations**: The plugin never runs git commands with a worker  
repository as cwd or `-C` target. Worker `.git/config` is attacker-controlled  
and could execute arbitrary commands through settings like `core.fsmonitor` or  
`core.hooksPath`. Instead it reads refs from confined files under the worker  
`.git`, imports results through a verified git bundle built from copied loose  
objects and packs (indexes regenerated on the host), and runs status/log/diff  
in one hardened query container per worker (no session name, no network, no  
Codex mount, workspace read-only). See `docs/security-host-git.md`.

This is container isolation, not a VM: containers share the host Linux kernel.
Do not use it for hostile-code workloads or where kernel-vulnerability
protection is required.

## Requirements

- Linux with rootless Podman
- Herdr 0.9.0 or later
- Git, Python 3.11 or later, and a current Codex and/or Claude Code CLI for host-side `hs login`

On Arch Linux:

```sh
sudo pacman -S --needed git python podman shadow fuse-overlayfs
podman info --format '{{.Host.Security.Rootless}}'
```

The last command must print `true`. Rootless Podman needs user-ID ranges in
`/etc/subuid` and `/etc/subgid`; use your distribution's Podman setup guide if
they are absent. Never run this plugin through `sudo`.

## Install

Install the published plugin:

```sh
herdr plugin install emmG17/herdr-sandbox
herdr plugin action list --plugin dev.herdr.sandbox
```

To develop the plugin itself, link a local checkout instead:

```sh
herdr plugin link /path/to/herdr-sandbox
herdr plugin list --plugin dev.herdr.sandbox
```

Linking registers the manifest but does not build the worker image. Do not
install over a local link; unlink it first if you change installation mode.

## Quick start

Run the first three commands once:

```sh
herdr plugin action invoke bootstrap --plugin dev.herdr.sandbox
hs login codex    # and/or: hs login claude
herdr plugin action invoke check --plugin dev.herdr.sandbox
```

`bootstrap` creates plugin-owned configuration and state directories, dedicated
agent homes, and the worker image (Codex and Claude Code CLIs). It also installs
a small `hs` shim to `~/.local/bin/hs` (ensure `~/.local/bin` is on your PATH).
The shim resolves the current plugin root from Herdr at runtime, so it continues
to work after plugin updates. `hs login codex` runs device auth with
`CODEX_HOME` pointed at the plugin-owned home. `hs login claude` runs
`claude auth login` with `CLAUDE_CONFIG_DIR` pointed at the plugin-owned Claude
home (see [Claude Code authentication](https://code.claude.com/docs/en/authentication)
and [`CLAUDE_CONFIG_DIR`](https://code.claude.com/docs/en/env-vars)).
`CLAUDE_CONFIG_DIR` relocates the `~/.claude` config directory (including
`.credentials.json`), but it does **not** relocate the separate global
`~/.claude.json` file, which is HOME-relative
([Claude directory](https://code.claude.com/docs/en/claude-directory)).
`hs login claude` therefore also sets `HOME` to a dedicated plugin-owned
directory under the plugin state dir (`login-homes/claude`, mode `0700`) so the
host CLI cannot create or modify the real `~/.claude.json`. That login HOME is
a sibling of worker state, not under the Claude agent home (workers only mount
per-launch session homes that copy auth files). Codex leaves host `HOME`
unchanged because `CODEX_HOME` already redirects its state. Login refuses
normal user homes (`~/.codex`, `~/.claude`) and banned host paths such as
`~/.claude.json`. After `hs login claude`, the manual security test still
`cmp`s the host `~/.claude.json` as a regression check (see
`docs/manual-worker-security-test.md`).

Then, in a Herdr pane containing the Git repository you want to change:

```sh
cd ~/my-project
hs codex
# or: hs claude
```

`hs codex` / `hs claude` automatically creates or reuses a worker for the current pane and
launches the agent in the sandbox. When the agent exits, it fetches the worker tip
into `refs/heads/sandbox/<id>` and prints the branch name, commit count, and a
short log. It works from any Herdr pane or plain shell without requiring
action-specific environment variables.

Review and integrate with plain git (never automatic):

```sh
git log main..sandbox/<id>
git diff main...sandbox/<id>
git merge sandbox/<id>    # or cherry-pick or rebase
hs fetch <id>   # re-run fetch by hand (for example after switching off a checked-out sandbox branch)
```

List workers and inspect the current pane's worker:

```sh
hs ls
hs inspect          # defaults to this pane's worker when HERDR_PANE_ID is set
hs inspect <id>     # explicit ID when the pane is ambiguous or unset
```

`hs ls` shows id, agent, source repo, pane, status (including `fetched`),
result branch (`sandbox/<id>` or `-`), and created time. It reads the state
store and container status only—no per-worker git query. `hs inspect` adds the
latest run result, container-side dirty status, and review commands that run in
the **source** repo against `sandbox/<id>`.

### Orchestrator agents on the host

Host orchestrators should spawn sandboxed workers through `hs`, not bare
`codex` / `claude` in a pane (that bypasses the sandbox). Prefer ordinary
tiled panes so `HERDR_PANE_ID` is set and the pane can reuse one worker.
Plugin popup panes lack `HERDR_PANE_ID`, so every launch gets a new worker.

Do **not** run bare `hs inspect` from the orchestrator pane: without an ID it
resolves against the caller's `HERDR_PANE_ID` and cwd, not the worker pane.
When you need the process exit code, run `hs <agent> --prompt` directly;
`herdr pane run` does not return it (use `herdr pane read` or
`runs/<run-id>/result.json` instead).

```sh
herdr pane split --current --direction right --cwd "$PWD" --no-focus
herdr pane run <pane> "hs claude"    # or: "hs codex"
herdr agent wait <pane> --until done
# In the source repo — discover first, then inspect by ID:
hs ls
# Find PANE=<pane>; require STATUS=fetched and RESULT=sandbox/<id>
hs inspect <id>
# Use Base: and the printed Review lines (or the Review: lines from auto-fetch)
git log <base>..sandbox/<id>
git diff <base>...sandbox/<id>
hs rm <id>    # optional; GC also runs on pane close and at launch
```

The shipped skill `skills/herdr-sandbox-worker/SKILL.md` documents this loop
for agents. There is no duplicate under `.agents/skills/` (that tree only
holds Beads guidance).

Herdr actions are thin wrappers over the same `hs` commands (`list` → `hs ls`,
`inspect` → `hs inspect`, `destroy` / `gc` → `hs gc`). The `start-codex` /
`start-claude` panes run `hs codex` / `hs claude`. Setup actions (`setup`,
`bootstrap`, `build-image`, `check`) stay as direct script commands. Menu
`inspect` lists every worker for `workspace_cwd` from
`HERDR_PLUGIN_CONTEXT_JSON` (actions run with the plugin directory as cwd).
CLI `hs inspect` without an ID still uses `HERDR_PANE_ID` plus the process
cwd. There is no menu `fetch` or `rm` action — use `hs fetch <id>` /
`hs rm <id>` on the CLI (auto-fetch already runs when an agent exits).

After keeping or discarding a result, remove the worker explicitly or let GC
clean it up:

```sh
hs rm <id>
hs gc
```

Closing a Herdr pane triggers a `pane.closed` plugin event that runs
`hs gc --from-event` for that pane's fetched workers. Each `hs <agent>` launch
also runs a short opportunistic GC (non-blocking state lock, no
fetch-before-GC, 2s budget). GC never deletes or rewrites refs in the
source repository (including `sandbox/*` branches).

Configure retention with `[gc].max_age` (default `7d`). Explicit `hs gc`
fetches an unfetched worker older than `max_age` first and deletes only on
success; otherwise it skips with a one-line warning. Before deleting any
`fetched` worker, GC also requires the current `agent/<id>` tip and
`refs/heads/sandbox/<id>` to equal `fetched_commit` (re-fetch on explicit
`hs gc`, skip on event/opportunistic paths if they diverge). Containers are
collected only when status is `not-found`, `exited`, or `stopped`. Pane
absence requires a structured `pane_not_found` from `herdr pane get`.
`pane.exited` is not a GC trigger.

List durable worker state at any time:

```sh
hs ls
herdr plugin action invoke list --plugin dev.herdr.sandbox
```

### `hs` CLI Installation

The `bootstrap` or `setup` action installs a small shim script to `~/.local/bin/hs`.
This shim queries Herdr for the current plugin root each time it runs, making it
resilient to plugin updates (GitHub-managed plugin checkouts get replaced on
update, which would break a direct symlink).

If `~/.local/bin` is not on your PATH, either:
- Add it: `export PATH="$HOME/.local/bin:$PATH"` (add to `~/.bashrc` or `~/.zshrc`)
- Use the full path: `~/.local/bin/hs codex`
- Call the plugin's `bin/hs` directly: `$(herdr plugin root dev.herdr.sandbox)/bin/hs codex`

The installed `~/.local/bin/hs` shim resolves the plugin root via
`herdr plugin list --plugin dev.herdr.sandbox --json`, then runs the plugin's
`bin/hs`. That script queries `herdr plugin config-dir <id>` and derives the
state directory from `XDG_STATE_HOME` (or `~/.local/state/herdr/plugins/<id>`),
because Herdr 0.9.x has no `herdr plugin state-dir` command.

## Herdr 0.9.0 integration limits

Herdr 0.9.0 runs `plugin action invoke` commands in the invoking workspace and
does not let a plugin dynamically create or move an action pane. The plugin
declares `start-codex` only as a managed `[[panes]]` entrypoint. Use `herdr
plugin pane open ... --entrypoint start-codex`; its default placement is an
overlay over the active pane. Open it from the worker's source workspace. If
Herdr reports a different `workspace_cwd` than the selected worker's recorded
source repository, the plugin refuses to launch instead of exposing that
workspace's task.

### Pane placement

The supported placements for `start-codex` are `overlay`, `split`, and `tab`.
The canonical target option is `--target-pane PANE`; this is the same value
Herdr's plugin.pane.open API sends as `target_pane_id`. The CLI flag and the
API field name the same concept, so use `--target-pane` in both the Herdr CLI
and the plugin launcher.

- `overlay` (default) opens over the active pane. It does not accept a target
  pane or direction.
- `split` opens next to a specific pane. It requires both `--target-pane` and
  `--direction right|down`.
- `tab` opens in a new tab. It does not accept a target pane or direction.

Examples:

```sh
# Overlay over the active pane (default)
herdr plugin pane open --plugin dev.herdr.sandbox --entrypoint start-codex --placement overlay

# Split an existing pane
herdr plugin pane open --plugin dev.herdr.sandbox --entrypoint start-codex \
  --placement split --target-pane w1:p1 --direction right

# New tab
herdr plugin pane open --plugin dev.herdr.sandbox --entrypoint start-codex --placement tab
```

You can also use the plugin launcher, which validates the placement contract
before calling Herdr:

```sh
SANDBOX_LAUNCHER="$(herdr plugin config-dir dev.herdr.sandbox)/herdr-sandbox"
"$SANDBOX_LAUNCHER" pane-open --placement split --target-pane w1:p1 --direction right
```

The Podman command is independent of Herdr's pane cwd: it mounts only the
selected worker clone at `/workspace`, sets the container workdir to
`/workspace`, and starts the agent with profile-owned flags (Codex:
`--cd /workspace`; Claude Code: `--dangerously-skip-permissions`, with
`CLAUDE_CONFIG_DIR` pointing at the mounted `/claude` home). Both interactive
`hs <agent>` / `start-<agent>` panes and noninteractive `hs <agent> --prompt "..."`
use the same per-launch temporary plugin-owned agent home that contains only
auth artifacts (`auth.json` for Codex; `.credentials.json` for Claude). The
session directory is removed when the agent exits, so history from another
worker or workspace cannot be resumed. For Claude, `--prompt` maps to `-p`.
The configured plugin agent home is never bind-mounted into a worker.

Optional Claude API-key auth: set `[claude].forward_api_key = true` and export
`ANTHROPIC_API_KEY` on the host. Podman then receives `--env ANTHROPIC_API_KEY`
with **no value** (inherits from the `hs` process environment), so the secret
never appears on argv / `ps` / `/proc/*/cmdline`. Host env is never forwarded
implicitly; never put the key in the repo, image, or config file. Prefer
`hs login claude` so credentials live under the plugin-owned
`CLAUDE_CONFIG_DIR` instead
([docs](https://code.claude.com/docs/en/authentication)).

**Token refresh (option a):** write-back is always attempted in a `finally`
block after the agent exits (including non-zero exits), then the per-launch
session directory is removed. Only the profile auth file(s) are candidates.
Write-back is atomic (temp file in the same directory, fsync, rename), runs
under the state lock, keeps mode `0600`, and refuses symlinks. Under the lock
it compare-and-swaps against the launch snapshot using file bytes only: the
session file must be valid JSON with the expected shape and differ from the
snapshot; if the dedicated destination already differs from that snapshot,
another session advanced auth and the write is skipped with a log line. A
mtime-only change on the destination does not block a legitimate refresh.
Unchanged or invalid session files are left alone. A hard kill (`SIGKILL`) can
skip write-back and leave a session directory; `sweep_stale_agent_sessions`
removes leftovers later. Because `--prompt` now uses the same per-launch home
as interactive mode, only auth is preserved across runs—history and other
session files under the dedicated home no longer accumulate. Re-run
`hs login <agent>` only if authentication is missing or write-back rejected a
bad file.

For Herdr screen-based agent detection when Podman wraps the real CLI, `hs`
sets `HERDR_AGENT` on the **host** pane/wrapper process (not only inside the
container; values match [Herdr agents](https://herdr.dev/docs/agents):
`codex` / `claude`). Isolation flags stay fixed on the runtime; agent profiles
may only supply image install packages, home mount, env, argv, auth files /
optional API-key env, banned host paths, forbidden args, and `HERDR_AGENT`.

## Customize the worker image

In the plugin configuration directory (`herdr plugin config-dir
dev.herdr.sandbox`), `[runtime].image` chooses the built worker tag and
`[build].base_image` chooses the starter Containerfile's `FROM` image:

```toml
[build]
base_image = "python:3.12-slim-bookworm"
```

Setup copies an editable starter to `image/Containerfile` there. Add
project-specific packages or language tools, then rebuild:

```sh
herdr plugin action invoke build-image --plugin dev.herdr.sandbox
```

The starter expects a Debian/Ubuntu-compatible base with `apt-get`. For Alpine
or another distribution, adapt its package-install steps and base image. The
build context is only the plugin-owned `image` directory, so your source
checkout and agent credentials cannot be copied into the image.

Set `[runtime].network` to `"offline"` before creating or opening a
self-contained worker to use Podman's `--network=none`; keep it `"online"`
when Codex or dependency installation needs network access. CPU, memory, and
PID limits are set in `[resources]`; isolation flags and mounts are fixed.

```toml
[runtime]
network = "offline"
```

Worker retention:

```toml
[gc]
max_age = "7d"
```

### Experimental reflink workspace strategy

**Experimental.** By default workers are created with `git clone --no-hardlinks`
(`[workspace].strategy = "clone"`). On Linux filesystems with reflink/CoW
support (btrfs, XFS with `reflink=1`), you can set:

```toml
[workspace]
strategy = "reflink"
copy_untracked = ["node_modules", "target", ".venv"]
```

Allowlisted paths should be gitignored so the source stays clean. Before
creating a worker, the plugin probes reflink by cloning a temp file from the
**source** filesystem into the **plugin state** directory using the same
`O_NOFOLLOW` + `FICLONE` primitive as allowlisted copies (no `cp` fallback).
Reflink cannot cross filesystems: if state and source are on different devices,
or the FS lacks CoW clones, create prints one notice line and falls back to
`clone` (no allowlist copy).

With a successful probe:

- Worker `.git` is built like the clone strategy (no hardlinked objects, no
  `alternates`, fresh hooks/config/info). The source `.git` is never reflinked.
- **Tracked files always come from git objects** (`checkout-index`), never from
  the source worktree, so `assume-unchanged`, `autocrlf`, or a swapped path
  cannot leak worktree bytes into the worker.
- Only allowlisted untracked/ignored paths are reflinked on top. Everything
  else (including `.env`) stays out.
- `copy_untracked` entries must be nonempty relative paths strictly under the
  source: no absolute paths, no `.` / `./` / empty roots, no `.` or `..`
  parts, and no `.git` segment at any depth. An allowlist entry that is itself
  a symlink is rejected. Nested `.git` directories are also skipped at walk time.
- Relative symlinks under an allowlisted tree are recreated only when their
  resolved target stays inside that same top-level entry (pnpm links within
  `node_modules` keep working). Absolute symlinks and links to `.env`, `.git`,
  or anywhere else outside that entry are skipped with one stderr line each.
- Hardlinks from allowlisted files to non-allowlisted untracked/ignored paths,
  to tracked files outside the allowlist, or to any regular file under
  `source/.git` are skipped with one stderr line each, so a `.env` or
  `.git/config` hardlinked into `node_modules` does not reach the worker.
  Hardlinks that stay entirely inside the allowlist (common with pnpm/npm) are
  kept. Allowlisted trees are walked with directory fds and
  `O_NOFOLLOW`/`O_DIRECTORY` so a directory swapped for a symlink to `../.git`
  fails closed instead of copying git metadata.
- Gitlinks/submodules are not populated; create prints one stderr notice listing
  skipped paths.
- Reflinked files get new inodes; a write in the worker must not change the
  source.
- Dirty-tree refusal runs inside `create_worker` whenever
  `strategy = "reflink"` (not only in the launcher).

## Advanced use and troubleshooting

For noninteractive automation from a clean source tree:

```sh
hs codex --prompt "Implement the requested change and run the relevant tests."
hs claude --prompt "Implement the requested change and run the relevant tests."
# Long prompts: hs codex --prompt - < prompt.txt
```

This reuses the interactive launcher path (generated worker ID, dirty-tree
refusal, state store/lock, auto-fetch into `sandbox/<id>`). Codex stdout and
stderr stream live; stdin to the agent is closed. The process exits with the
agent's exit status. Every run also writes `runs/<run-id>/result.json` and
`runs/<run-id>/output.log` under the worker directory. `result.json` keeps its
existing keys and adds `result_branch` and `commit_count` (computed in the
**source** repo against the fetched `sandbox/<id>` ref). `hs inspect` exposes
the latest run; `hs ls` stays limited to the RFC columns. Legacy per-action
tables in an old `config.toml` are ignored with a single deprecation notice.

### One-off prompt and remove inputs

Prefer `hs` on `PATH` (or the bound launcher under the plugin config directory)
for per-run prompts and cleanup. Herdr 0.9.0 plugin actions have fixed command
arguments and do not forward extras after the action ID, so prompts go through
`hs <agent> --prompt`, not a config table.

Setup/bootstrap creates an executable launcher at the plugin config directory:

```sh
SANDBOX_LAUNCHER="$(herdr plugin config-dir dev.herdr.sandbox)/herdr-sandbox"

hs codex --prompt "Implement the requested change and run the relevant tests."
# Same path via the bound launcher:
"$SANDBOX_LAUNCHER" codex --prompt "Implement the requested change and run the relevant tests."

"$SANDBOX_LAUNCHER" fetch WORKER-001
"$SANDBOX_LAUNCHER" rm --id WORKER-001
# destroy --id remains a CLI alias of rm
"$SANDBOX_LAUNCHER" destroy --id WORKER-001
```

The launcher safely binds the Herdr-provided config and state directories to the
plugin script and forwards arguments directly, without shell evaluation. It
still uses the same hardened runtime, worker-path validation, and remove
ordering as the corresponding Herdr action. After auto-fetch (or `hs fetch`),
integrate with plain git on `sandbox/<id>`. Rerun setup/bootstrap after changing
the plugin installation so the launcher refreshes its bound script path.

The plugin waits for the agent process it starts, but it cannot add a separate
`--wait` or status protocol to Herdr's plugin-action API. Herdr 0.9.0 controls
whether the caller waits for and displays an action's live output. If that
control-plane invocation is asynchronous or does not forward the stream, use
`hs ls` or `hs inspect` after the run; the persisted result and log remain the
source of truth. Worker cleanup: `hs rm` removes the worker directory,
including its run history. Prefer `hs gc` for routine cleanup.

- **Podman unavailable or running as root:** repair rootless Podman and check
  `podman info --format '{{.Host.Security.Rootless}}'` returns `true`.
- **Worker image missing:** run `bootstrap` or `build-image`, then `check`.
- **Codex authentication missing:** run `hs login codex` (or the equivalent
  `CODEX_HOME=... codex login --device-auth`); never copy credentials from
  `~/.codex`.
- **Worker failed or stale:** use `hs inspect` and `hs ls`; use `hs rm <id>` or
  wait for `hs gc` after fetch.
- **Task needs network:** use the online profile; offline workers cannot fetch
  dependencies or contact model APIs.

Before relying on the plugin, run the
[manual disposable-worker security test](docs/manual-worker-security-test.md)
against a clean disposable worker. If a denied check succeeds, stop using the
plugin, preserve the worker for investigation, and do not fetch or integrate
its changes.

## Help, feedback, and changes

Report bugs, request features, propose documentation improvements, or discuss
a behavior change in [GitHub Issues](https://github.com/emmG17/herdr-sandbox/issues).
Open an issue before starting implementation or a pull request so the
maintainer can confirm the design and scope.

Include your Linux distribution, Herdr/Codex/Podman versions, reproduction
steps, expected and actual behavior, and sanitized logs. Do not put tokens,
credentials, repository secrets, or private source code in an issue.

For a suspected isolation escape, credential exposure, or other security
vulnerability, do **not** open a public issue. Report it privately at
[emmanuel@emm-g.com](mailto:emmanuel@emm-g.com) with enough detail to reproduce
it safely.
