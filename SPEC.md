# Herdr Sandbox Plugin

**Status:** Draft / implementation-ready
**Working name:** `herdr-sandbox`
**Target platform:** Linux
**Initial runtime:** Rootless Podman
**Initial coding agent:** OpenAI Codex
**Primary goal:** Safely run autonomous Codex workers with no interactive command approvals while retaining Herdr as the control plane.

## 1. Problem

The current development workflow uses:

* Herdr as the workspace/control room
* Codex as the coding agent
* Beads (`bd`) as the project task/context tracker

There are two common workflows:

### Single-shot

A sufficiently bounded task is assigned directly to one Codex session.

### Multi-shot

A larger task is decomposed into vertical slices represented in Beads.

A typical workflow is:

1. Sol orchestrates/decomposes.
2. Individual slices are assigned to Luna workers.
3. Workers implement slices independently.
4. Sol performs final integration/review.

The current limitation is that Codex frequently requires interactive command approval.

The desired model is instead:

```text
Herdr
  │
  ├── trusted orchestration
  │
  └── isolated worker
        │
        ├── disposable repository clone
        ├── rootless Podman container
        └── Codex
             sandbox=danger-full-access
             approval_policy=never
```

Podman, rather than Codex's internal sandbox, is the security boundary.

## 2. Design principles

The implementation MUST follow these principles.

### 2.1 Herdr remains the control plane

Do not create a replacement orchestration system.

The plugin extends Herdr with isolated worker execution.

### 2.2 Workers are disposable

Workers MUST NOT operate directly on the source repository.

Each worker receives an independent local clone.

Destroying a worker must be safe.

### 2.3 Workers have implementation authority, not integration authority

A worker may:

* read/write/delete anything inside its disposable repository
* install dependencies inside its environment
* execute arbitrary commands inside its container
* run tests
* create Git commits

A worker MUST NOT automatically receive:

* host filesystem access
* source repository write access
* SSH credentials
* GPG credentials
* AWS credentials
* Kubernetes credentials
* Podman socket
* Docker socket
* production credentials
* GitHub write credentials

### 2.4 Integration is explicit

Completed worker commits are fetched into the source repository and reviewed/integrated separately.

Workers MUST NOT directly modify or merge into the source repository.

### 2.5 Runtime and workspace implementation must be abstractable

Podman and Git clones are the initial implementations, not permanent architectural requirements.

Use interfaces approximately equivalent to:

```text
WorkspaceProvider
├── create()
├── destroy()
├── path()
└── metadata()

SandboxRuntime
├── runInteractive()
├── execute()
├── stop()
└── inspect()
```

Initial implementations:

```text
WorkspaceProvider
└── GitCloneWorkspaceProvider

SandboxRuntime
└── PodmanSandboxRuntime
```

Future implementations may include:

```text
BtrfsSnapshotWorkspaceProvider
MicroVMSandboxRuntime
RemoteSandboxRuntime
```

Do NOT implement these future providers now.

## 3. Existing prototype

Four shell scripts already exist and demonstrate the desired behavior:

```text
herdr-worker-create
herdr-worker-run
herdr-worker-exec
herdr-worker-destroy
```

The implementation should inspect these existing scripts and preserve their proven behavior where appropriate.

Do not blindly wrap the scripts.

Extract their behavior into the plugin implementation and treat the scripts as the behavioral prototype/reference.

## 4. Initial technology choice

Prefer a small implementation with minimal dependencies.

Recommended implementation:

```text
Bash
```

or, if state/config handling becomes meaningfully cleaner:

```text
Python 3 standard library
```

Do NOT introduce:

* Node.js application frameworks
* databases
* background daemons
* web servers
* Kubernetes
* Docker Compose
* additional orchestration platforms

The plugin should remain a thin adapter around:

```text
Herdr + Git + Podman + Codex
```

## 5. Plugin structure

Target approximately:

```text
herdr-sandbox/
├── herdr-plugin.toml
├── README.md
├── bin/
│   └── herdr-sandbox
├── lib/
│   ├── workspace.*
│   ├── podman.*
│   └── worker.*
└── tests/
```

Exact organization may change if implementation language makes another layout more appropriate.

## 6. Plugin identity

Suggested plugin ID:

```text
dev.herdr.sandbox
```

Suggested display name:

```text
Sandbox Workers
```

The manifest MUST declare a valid `min_herdr_version`.

Determine the appropriate version from the installed Herdr CLI/documentation rather than guessing.

## 7. Worker storage

Plugin state MUST use Herdr's plugin state/config mechanisms where appropriate.

Do not hardcode the prototype path:

```text
~/.local/share/herdr/workers
```

if Herdr exposes an authoritative plugin state directory.

Herdr injects plugin runtime paths including:

```text
HERDR_PLUGIN_CONFIG_DIR
HERDR_PLUGIN_STATE_DIR
HERDR_PLUGIN_ROOT
```

Use these where appropriate.

Worker state should conceptually look like:

```text
$HERDR_PLUGIN_STATE_DIR/
└── workers/
    └── TEST-001/
        ├── repo/
        └── worker.json
```

`worker.json` should contain enough metadata to understand and recover the worker, such as:

```json
{
  "id": "TEST-001",
  "source_repo": "/home/user/code/project",
  "base_ref": "main",
  "branch": "agent/TEST-001",
  "created_at": "...",
  "runtime": "podman",
  "status": "created"
}
```

Do not duplicate state that can reliably be derived.

## 8. Codex state

Codex credentials/configuration must remain separate from worker repositories.

Use a dedicated Codex home such as:

```text
$HERDR_PLUGIN_CONFIG_DIR/codex
```

or another appropriate persistent plugin-owned location.

Workers receive:

```text
CODEX_HOME=/codex
```

with that dedicated directory mounted into the container.

Never mount the user's normal:

```text
~/.codex
```

directory.

The implementation should provide clear setup instructions for:

```bash
codex login --device-auth
```

against the dedicated Codex home.

Do NOT attempt to automate interactive authentication.

## 9. Worker creation

Provide a Herdr plugin action equivalent to:

```text
Create Sandbox Worker
```

Inputs or context should resolve:

```text
worker ID
source repository
base ref
```

Where possible, infer the source repository from the Herdr workspace context.

The underlying behavior is approximately:

```bash
git clone \
  --local \
  --no-hardlinks \
  SOURCE_REPO \
  WORKER/repo
```

Then:

```bash
git switch --detach BASE_REF
git switch -c agent/WORKER_ID
```

The worker clone MUST have independent Git metadata.

Do not use Git worktrees in v1.

## 10. Interactive worker

Interactive is the default worker session mode. Opening a worker from Herdr
should open a Herdr-managed terminal pane attached to the sandbox, with a shell
or Codex session that accepts input and displays progress. This default applies
when a user opens a worker; it does not make plugin actions prompt for input.

Expose an action/pane allowing:

```text
Open Sandbox
```

This starts an interactive shell or Codex session in the worker.

Equivalent runtime behavior:

```text
podman run
  --rm
  -it
  --userns=keep-id
  --cap-drop=ALL
  --security-opt=no-new-privileges
  --read-only
  --pids-limit=<configured>
  --memory=<configured>
  --cpus=<configured>
  ...
```

The worker repository is mounted:

```text
/workspace RW
```

The dedicated Codex home is mounted:

```text
/codex RW
```

The container root filesystem is read-only.

Interactive mode SHOULD be opened as a Herdr-managed plugin pane so that it remains visible and controllable through Herdr.

## 11. Noninteractive execution

Noninteractive is an explicit task execution mode for a supplied prompt. It
uses the same worker repository, dedicated Codex home, and Podman security
settings as interactive mode. Both modes operate on the same worker rather
than creating separate classes of workers.

Expose an action equivalent to:

```text
Execute Sandbox Task
```

It runs:

```text
codex exec
```

inside the sandbox.

The process MUST NOT allocate a TTY.

stdin MUST be closed.

Equivalent behavior:

```text
codex exec \
  --sandbox danger-full-access \
  PROMPT < /dev/null
```

This avoids Codex waiting with:

```text
Reading additional input from stdin...
```

The execution result should expose:

* exit status
* worker ID
* Git status
* HEAD commit
* whether the repository is dirty
* useful Codex output/log location

Do not parse human-oriented Codex terminal output unless necessary.

Prefer structured output when Codex provides an appropriate stable mechanism.

## 12. Sandbox security requirements

Every worker container MUST:

* use rootless Podman
* run without `--privileged`
* drop all capabilities
* enable `no-new-privileges`
* use a read-only root filesystem
* use resource limits
* mount only explicitly permitted paths
* never mount the Podman socket
* never mount the Docker socket
* never mount the user's home directory
* never mount the source repository
* never inherit arbitrary host environment variables

Forbidden options include:

```text
--privileged
--pid=host
--network=host
--userns=host
```

Forbidden mounts include:

```text
/
$HOME
~/.ssh
~/.gnupg
~/.aws
~/.kube
~/.config
/run/podman/podman.sock
/var/run/docker.sock
```

These rules MUST be enforced centrally by `PodmanSandboxRuntime`, not individually by callers.

## 13. Network profiles

Implement two profiles in v1.

### online

Normal rootless Podman networking.

Used when workers need:

* package installation
* dependency resolution
* public documentation
* other Internet access

### offline

Use:

```text
--network=none
```

Useful for:

* review
* refactoring
* static analysis
* tests that need no external resources

Do NOT implement domain-level network allowlisting in v1.

## 14. Resource profiles

Provide configurable defaults, approximately:

```toml
[resources]
cpus = 4
memory = "8g"
pids = 1024
```

Do not assume these exact values are appropriate for every machine.

Configuration belongs in the plugin config directory.

## 15. Worker lifecycle

Model explicit states approximately as:

```text
created
running
stopped
completed
failed
```

Avoid building an elaborate state machine.

Filesystem/process reality remains authoritative.

State exists primarily for UI and recovery.

## 16. List workers

Provide an action or command:

```text
List Sandbox Workers
```

Display at least:

```text
ID
repository
base ref
branch
status
created
HEAD
dirty
```

Where feasible, indicate whether a corresponding Podman container is currently running.

## 17. Inspect worker

Provide:

```text
Inspect Sandbox Worker
```

Useful output:

```text
Worker: BD-123
Source: ~/code/project
Base: main
Branch: agent/BD-123
Runtime: podman
Network: online
Status: completed
HEAD: abc123
Dirty: no
```

Also expose:

```text
git log
git status
git diff BASE...HEAD
```

through appropriate commands/actions rather than attempting to build a custom Git UI.

## 18. Integration

Provide an explicit action:

```text
Integrate Sandbox Worker
```

Integration MUST happen from outside the worker container.

The implementation should approximately perform:

```bash
cd SOURCE_REPO

git fetch \
  WORKER_REPO \
  agent/WORKER_ID
```

Do NOT automatically merge/cherry-pick in v1 unless explicitly requested by the user.

The default integration action should leave the result available as:

```text
FETCH_HEAD
```

and report:

```text
commit
changed files
diff stat
```

The user or review agent can then inspect and integrate it.

Optional explicit follow-up actions may include:

```text
Cherry-pick Worker
```

but this must be a separate deliberate operation.

## 19. Destroy worker

Expose:

```text
Destroy Sandbox Worker
```

It must:

1. Stop/remove any running worker container.
2. Remove the disposable worker directory.
3. Remove plugin worker metadata.

It MUST NOT modify the source repository.

Destruction should be idempotent where practical.

## 20. Herdr integration

Use Herdr's actual plugin system.

The plugin should use:

```text
herdr-plugin.toml
```

and declarative plugin actions/panes.

Call Herdr using:

```text
$HERDR_BIN_PATH
```

rather than assuming `herdr` is on `PATH`.

Use injected context such as:

```text
HERDR_WORKSPACE_ID
HERDR_PANE_ID
HERDR_PLUGIN_ROOT
HERDR_PLUGIN_CONFIG_DIR
HERDR_PLUGIN_STATE_DIR
HERDR_PLUGIN_CONTEXT_JSON
```

where appropriate.

Do not implement a custom Herdr socket client unless the CLI cannot provide a required capability.

## 21. Herdr UI

Keep UI intentionally small.

Desired actions:

```text
Sandbox: Create Worker
Sandbox: Open Worker
Sandbox: Execute Task
Sandbox: Inspect Worker
Sandbox: Integrate Worker
Sandbox: Destroy Worker
```

Interactive worker sessions should preferably appear as normal Herdr-managed terminal panes.

The primary worker action is **Open Worker** (interactive). **Execute Task**
selects noninteractive execution explicitly. Herdr plugin actions themselves
are fixed, non-prompting commands, so the mode is selected by the action or
entrypoint rather than by trying to read from an action's stdin.

The plugin should feel like Herdr gained a new worker capability, not like another orchestration product was embedded inside it.

## 22. Beads

Do NOT tightly couple the plugin to Beads in v1.

Worker IDs may naturally be Beads IDs:

```text
BD-123
```

but the sandbox plugin should accept arbitrary worker IDs.

Architecture:

```text
Beads
   │
   │ optionally provides task IDs
   ▼
Herdr orchestration
   │
   ▼
Sandbox plugin
```

not:

```text
Sandbox plugin → hard dependency on Beads
```

A later orchestration plugin/feature can compose Beads with sandbox workers.

## 23. Codex models

Do not hardcode Sol/Luna selection into the sandbox layer.

The caller should be able to pass Codex/model arguments.

The sandbox layer owns:

```text
where the agent executes
```

not:

```text
which reasoning strategy the orchestrator uses
```

## 24. Configuration

Provide a small plugin configuration file, approximately:

```toml
[runtime]
engine = "podman"
image = "localhost/herdr-codex-worker:latest"
network = "online"

[resources]
cpus = 4
memory = "8g"
pids = 1024

[codex]
home = "codex"
```

Avoid configuration for hypothetical future functionality.

## 25. Container image

Include a `Containerfile` in the plugin.

Base image may be:

```text
node:22-bookworm-slim
```

Install at least:

```text
git
curl
wget
jq
ripgrep
ca-certificates
build-essential
python3
python3-pip
python3-venv
unzip
procps
pnpm
@openai/codex
```

Provide an explicit setup/build action or documented command.

Do not silently rebuild the image for every worker.

## 26. Preflight checks

Before worker creation/execution, validate:

```text
Linux
podman exists
Podman is rootless
git exists
source is a Git repository
container image exists
Codex home exists
```

For execution, additionally validate that Codex authentication appears configured.

Errors should be actionable.

Example:

```text
Codex authentication is not configured.

Run:
  herdr plugin config-dir dev.herdr.sandbox

Then authenticate the dedicated Codex home using the documented setup command.
```

Do not fall back to mounting the user's normal Codex credentials.

## 27. Security test

Include an explicit manual security test documented in README.

A disposable worker should be instructed to attempt, without exploiting unknown vulnerabilities:

* deleting its workspace
* accessing host home
* finding SSH credentials
* finding cloud credentials
* accessing Podman/Docker
* writing outside `/workspace`
* escalating privileges
* inspecting host processes

Expected result:

```text
workspace         destroyable
container         destroyable
source repository unaffected
host home         inaccessible
SSH credentials   inaccessible
cloud credentials inaccessible
container socket  inaccessible
host system       unaffected
```

## 28. Automated tests

At minimum test:

### Workspace

* create clone
* clone uses independent Git metadata
* correct base ref
* correct branch
* destruction leaves source untouched

### Runtime command construction

Assert forbidden options never appear.

Assert required options always appear:

```text
--cap-drop=ALL
--security-opt=no-new-privileges
--read-only
--userns=keep-id
```

### Noninteractive execution

Verify:

* no TTY
* stdin receives EOF
* process exits normally
* file changes persist in worker repository

### Integration

Verify:

* worker commit can be fetched
* source repository is unchanged before explicit integration
* destroying worker after fetch does not destroy fetched commit

### Security regression

Add tests ensuring configuration cannot accidentally request:

```text
privileged=true
host networking
host PID namespace
host user namespace
container-engine socket mounts
```

## 29. Local development workflow

The completed plugin must support:

```bash
herdr plugin link /path/to/herdr-sandbox
```

Then:

```bash
herdr plugin list --plugin dev.herdr.sandbox
```

and expose its actions through:

```bash
herdr plugin action list --plugin dev.herdr.sandbox
```

During development, use `plugin link`; do not require installation from GitHub.

## 30. Distribution

Once stable, the repository should be installable with Herdr's GitHub plugin mechanism:

```bash
herdr plugin install OWNER/herdr-sandbox
```

Do not implement publishing automation in the initial milestone.

## 31. README

Document:

1. Threat model
2. What is and is not isolated
3. Arch Linux prerequisites
4. Rootless Podman setup
5. Container image build
6. Dedicated Codex authentication
7. Plugin linking
8. Creating a worker
9. Interactive use
10. Noninteractive execution
11. Integration
12. Destruction
13. Security test
14. Troubleshooting

Explicitly explain:

> `danger-full-access` applies inside the container. Rootless Podman is the outer security boundary.

Also explain that containers share the host Linux kernel and therefore do not provide VM-level isolation.

## 32. Out of scope for v1

Do NOT implement:

* Btrfs snapshots
* Git worktrees
* microVMs
* Kubernetes
* remote execution
* domain-level network filtering
* automatic PR creation
* automatic merges
* production deployments
* Beads orchestration
* multi-agent DAG scheduling
* web UI
* databases
* background service/daemon
* automatic Codex login
* arbitrary host secret forwarding

These are potential future layers, not v1 requirements.

## 33. Future extension points

Design, but do not implement, extension points for:

```text
WorkspaceProvider
├── GitCloneWorkspaceProvider
└── BtrfsSnapshotWorkspaceProvider

SandboxRuntime
├── PodmanSandboxRuntime
└── MicroVMSandboxRuntime
```

This prevents the architecture from becoming coupled to Btrfs while still allowing Btrfs CoW snapshots later.

## 34. Acceptance criteria

The first milestone is complete when this workflow succeeds:

```text
source repository
       │
       ▼
Herdr plugin: Create Worker
       │
       ▼
independent disposable clone
       │
       ▼
Herdr plugin: Execute Task
       │
       ▼
rootless Podman
       │
       ▼
Codex YOLO
       │
       ├── edits
       ├── arbitrary commands
       ├── tests
       └── commit
              │
              ▼
Herdr plugin: Inspect Worker
              │
              ▼
Herdr plugin: Integrate Worker
              │
              ▼
FETCH_HEAD in source repository
              │
              ▼
review/cherry-pick
              │
              ▼
Herdr plugin: Destroy Worker
```

Throughout the process:

```text
source repo      protected
host home        protected
host credentials protected
Podman host      protected
worker repo      disposable
```

## 35. Implementation approach

Implement incrementally.

### Milestone 1

* manifest
* configuration
* preflight
* create
* list
* inspect
* destroy

### Milestone 2

* interactive sandbox pane
* noninteractive Codex execution
* logs/results

### Milestone 3

* fetch/integration
* explicit cherry-pick action
* safety regression tests

### Milestone 4

* README
* security test procedure
* cleanup/refactoring
* GitHub-installable packaging

Do not implement later milestones until the preceding lifecycle works end-to-end.

## 36. First task for the coding agent

Before writing implementation code:

1. Inspect the four existing `herdr-worker-*` scripts.
2. Inspect the installed Herdr version.
3. Inspect current Herdr plugin documentation and CLI help.
4. Determine the exact current `herdr-plugin.toml` schema.
5. Produce a short implementation plan.
6. Identify any differences between this specification and the actual installed Herdr plugin API.
7. Prefer the installed/current Herdr behavior over assumptions in this specification.
8. Then implement Milestone 1.

Do not modify the existing prototype scripts during Milestone 1.
