---
name: herdr-sandbox-worker
description: Operates the Herdr Sandbox Workers plugin to have Codex work autonomously in an isolated disposable Git clone, then review and explicitly integrate the result. Use when a task should be delegated to a rootless-Podman sandbox worker, or when creating, running, inspecting, fetching, cherry-picking, or destroying a `dev.herdr.sandbox` worker.
---

# Herdr Sandbox Worker

Use `dev.herdr.sandbox` when the task benefits from an isolated Codex worker. Herdr is the control plane: it creates the clone, starts Codex, exposes evidence for review, and performs any integration. The worker only owns its disposable clone.

## Preconditions

Work from the target Herdr Git workspace. Confirm the host is Linux with rootless Podman, Herdr 0.9+, Git, Python 3.11+, and Codex CLI. The plugin runs as the ordinary user, never via `sudo`.

Install or link the plugin if absent:

```sh
herdr plugin install emmG17/herdr-sandbox
# For development of this plugin itself:
herdr plugin link /path/to/herdr-sandbox
```

For a new installation, prepare and authenticate the plugin-owned Codex home. Never reuse or copy the normal Codex home.

```sh
herdr plugin action invoke bootstrap --plugin dev.herdr.sandbox
CODEX_HOME="$(herdr plugin config-dir dev.herdr.sandbox)/codex" codex login --device-auth
herdr plugin action invoke check --plugin dev.herdr.sandbox
```

`check` must succeed before a worker is created or run. If it reports that the image is missing, run `bootstrap` or `build-image`; if authentication is missing, repeat the dedicated-home login.

## Autonomous work loop

1. Create a worker from the current workspace. Record its ID; with one worker, later actions select it automatically.

   ```sh
   herdr plugin action invoke create --plugin dev.herdr.sandbox
   herdr plugin action invoke list --plugin dev.herdr.sandbox
   ```

2. Start Codex interactively to carry out the task, or use configured noninteractive execution. Give Codex a bounded task and require it to commit its completed change in the worker clone.

   ```sh
   herdr plugin pane open --plugin dev.herdr.sandbox --entrypoint start-codex --placement overlay
   # Noninteractive: configure [execute].id and [execute].prompt in config.toml,
   # then invoke `execute`.
   herdr plugin action invoke execute --plugin dev.herdr.sandbox
   ```

   Noninteractive output is streamed while Codex runs. Each invocation also
   persists `runs/<run-id>/result.json` and `runs/<run-id>/output.log` below the
   worker directory; `list` and `inspect` show a compact summary of the latest
   result. The plugin waits for the Codex child process, but does not invent a
   Herdr 0.9.0 `--wait` or status extension. If the Herdr action invocation is
   asynchronous or does not forward live output, inspect the persisted result
   and log after the run.

3. Inspect the worker before bringing anything back. Validate the commit, status, tests, and diff against the requested task.

   ```sh
   herdr plugin action invoke inspect --plugin dev.herdr.sandbox
   herdr plugin action invoke fetch --plugin dev.herdr.sandbox
   ```

   `fetch` only updates the source repository's `FETCH_HEAD`; it never merges a worker change. Treat the reported full SHA and changed-file list as review evidence.

4. Integrate only an approved full 40-character SHA. Add the selected worker and SHA to the plugin configuration, then invoke the explicit integration action.

   ```toml
   [cherry_pick]
   id = "WORKER-001"
   commit = "0123456789abcdef0123456789abcdef01234567"
   ```

   ```sh
   herdr plugin action invoke cherry-pick --plugin dev.herdr.sandbox
   ```

   The source checkout must be clean. Resolve any cherry-pick conflict in that checkout with Git's normal `cherry-pick --continue` or `--abort` flow.

5. After retaining or discarding the result, select the worker in `[destroy].id` and remove its disposable state.

   ```sh
   herdr plugin action invoke destroy --plugin dev.herdr.sandbox
   ```

## Worker selection and configuration

With multiple workers, configure worker IDs in the plugin config at `$(herdr plugin config-dir dev.herdr.sandbox)/config.toml`: `[fetch]`, `[inspect]`, `[open]`, `[execute]`, `[cherry_pick]`, or `[destroy]`. `[open].id` selects the worker for the interactive `start-codex` pane. Use `list` to recover IDs and lifecycle state.

Herdr 0.9.0 runs plugin actions in their invoking workspace and cannot let an
action dynamically create or move its own pane. Use the manifest `start-codex`
pane entrypoint for an interactive Herdr pane, and invoke it from the selected
worker's source workspace. The plugin refuses a `workspace_cwd` mismatch. The
Podman worker still mounts the selected clone at `/workspace`; interactive
start-codex sessions use a temporary Codex home with only authentication, while
the noninteractive execute action keeps its shared dedicated home.

Set `[runtime].network = "offline"` only for self-contained tasks. Use the default `"online"` mode for model access or dependency installation. Customize project dependencies in the plugin-owned `image/Containerfile` and run `build-image`; the source repository and credentials are deliberately outside that build context.

## Safety boundary

The worker has broad permissions *inside* its container, but only its clone at `/workspace` and the dedicated `/codex` home are mounted. It has no host home, normal Codex home, SSH/GPG/cloud credentials, source-repository write access, or container-engine socket.

Do not treat isolation as a VM boundary: avoid untrusted or hostile workloads. Before relying on a new environment, run the repository's disposable-worker security test. If any access expected to be denied succeeds, preserve the worker, do not fetch or cherry-pick it, and escalate the finding.
