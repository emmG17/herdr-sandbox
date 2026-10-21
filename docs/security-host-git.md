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

Alternatives considered:

1. **Source-repo alternates + `repack -a -d`** (rejected). Mutates the user's
   real repository, races on `objects/info/alternates`, can leave a permanent
   dependency on an attacker-writable object store if interrupted, ignores
   repack failures, and rewrites every pack on each fetch.
2. **`git bundle` inside the worker container, then host `bundle verify` +
   `fetch`** (sound, but couples every fetch to Podman/image availability).
3. **Copy worker object files into a temporary bare repo we own, create a
   bundle there, verify it, then `git fetch <bundle>` into the source repo**
   (chosen). Host git never uses the worker clone as cwd/`-C`. The source repo
   is untouched until the final verified bundle fetch. Failure or a crash
   before that fetch leaves no alternates, no dangling `sandbox/<id>` ref, and
   no dependency on the worker object store. Concurrent fetches use distinct
   temp directories and git's own source-repo ref locking.

Steps for `_safe_fetch_from_worker`:

1. Read the worker tip from loose refs or `packed-refs` (no git; refuse
   symlinks; confine paths under the worker `.git`).
2. `git init --bare` in a temporary directory.
3. Copy only object files from the worker (refuse symlinks; skip
   `objects/info/alternates`).
4. Write the branch tip in the temp repo and `git bundle create`.
5. `git bundle verify`, then `git -C <source> fetch <bundle> <branch>:refs/heads/sandbox/<id>`.
6. Delete the temporary directory.

### Ref and HEAD reads

`_safe_read_worker_ref`, `_safe_read_worker_head`, and
`_safe_read_worker_branch` parse `.git/HEAD`, loose refs, and `packed-refs`.
They refuse symlinks and require every path to stay under the worker `.git`.

### Inspect / execute status, log, and diff

Those need a live worktree view. They run via
`_query_worker_git_in_container`: a short-lived Podman invocation that runs
`git` inside the sandbox. Worker config may execute only inside the container.
When Podman or the worker runtime is unavailable, the fields report
`(unavailable: ...)` with an issue entry rather than silent placeholders.

HEAD and branch for list/inspect still come from the file-based helpers so a
missing container does not block identity reporting.

### Deprecated helper

`_git_read(repo, ...)` remains for non-worker paths but is marked deprecated for
worker clones. Grep-style tests require `_git_reality` to use the safe helpers
and container query instead.

## Tests

`tests/test_security_worker_config.py` covers:

- Malicious `core.fsmonitor` / hooks / textconv / sshCommand markers for list,
  inspect, fetch, cherry-pick, and post-run HEAD reads
- Packed-refs resolution and fetch
- Symlinked refs and objects rejected; source left without sandbox ref or
  alternates
- Simulated bundle-create failure leaving the source unchanged
- Concurrent fetches of two workers
- Audit that safe fetch does not use source-repo alternates/repack

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
verified bundle import into the source repo, or via git inside the sandbox
container.
