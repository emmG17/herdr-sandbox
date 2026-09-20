# Herdr Sandbox Workers

This plugin provides setup, image build, readiness, worker creation, listing, inspection, and safe destruction actions. Other worker lifecycle actions are tracked in separate Beads issues.

## Requirements and setup

Use Linux with Git, Python 3.11 or newer, and rootless Podman. On Arch Linux, install `git`, `python`, and `podman` with pacman, then configure rootless Podman for your user. Verify `podman info --format '{{.Host.Security.Rootless}}'` prints `true`.

```sh
herdr plugin link /path/to/herdr-sandbox
herdr plugin action list --plugin dev.herdr.sandbox
herdr plugin action invoke setup --plugin dev.herdr.sandbox
herdr plugin action invoke build-image --plugin dev.herdr.sandbox
herdr plugin action invoke check --plugin dev.herdr.sandbox
herdr plugin action invoke create --plugin dev.herdr.sandbox
herdr plugin action invoke list --plugin dev.herdr.sandbox
herdr plugin action invoke fetch --plugin dev.herdr.sandbox
herdr plugin action invoke cherry-pick --plugin dev.herdr.sandbox
herdr plugin action invoke destroy --plugin dev.herdr.sandbox
herdr plugin action invoke open --plugin dev.herdr.sandbox
herdr plugin action invoke execute --plugin dev.herdr.sandbox
```

The setup action writes `config.toml` and creates a dedicated Codex home inside Herdr's plugin config directory. It leaves an existing config alone. The image build action explicitly builds `Containerfile` as `localhost/herdr-codex-worker:latest`, or the image name in `config.toml`. Local `plugin link` does not run build steps.

To authenticate, use the Codex home path printed by setup. For the default config:

```sh
CODEX_HOME="$(herdr plugin config-dir dev.herdr.sandbox)/codex" codex login --device-auth
```

For an execution readiness check, which also checks for dedicated Codex authentication:

```sh
HERDR_PLUGIN_CONFIG_DIR=/path/from/HERDR_PLUGIN_CONFIG_DIR \
HERDR_PLUGIN_STATE_DIR=/path/from/HERDR_PLUGIN_STATE_DIR \
python3 bin/herdr-sandbox check --execution --repo /path/to/source-repo
```

When invoked by Herdr, config and state paths come from `HERDR_PLUGIN_CONFIG_DIR` and `HERDR_PLUGIN_STATE_DIR`. The action form supplies them automatically and checks the current workspace as a Git source when Herdr provides one. Use setup output to find the current paths for a manual check.

## Create a worker

The `create` action uses the current Herdr Git workspace and its `HEAD`. Herdr actions run without interactive input, so the action generates a unique worker ID by default. To choose an ID, source, and base ref through the action, add an optional `[create]` table to the plugin's `config.toml` (find it with `herdr plugin config-dir dev.herdr.sandbox`):

```toml
[create]
id = "TEST-001"
source_repo = "/path/to/source-repo"
base_ref = "main"
```

Then run `herdr plugin action invoke create --plugin dev.herdr.sandbox`. Change or remove the configured ID before creating another worker; duplicates fail safely. Omit `source_repo` to use the current workspace, and omit `base_ref` to use `HEAD`. For a one-off creation without changing config, run the same plugin entry point directly with Herdr's plugin paths:

```sh
HERDR_PLUGIN_CONFIG_DIR=/path/from/HERDR_PLUGIN_CONFIG_DIR \
HERDR_PLUGIN_STATE_DIR=/path/from/HERDR_PLUGIN_STATE_DIR \
python3 bin/herdr-sandbox create --id TEST-001 --repo /path/to/source-repo --base main
```

The worker lives at `$HERDR_PLUGIN_STATE_DIR/workers/TEST-001/`, with an independent `repo/.git` and `worker.json` recording its source, base commit, branch, and creation time. The new branch is `agent/TEST-001`. IDs and base refs are checked before the clone is created; a duplicate ID is rejected. Creation requires the same setup readiness checks as `check`, including the rootless Podman image, but does not start a container.

## Open a worker

`open` starts the default interactive worker session in the pane Herdr creates for the action. Configure the target because plugin actions are fixed commands and do not read an interactive prompt:

```toml
[open]
id = "TEST-001"
```

Then invoke `herdr plugin action invoke open --plugin dev.herdr.sandbox`. The pane attaches to `podman run -it`, so its shell or Codex session accepts input and remains visible through Herdr. Direct use supports an explicit command and Codex arguments:

```sh
python3 bin/herdr-sandbox open TEST-001 -- codex --model gpt-5
```

The shared runtime reads `[runtime].network` (`online` or `offline`) and `[resources]` from `config.toml`. Offline adds `--network=none`; online uses normal rootless Podman networking. It always uses the configured image and limits, `--userns=keep-id`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, and a read-only container root. The only host bind mounts are the worker repo at `/workspace` and the dedicated Codex home at `/codex`, both read-write. The runtime does not accept privilege, host namespace, socket, home, source repository, or arbitrary host-environment options.

## Execute a task

`execute` runs a supplied Codex prompt noninteractively in an existing worker. It uses the same worker repository, dedicated Codex home, and hardened Podman runtime as `open`, but deliberately allocates no TTY and closes stdin. Configure the fixed action inputs in `config.toml`:

```toml
[execute]
id = "TEST-001"
prompt = "Implement the requested change and run the relevant tests."
```

Then invoke it through Herdr:

```sh
herdr plugin action invoke execute --plugin dev.herdr.sandbox
```

For direct use, pass both inputs explicitly:

```sh
python3 bin/herdr-sandbox execute TEST-001 "Implement the requested change and run the relevant tests."
```

The action requires authentication in the dedicated Codex home, preserves edits in the worker clone, streams Codex stdout/stderr to the Herdr action output, and finishes with the exit status, worker ID, Git HEAD, dirty state, and Git status. A nonzero Codex exit code leaves the worker in `failed` state while retaining its files for inspection or recovery.

## List and inspect workers

The `list` action reads every persisted worker directory after a restart and combines `worker.json` with the current worker clone's Git HEAD, branch, dirty state, and the `herdr-WORKER_ID` Podman container state. It reports missing metadata, missing repositories, missing source repositories, and stale lifecycle state instead of silently dropping an entry:

```sh
HERDR_PLUGIN_CONFIG_DIR=/path/from/HERDR_PLUGIN_CONFIG_DIR \
HERDR_PLUGIN_STATE_DIR=/path/from/HERDR_PLUGIN_STATE_DIR \
python3 bin/herdr-sandbox list
```

For machine-readable output, add `--json`. The same option is available on `inspect`:

```sh
python3 bin/herdr-sandbox inspect TEST-001
```

The inspect output includes ordinary `git log`, `git status`, and `git diff BASE...HEAD` output. Since Herdr actions do not prompt for arbitrary arguments, configure an inspection target in the plugin config when invoking the action:

```toml
[inspect]
id = "TEST-001"
```

Then run `herdr plugin action invoke inspect --plugin dev.herdr.sandbox`. The worker state remains the source of the display, while the filesystem, Git, and Podman checks identify stale or missing reality.

## Fetch a worker commit for review

Fetching is an explicit, fetch-only integration step: it writes the worker branch to the source repository's `FETCH_HEAD`, reports the fetched commit, changed files, and diff stat, and does not merge or cherry-pick it. Configure the worker ID before invoking the fixed action:

```toml
[fetch]
id = "TEST-001"
```

```sh
herdr plugin action invoke fetch --plugin dev.herdr.sandbox
```

The fetched commit remains available through `FETCH_HEAD` after the disposable worker is destroyed; deciding whether to integrate it is a later, deliberate action.

## Cherry-pick a reviewed worker commit

Cherry-picking is separate from fetching and is never automatic. After reviewing a fetched commit, configure both the worker and its full 40-character commit SHA:

```toml
[cherry_pick]
id = "TEST-001"
commit = "0123456789abcdef0123456789abcdef01234567"
```

Then run `herdr plugin action invoke cherry-pick --plugin dev.herdr.sandbox`. The action only accepts a commit in the fetched worker change range and refuses a dirty source repository. On a conflict it leaves Git's cherry-pick state intact and prints the exact `git cherry-pick --continue` and `git cherry-pick --abort` recovery commands.

## Destroy a worker

Destroying a worker first runs `podman rm -f herdr-WORKER_ID`, which stops and removes its container, and only then removes that worker's disposable directory under `$HERDR_PLUGIN_STATE_DIR/workers/`. A missing container or already-removed worker is harmless; an unknown Podman failure stops cleanup so the worker files remain available for recovery. The command never reads from or modifies the source repository.

For a direct, deliberate destroy command, pass the worker ID explicitly:

```sh
HERDR_PLUGIN_CONFIG_DIR=/path/from/HERDR_PLUGIN_CONFIG_DIR \
HERDR_PLUGIN_STATE_DIR=/path/from/HERDR_PLUGIN_STATE_DIR \
python3 bin/herdr-sandbox destroy --id TEST-001
```

To invoke the Herdr action without interactive input, configure the target in the plugin config:

```toml
[destroy]
id = "TEST-001"
```

Then run `herdr plugin action invoke destroy --plugin dev.herdr.sandbox`. Worker IDs are validated before any container or filesystem operation, and symlinked worker storage is rejected so cleanup cannot escape the plugin's worker directory.

## Herdr API notes

Verified with Herdr 0.9.0: actions are manifest entries with argv commands; `plugin link` registers them without running a build; Herdr creates plugin config and state directories but leaves their contents to this plugin. There is no separate plugin storage API. The manifest declares 0.9.0 as its verified minimum.

## Isolation

The worker design mounts only a disposable worker repository and this dedicated plugin Codex home. It does not mount your normal `~/.codex`, SSH keys, cloud credentials, or the Podman socket. `danger-full-access` applies inside the container. Rootless Podman is the outer security boundary. Containers share the host Linux kernel and do not provide VM-level isolation.

Before using worker execution, run the documented [manual disposable-worker security test](docs/manual-worker-security-test.md). It verifies expected workspace access and denied access to host home, credentials, sockets, host processes, privilege escalation, and writes outside `/workspace`.

If readiness fails, follow the reported command or path. A Podman error from `podman info` means rootless Podman itself needs repair before the image can be built or used.

The four root-level `herdr-worker-*` scripts are historical prototypes and are not called by this plugin.
