# Security Design: Host Git Operations

## Problem

Worker repositories are mounted read-write at `/workspace` inside containers, so
the worker controls `.git/config`. Host-side `git -C <worker-repo> ...` can
execute arbitrary commands through settings such as:

- `core.fsmonitor`
- `core.hooksPath`
- `core.sshCommand`
- `diff.<driver>.textconv` with `.gitattributes`
- `core.pager`

That breaks the invariant that a worker must not gain write access to the source
repository or host, including indirectly.

## Approach

### Fetch: temporary bare repo + verified bundle

1. Read the worker tip SHA from loose or packed refs (no git; refuse symlinks;
   confine paths under the worker `.git`). The imported worker ref is
   `refs/heads/agent/<id>` (the branch `create_worker` always creates), not
   symbolic `HEAD`.
2. `git init --bare` in a temporary directory.
3. Copy **only** loose objects (`[0-9a-f]{2}/[0-9a-f]{38}`) and
   `pack/pack-*.pack` files. Skip commit-graphs, multi-pack-indexes, bitmaps,
   `.idx`/`.rev`, and `objects/info/*`.
4. Regenerate pack indexes in the temp repo with `git index-pack`.
5. Write the branch tip, `git bundle create`, then `git bundle verify`.
6. `git -C <source> fetch` the bundle with `-c fetch.fsckObjects=true` and
   `-c transfer.fsckObjects=true`, using a single forced refspec
   `+agent/<id>:refs/heads/sandbox/<id>`, plus `--no-tags`,
   `--no-write-fetch-head`, and `--no-recurse-submodules`.

Before step 6, `validate_sandbox_result_branch` requires a concrete
`sandbox/<id>` destination (no wildcards). If that branch is checked out in any
source worktree (`git worktree list --porcelain`), fetch fails with a one-line
message and leaves the worker un-fetched. Never pass `--update-head-ok` or
`--prune`.

Commit counts, changed files, and diff stats are computed only with
`git -C <source>` against `refs/heads/sandbox/<id>` and the recorded
`base_commit`. A zero-commit worker still creates the sandbox ref (tip equals
base) so review commands stay predictable. Uncommitted worker changes, when
detectable via the hardened query container, produce a warning that they are
not in the result.

The source repository is untouched until step 6. Failure or a crash before that
fetch leaves no alternates, no dangling `sandbox/<id>` ref, and no dependency
on the worker object store. Concurrent fetches use distinct temp directories and
git's own source-repo ref locking.

Rejected alternatives: mutating the source repo via `objects/info/alternates`
plus `repack -a -d`, and trusting worker-supplied pack indexes / commit-graphs.

### Ref and HEAD reads

`_safe_read_worker_ref`, `_safe_read_worker_head`, and
`_safe_read_worker_branch` parse `.git/HEAD`, loose refs, and `packed-refs`.
They refuse symlinks and require every path to stay under the worker `.git`.

### Inspect / execute status, log, and diff

Those need a live worktree view. `_query_worker_git_snapshot` runs **one**
short-lived Podman container per worker via `build_worker_git_query_command` /
`PodmanSandboxRuntime.git_query_command`. That invocation:

- has **no** `--name herdr-<id>` (cannot collide with a running session)
- forces `--network=none`
- mounts the workspace **read-only**
- does **not** mount `/codex` or set `CODEX_HOME`
- sets `GIT_OPTIONAL_LOCKS=0` and uses `git --no-optional-locks`
- keeps `--userns=keep-id`, `--cap-drop=ALL`, `no-new-privileges`, `--read-only`,
  and the configured pids/memory/cpu limits

Status, log, and diff are collected in a single `sh -c` with the base commit
passed as a positional argument (not interpolated). When Podman or the worker
runtime is unavailable, fields report `(unavailable: ...)` with an issue entry.

HEAD and branch for list/inspect still come from the file-based helpers so a
missing container does not block identity reporting.

## Tests

`tests/test_security_worker_config.py` covers:

- Malicious `core.fsmonitor` / hooks / textconv / sshCommand markers for list,
  inspect, fetch, cherry-pick, and post-run HEAD reads
- Packed-refs resolution and fetch
- Symlinked refs and objects rejected; source left without sandbox ref or
  alternates
- Simulated bundle-create failure leaving the source unchanged
- Concurrent fetches of two workers
- Query command isolation (no session name, `--network=none`, no `/codex`,
  workspace `ro`)
- Auxiliary object metadata (commit-graph, MIDX, bitmap, info) not copied
- Corrupt loose object: fetch fails, source unchanged
- Audit that `_git_reality` calls `_query_worker_git_snapshot` once and that
  `_git_read` is absent

## Usage

```sh
hs list
hs inspect <id>
hs fetch <id>
git log main..sandbox/<id>
git diff main...sandbox/<id>
```

## README rule

The README safety section states that host-side git never uses a worker
repository as cwd or `-C`. Worker state is read from confined ref files, via a
verified bundle import into the source repo, or via one hardened git query
container per worker.
