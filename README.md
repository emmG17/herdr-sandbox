# Herdr Sandbox Workers

`dev.herdr.sandbox` creates disposable Git clones and runs Codex in a hardened,
rootless Podman container. Herdr remains the control plane; this plugin only
provides worker lifecycle actions.

## Threat model and isolation

The worker can read, write, delete, install dependencies, run commands, and
commit **inside its disposable clone**. `danger-full-access` applies inside the
container; rootless Podman is the outer security boundary.

The runtime mounts only the worker repository at `/workspace` and the dedicated
plugin Codex home at `/codex`. It does not mount the source repository, the
normal `~/.codex`, host home, SSH/GPG/cloud credentials, or Podman/Docker
sockets. It also fixes `--userns=keep-id`, drops all capabilities, enables
`no-new-privileges`, and uses a read-only container root.

This is container isolation, not a VM: containers share the host Linux kernel.
Do not use it for work that requires a hostile-code boundary or protection from
kernel vulnerabilities. Workers have implementation authority only; fetching,
reviewing, and cherry-picking their commits is always explicit.

## Arch Linux and rootless Podman

Use Linux, Git, Python 3.11+, a current Codex CLI, and rootless Podman. On Arch:

```sh
sudo pacman -S --needed git python podman shadow fuse-overlayfs
podman info --format '{{.Host.Security.Rootless}}'
```

The last command must print `true`. Rootless Podman needs a range for your user
in both `/etc/subuid` and `/etc/subgid`. If your administrator has not already
allocated one, they can use the documented mapping setup (substitute your login
name, and do not add a range that overlaps another account):

```sh
sudo usermod --add-subuids 10000-75535 USERNAME
sudo usermod --add-subgids 10000-75535 USERNAME
podman unshare cat /proc/self/uid_map /proc/self/gid_map
```

Start a new login session after changing mappings. `shadow` supplies the mapping
helpers and `fuse-overlayfs` gives rootless storage better performance. Never
run this plugin through `sudo`.

## Install or link the plugin

The root-level `herdr-plugin.toml` makes this repository directly compatible
with Herdr's GitHub installer. After publishing the repository, install it with:

```sh
herdr plugin install OWNER/herdr-sandbox
```

There is intentionally no publishing automation. During local development, link
the checkout instead:

```sh
herdr plugin link /path/to/herdr-sandbox
herdr plugin list --plugin dev.herdr.sandbox
herdr plugin action list --plugin dev.herdr.sandbox
```

Linking only registers the manifest; it does not build an image. Do not install
over a local link: unlink the local plugin first if changing installation mode.

## Set up, build, and authenticate

```sh
herdr plugin action invoke setup --plugin dev.herdr.sandbox
herdr plugin action invoke build-image --plugin dev.herdr.sandbox
CODEX_HOME="$(herdr plugin config-dir dev.herdr.sandbox)/codex" codex login --device-auth
herdr plugin action invoke check --plugin dev.herdr.sandbox
```

`setup` preserves an existing `config.toml`, creates plugin-owned config/state
directories, and creates the dedicated Codex home. `build-image` builds the
`Containerfile` as `localhost/herdr-codex-worker:latest` unless the configured
image name differs. Authentication belongs in the dedicated home, never the
normal user Codex home.

## Online and offline profiles

Edit the plugin configuration at:

```sh
herdr plugin config-dir dev.herdr.sandbox
```

The default profile permits normal rootless container networking, which Codex
and dependency installation typically need:

```toml
[runtime]
network = "online"
```

For a fully disconnected task, switch before creating or opening the worker:

```toml
[runtime]
network = "offline"
```

Offline maps to Podman's `--network=none`. It prevents network access but also
prevents package downloads and remote model/API access, so use it only when the
worker image and task are self-contained. CPU, memory, and PID limits live in
the `[resources]` table; isolation flags and mount choices are not configurable.

## End-to-end worker workflow

Run these actions in a Herdr workspace whose current directory is the clean Git
source repository. Actions have fixed commands and do not prompt, so configure
the worker inputs in the plugin `config.toml`:

```toml
[create]
id = "TEST-001"
# Omit source_repo to use Herdr's current workspace.
# source_repo = "/path/to/source-repo"
# Omit base_ref to use HEAD.
# base_ref = "main"

[execute]
id = "TEST-001"
prompt = "Implement the requested change and run the relevant tests."
```

Then create, execute, inspect, and fetch the candidate change:

```sh
herdr plugin action invoke create --plugin dev.herdr.sandbox
herdr plugin action invoke execute --plugin dev.herdr.sandbox
herdr plugin action invoke inspect --plugin dev.herdr.sandbox
herdr plugin action invoke fetch --plugin dev.herdr.sandbox
```

Creation validates the source and base ref, makes an independent clone under
the plugin state directory, and creates branch `agent/TEST-001`. `execute`
has no TTY and closes stdin; it streams Codex output, preserves the worker files
on failure, and reports the exit status, HEAD, and dirty state. `fetch` writes
only `FETCH_HEAD` in the source repository; it never merges or cherry-picks.

To list durable worker state after a restart, run:

```sh
herdr plugin action invoke list --plugin dev.herdr.sandbox
```

For one-off direct use, explicitly provide Herdr's plugin paths instead of
inventing storage paths:

```sh
HERDR_PLUGIN_CONFIG_DIR=/path/to/config HERDR_PLUGIN_STATE_DIR=/path/to/state \
python3 bin/herdr-sandbox create --id TEST-001 --repo /path/to/source-repo --base main
```

## Interactive use

To open a shell or a Codex session in a Herdr-managed pane, configure an ID:

```toml
[open]
id = "TEST-001"
```

```sh
herdr plugin action invoke open --plugin dev.herdr.sandbox
```

The default is an interactive `bash` session. Direct invocation also accepts a
specific command, for example:

```sh
python3 bin/herdr-sandbox open TEST-001 -- codex --model gpt-5
```

## Integrate a reviewed change

Review the fetched commit and diff before integration. Then set the exact,
40-character SHA reported by `fetch`:

```toml
[cherry_pick]
id = "TEST-001"
commit = "0123456789abcdef0123456789abcdef01234567"
```

```sh
herdr plugin action invoke cherry-pick --plugin dev.herdr.sandbox
```

The action rejects a dirty source repository and a commit outside that worker's
fetched change range. On a conflict it leaves Git's cherry-pick state intact and
prints the precise `git cherry-pick --continue` and `--abort` recovery commands.

## Destroy a worker

After fetching any commit you want to preserve, destroy the disposable worker:

```toml
[destroy]
id = "TEST-001"
```

```sh
herdr plugin action invoke destroy --plugin dev.herdr.sandbox
```

Destruction removes `herdr-TEST-001` with Podman and then only that worker's
directory in plugin state. It never changes the source repository. A missing
worker/container is safe; an unexpected Podman failure preserves worker files
for recovery.

## Security test

Before relying on task execution, follow the
[manual disposable-worker security test](docs/manual-worker-security-test.md)
against a clean, disposable worker. It confirms intended workspace access and
denied access to host credentials, container-engine sockets, host processes,
privilege escalation, and writes outside `/workspace`. If any denied check
succeeds, stop using the plugin, retain the worker for investigation, and do
not fetch or integrate its work.

## Troubleshooting

- **`Podman is running as root` or `Podman is unavailable`:** repair rootless
  Podman, then confirm `podman info --format '{{.Host.Security.Rootless}}'` is
  `true`.
- **Worker image missing:** run `build-image` after `setup`, then rerun `check`.
- **Dedicated Codex authentication missing:** run the `CODEX_HOME=... codex
  login --device-auth` command above; do not copy normal-home credentials.
- **Create cannot find a source repository:** invoke it from a Herdr Git
  workspace or set `[create].source_repo` to that repository's root.
- **A worker is failed or stale:** use `inspect` and `list`; files remain until
  explicit destruction. An execute failure is not an instruction to destroy it.
- **Network-required work fails offline:** use the online profile only when the
  task needs remote model access or package downloads.

The root-level `herdr-worker-*` scripts are historical prototypes and are not
called by this plugin.
