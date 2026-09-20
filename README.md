# Herdr Sandbox Workers

This is the setup foundation for isolated Codex workers. It currently registers setup, image build, and readiness actions. Worker lifecycle actions are tracked in separate Beads issues.

## Requirements and setup

Use Linux with Git, Python 3.11 or newer, and rootless Podman. On Arch Linux, install `git`, `python`, and `podman` with pacman, then configure rootless Podman for your user. Verify `podman info --format '{{.Host.Security.Rootless}}'` prints `true`.

```sh
herdr plugin link /path/to/herdr-sandbox
herdr plugin action list --plugin dev.herdr.sandbox
herdr plugin action invoke setup --plugin dev.herdr.sandbox
herdr plugin action invoke build-image --plugin dev.herdr.sandbox
herdr plugin action invoke check --plugin dev.herdr.sandbox
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

## Herdr API notes

Verified with Herdr 0.9.0: actions are manifest entries with argv commands; `plugin link` registers them without running a build; Herdr creates plugin config and state directories but leaves their contents to this plugin. There is no separate plugin storage API. The manifest declares 0.9.0 as its verified minimum.

## Isolation

The worker design mounts only a disposable worker repository and this dedicated plugin Codex home. It does not mount your normal `~/.codex`, SSH keys, cloud credentials, or the Podman socket. `danger-full-access` applies inside the container. Rootless Podman is the outer security boundary. Containers share the host Linux kernel and do not provide VM-level isolation.

Before using future worker execution, test a disposable worker by asking it to delete its own workspace, inspect host home and SSH or cloud credentials, reach Podman or Docker, write outside `/workspace`, and escalate privileges. Expected result: it can affect its disposable workspace but cannot reach those host resources.

If readiness fails, follow the reported command or path. A Podman error from `podman info` means rootless Podman itself needs repair before the image can be built or used.

The four root-level `herdr-worker-*` scripts are historical prototypes and are not called by this plugin.
