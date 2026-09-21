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
write access. `fetch` writes only `FETCH_HEAD`; cherry-picking is a separate,
explicit action after review.

Codex uses `danger-full-access` *inside the container*. Rootless Podman is the
security boundary: the runtime mounts only the worker repository at
`/workspace` and a dedicated plugin Codex home at `/codex`. It does not mount
your normal `~/.codex`, host home, SSH/GPG/cloud credentials, or
Podman/Docker sockets. It uses `--userns=keep-id`, drops capabilities, enables
`no-new-privileges`, and has a read-only container root.

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
dedicated Codex home, and the worker image. Authenticate only in that dedicated
home, never in the normal user Codex home.

Then, in a Herdr workspace containing the Git repository you want to change:

```sh
herdr plugin action invoke create --plugin dev.herdr.sandbox
herdr plugin action invoke start-codex --plugin dev.herdr.sandbox
herdr plugin action invoke inspect --plugin dev.herdr.sandbox
herdr plugin action invoke fetch --plugin dev.herdr.sandbox
```

`create` generates a worker ID and clones the workspace repository.
`start-codex` opens Codex in that disposable clone. With exactly one worker,
`start-codex`, `inspect`, and `fetch` select it automatically; when several
workers exist, configure the action's `id` in `config.toml`.

Review the fetched commit, changed files, and diff. To integrate it, configure
the exact 40-character SHA reported by `fetch`:

```toml
[cherry_pick]
id = "WORKER-001"
commit = "0123456789abcdef0123456789abcdef01234567"
```

```sh
herdr plugin action invoke cherry-pick --plugin dev.herdr.sandbox
```

Cherry-pick rejects a dirty source repository and commits outside the selected
worker's fetched range. After fetching anything worth keeping, configure
`[destroy].id` and remove the worker:

```sh
herdr plugin action invoke destroy --plugin dev.herdr.sandbox
```

List durable worker state at any time:

```sh
herdr plugin action invoke list --plugin dev.herdr.sandbox
```

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

## Advanced use and troubleshooting

For noninteractive automation, configure `[execute].id` and `[execute].prompt`
then invoke `execute`. You may configure action-specific IDs for `fetch`,
`inspect`, or `open` when multiple workers exist. Direct script use requires
Herdr's own paths; do not invent storage locations:

```sh
HERDR_PLUGIN_CONFIG_DIR=/path/to/config HERDR_PLUGIN_STATE_DIR=/path/to/state \
python3 bin/herdr-sandbox create --id WORKER-001 --repo /path/to/source-repo --base main
```

- **Podman unavailable or running as root:** repair rootless Podman and check
  `podman info --format '{{.Host.Security.Rootless}}'` returns `true`.
- **Worker image missing:** run `bootstrap` or `build-image`, then `check`.
- **Codex authentication missing:** rerun the dedicated `CODEX_HOME=...` login;
  never copy credentials from `~/.codex`.
- **Worker failed or stale:** use `inspect` and `list`; its files remain until
  you explicitly destroy it.
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
