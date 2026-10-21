# Herdr Sandbox Workers

`dev.herdr.sandbox` is a Linux plugin for Herdr. It runs Codex in a disposable
Git clone inside a hardened, rootless Podman container; Herdr remains the
control plane.

## What it does—and does not do

Each worker gets an independent local clone. Codex may read, write, delete,
install dependencies, run commands, and commit **inside that clone**. It does
not operate directly in your source checkout.

The plugin does not replace Herdr, automatically merge worker changes, or give
a worker host credentials, container-engine sockets, or source-repository
write access. `fetch` force-updates only `refs/heads/sandbox/<id>` in the
source repo. Integration is ordinary git (`merge`, rebase, or applying
selected commits) after you review that branch.

Codex uses `danger-full-access` *inside the container*. Rootless Podman is the
security boundary: the runtime mounts only the worker repository at
`/workspace` and a dedicated plugin Codex home at `/codex`. It does not mount
your normal `~/.codex`, host home, SSH/GPG/cloud credentials, or
Podman/Docker sockets. It uses `--userns=keep-id`, drops capabilities, enables
`no-new-privileges`, and has a read-only container root.

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
- Git, Python 3.11 or later, and a current Codex CLI

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
CODEX_HOME="$(herdr plugin config-dir dev.herdr.sandbox)/codex" codex login --device-auth
herdr plugin action invoke check --plugin dev.herdr.sandbox
```

`bootstrap` creates plugin-owned configuration and state directories, a
dedicated Codex home, and the worker image. It also installs a small `hs` shim
to `~/.local/bin/hs` (ensure `~/.local/bin` is on your PATH). The shim resolves
the current plugin root from Herdr at runtime, so it continues to work after
plugin updates. Authenticate only in that dedicated home, never in the normal
user Codex home.

Then, in a Herdr pane containing the Git repository you want to change:

```sh
cd ~/my-project
hs codex
```

`hs codex` automatically creates or reuses a worker for the current pane and
launches Codex in the sandbox. When the agent exits, it fetches the worker tip
into `refs/heads/sandbox/<id>` and prints the branch name, commit count, and a
short log. It works from any Herdr pane or plain shell without requiring
action-specific environment variables.

Review and integrate with plain git:

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

Alternatively, use the plugin actions:

```sh
herdr plugin action invoke create --plugin dev.herdr.sandbox
herdr plugin pane open --plugin dev.herdr.sandbox --entrypoint start-codex --placement overlay
herdr plugin action invoke inspect --plugin dev.herdr.sandbox
herdr plugin action invoke fetch --plugin dev.herdr.sandbox
```

`create` generates a worker ID and clones the workspace repository.
`start-codex` opens a fresh Codex session in that disposable clone. With exactly
one worker, `start-codex` and `fetch` select it automatically; when several
workers exist, configure `[open].id` in `config.toml` for the pane. `inspect`
resolves the current pane's worker (or requires an explicit ID)—it no longer
reads `[inspect].id`.

`fetch` writes `refs/heads/sandbox/<id>` and prints the same review and
integration commands. After keeping or discarding the result, remove the
worker explicitly or let GC clean it up:

```sh
hs rm <id>
hs gc
```

Closing a Herdr pane triggers a `pane.closed` plugin event that runs
`hs gc --from-event` for that pane's fetched workers. Each `hs codex` launch
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

The shim uses `herdr plugin root`, `herdr plugin config-dir`, and `herdr plugin
state-dir` to discover directories at runtime, so it works regardless of XDG
overrides or custom Herdr base directories.

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
`/workspace`, and starts Codex with `--cd /workspace`. Each interactive
`start-codex` / `hs codex` launch also gets a temporary plugin-owned `CODEX_HOME`
containing only `auth.json`; the directory is removed when the session exits,
so history from another worker or workspace cannot be resumed.

Noninteractive `hs codex --prompt "..."` (and the thin `execute --prompt`
alias) keeps the shared dedicated Codex home mount. Long noninteractive runs
can refresh tokens in place under that home; a per-launch temp home would
discard those writes when the session ends. Issue #11 owns the token-refresh
decision for every profile (write-back of `auth.json`, never write back, or
mount auth alone). Until #11 lands, `--prompt` continues to use the shared
home and does not write tokens back from a temp home.

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
checkout and Codex credentials cannot be copied into the image.

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

## Advanced use and troubleshooting

For noninteractive automation from a clean source tree:

```sh
hs codex --prompt "Implement the requested change and run the relevant tests."
# Long prompts: hs codex --prompt - < prompt.txt
```

This reuses the interactive launcher path (generated worker ID, dirty-tree
refusal, state store/lock, auto-fetch into `sandbox/<id>`). Codex stdout and
stderr stream live; stdin to the agent is closed. The process exits with the
agent's exit status. Every run also writes `runs/<run-id>/result.json` and
`runs/<run-id>/output.log` under the worker directory. `result.json` keeps its
existing keys and adds `result_branch` and `commit_count` (computed in the
**source** repo against the fetched `sandbox/<id>` ref). `hs inspect` exposes
the latest run; `hs ls` stays limited to the RFC columns. `[execute]` in
`config.toml` is ignored with a one-line deprecation notice. `execute --prompt`
remains a thin alias until thin action wrappers land.

You may configure action-specific IDs for `fetch` or the interactive
`start-codex` pane via `[open].id` when multiple workers exist. Direct script
use requires Herdr's own paths; do not invent storage locations:

```sh
HERDR_PLUGIN_CONFIG_DIR=/path/to/config HERDR_PLUGIN_STATE_DIR=/path/to/state \
python3 bin/herdr-sandbox create --id WORKER-001 --repo /path/to/source-repo --base main
```

### One-off prompt and remove inputs

Herdr 0.9.0 plugin actions have fixed command arguments. `herdr plugin action
invoke` does not forward arguments after the action ID, so prefer `hs` on
`PATH` or the bound launcher for per-run prompts.

Setup/bootstrap creates an executable launcher at the plugin config directory,
which can be found without discovering Herdr's state directory:

```sh
SANDBOX_LAUNCHER="$(herdr plugin config-dir dev.herdr.sandbox)/herdr-sandbox"

hs codex --prompt "Implement the requested change and run the relevant tests."
# Thin alias (same launcher path):
"$SANDBOX_LAUNCHER" execute --prompt "Implement the requested change and run the relevant tests."

"$SANDBOX_LAUNCHER" fetch WORKER-001

"$SANDBOX_LAUNCHER" rm --id WORKER-001
# destroy --id remains an alias of rm until thin action wrappers land
"$SANDBOX_LAUNCHER" destroy --id WORKER-001
```

The launcher safely binds the Herdr-provided config and state directories to the
plugin script and forwards arguments directly, without shell evaluation. It
still uses the same hardened runtime, worker-path validation, and remove
ordering as the corresponding Herdr action. After auto-fetch (or `hs fetch`),
integrate with plain git on `sandbox/<id>`. Rerun setup/bootstrap after changing
the plugin installation so the launcher refreshes its bound script path.

The plugin waits for the Codex process it starts, but it cannot add a separate
`--wait` or status protocol to Herdr's plugin-action API. Herdr 0.9.0 controls
whether the caller waits for and displays an action's live output. If that
control-plane invocation is asynchronous or does not forward the stream, use
`list` or `inspect` after the run; the persisted result and log remain the
source of truth. Worker cleanup: `rm` / `destroy --id` removes the worker
directory, including its run history. Prefer `hs gc` for routine cleanup.

- **Podman unavailable or running as root:** repair rootless Podman and check
  `podman info --format '{{.Host.Security.Rootless}}'` returns `true`.
- **Worker image missing:** run `bootstrap` or `build-image`, then `check`.
- **Codex authentication missing:** rerun the dedicated `CODEX_HOME=...` login;
  never copy credentials from `~/.codex`.
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
