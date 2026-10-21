# Worker Reuse and State Store

This document describes the worker identity, state store, and reuse logic implemented for the herdr-sandbox plugin.

## Worker Identity

Worker IDs follow the format `<repo-slug>-<short-random>`:
- The repo slug is derived from the source repository directory name, sanitized to match `WORKER_ID_RE`
- The random suffix is a 6-character hex string (3 bytes from `secrets.token_hex(3)`)
- Maximum worker ID length is 64 characters
- Generated IDs are validated to be legal refs under both `agent/` and `sandbox/` namespaces

### Example IDs
- `herdr-sandbox-a1b2c3`
- `my-project-9f8e7d`
- `test-repo-123abc`

## State Store

Worker metadata is stored in `$HERDR_PLUGIN_STATE_DIR/workers/<worker-id>/worker.json`.

### Concurrent Safety

The state store uses two mechanisms to prevent corruption from concurrent operations:

1. **File locking**: A global lock file (`state.lock`) ensures exclusive access during create/update operations using `fcntl.flock()`
2. **Atomic writes**: Metadata updates write to a temporary file, fsync, then `os.replace()` to ensure atomicity

This prevents two panes launching `hs` simultaneously from corrupting state or generating duplicate IDs.

### Worker Metadata Schema

```json
{
  "id": "herdr-sandbox-a1b2c3",
  "source_repo": "/path/to/source",
  "agent": "codex",
  "pane_id": "w1:p1",
  "workspace_id": "w1",
  "base_ref": "main",
  "base_commit": "abc123...",
  "branch": "agent/herdr-sandbox-a1b2c3",
  "created_at": "2026-10-19T05:38:00Z",
  "runtime": "podman",
  "network": "online",
  "status": "created",
  "result_branch": null,
  "fetched_commit": null
}
```

New fields:
- `agent`: The agent name (e.g., "codex", "claude")
- `pane_id`: Herdr pane ID (null for popup panes or non-Herdr shells)
- `workspace_id`: Herdr workspace ID (null when pane_id is null)
- `result_branch`: Branch name in source repo after fetch (e.g., "sandbox/herdr-sandbox-a1b2c3")
- `fetched_commit`: Commit SHA after fetch

The `status` field now includes a new lifecycle state: `"fetched"`.

## Worker Reuse

Worker reuse is based on a four-part key: **(pane ID, socket path, source repo, agent)**.

### Reuse Rules

1. **Missing or empty `HERDR_PANE_ID`** → always create a new worker
   - Popup panes do not receive `HERDR_PANE_ID`
   - Shells outside Herdr do not receive `HERDR_PANE_ID`
   - Never fall back to `focused_pane_id` from `HERDR_PLUGIN_CONTEXT_JSON`

2. **All four keys must match** for reuse:
   - Same pane ID (`HERDR_PANE_ID`)
   - Same socket path (`HERDR_SOCKET_PATH`)
   - Same source repository (resolved to absolute path)
   - Same agent

3. **Different directory, agent, or session** → new worker
   - A pane that changes directory into another repo gets a new worker
   - A pane that switches from `hs codex` to `hs claude` gets a new worker
   - A pane in a different Herdr server instance gets a new worker

### Collision Verification (Empirically Tested with Herdr 0.9.5)

#### Test 1: Workspace ID Reuse After Closure

**Commands:**
```bash
herdr server &
herdr workspace create --label "test1"
# Result: workspace_id=w1, pane_id=w1:p1

herdr workspace close "w1"

herdr workspace create --label "test2"
# Result: workspace_id=w2, pane_id=w2:p1
```

**Result:** ✅ **NO COLLISION** - Workspace IDs are NOT reused after closure

#### Test 2: Multiple Servers with Different Sockets

**Commands:**
```bash
HERDR_SOCKET_PATH="/tmp/herdr-a.sock" herdr server &
HERDR_SOCKET_PATH="/tmp/herdr-b.sock" herdr server &

HERDR_SOCKET_PATH="/tmp/herdr-a.sock" herdr workspace create --label "session-a"
# Result: workspace_id=w1, pane_id=w1:p1

HERDR_SOCKET_PATH="/tmp/herdr-b.sock" herdr workspace create --label "session-b"
# Result: workspace_id=w1, pane_id=w1:p1

# Check pane environments
HERDR_SOCKET_PATH="/tmp/herdr-a.sock" herdr pane read "w1:p1"
# Output: SOCKET=/tmp/herdr-a.sock PANE=w1:p1 WS=w1

HERDR_SOCKET_PATH="/tmp/herdr-b.sock" herdr pane read "w1:p1"
# Output: SOCKET=/tmp/herdr-b.sock PANE=w1:p1 WS=w1
```

**Result:** ❌ **COLLISION DETECTED** - Multiple Herdr servers CAN have the same pane ID simultaneously!

**Discriminator:** ✅ `HERDR_SOCKET_PATH` is available in pane environments and differs per server:
- Server A pane sees: `/tmp/herdr-a.sock`
- Server B pane sees: `/tmp/herdr-b.sock`

### Conclusion from Empirical Testing

**Socket path must be included in the reuse key** to prevent collision across multiple Herdr servers.

- **Pane IDs are stable** within a single Herdr server across restarts
- **Pane IDs are never reused** after a pane is closed (within same server)
- **Pane IDs CAN collide** across different Herdr server instances
- **Socket path disambiguates** server instances

#### Full Test Results (Herdr 0.9.5)

**Test 1: Workspace ID Reuse After Closure**

Commands:
```bash
herdr server &
herdr workspace create --label "test1"      # → w1, pane w1:p1
herdr workspace close "w1"
herdr workspace create --label "test2"      # → w2, pane w2:p1
```

Result: ✅ **NO COLLISION** - Workspace IDs are NOT reused after closure

**Test 2: Multiple Servers with Different Sockets**

Commands:
```bash
HERDR_SOCKET_PATH="/tmp/herdr-a.sock" herdr server &
HERDR_SOCKET_PATH="/tmp/herdr-b.sock" herdr server &

HERDR_SOCKET_PATH="/tmp/herdr-a.sock" herdr workspace create
# → workspace=w1, pane=w1:p1, socket=/tmp/herdr-a.sock

HERDR_SOCKET_PATH="/tmp/herdr-b.sock" herdr workspace create
# → workspace=w1, pane=w1:p1, socket=/tmp/herdr-b.sock
```

Result: ❌ **COLLISION DETECTED** - Both servers have pane w1:p1!

Discriminator: ✅ Each pane sees different `HERDR_SOCKET_PATH`:
- Server A pane: `/tmp/herdr-a.sock`
- Server B pane: `/tmp/herdr-b.sock`

### Metadata Schema Update

New field:
- `socket_path`: Socket path from `HERDR_SOCKET_PATH` (null when not in Herdr)

## HEAD-Moved Behavior

### At Fetch Time

When a worker's result is fetched and the source repository HEAD has moved past the worker's base commit:

**Decision: Proceed as-is with notice**

The plugin:
1. Fetches `refs/heads/agent/<id>` into `refs/heads/sandbox/<id>` as-is
   (forced update; `--no-write-fetch-head`; still creates the ref when the
   commit count relative to base is zero)
2. Prints the branch name, commit count relative to base, a short
   `git log --oneline`, and a one-line notice showing both SHAs when HEAD moved:
   ```
   Notice: source HEAD has moved from abc123def456 to 789abc012def.
   The worker result is based on the original base commit.
   ```
3. Does NOT auto-rebase
4. Does NOT touch the host branch (only `refs/heads/sandbox/<id>` changes)
5. Fails cleanly if `sandbox/<id>` is checked out in any worktree; re-run with
   `hs fetch <id>` after switching away

**Rationale:**
- **Predictable**: The user knows exactly what commit the worker started from
- **Safe**: No automatic changes to the worker's result or the host branch
- **Flexible**: The user can rebase, merge, or cherry-pick manually as needed
- **Transparent**: The notice provides both SHAs so the user can assess the delta

### At Reuse Time

When `hs <agent>` would reuse an existing worker and the source HEAD has moved since that worker's base commit:

**Decision: Reuse the worker with notice**

The plugin:
1. Reuses the existing worker (to preserve any uncommitted work)
2. Prints a one-line notice:
   ```
   Notice: reusing worker herdr-sandbox-a1b2c3; source HEAD has moved from abc123... to def456....
   The worker is based on the original commit.
   ```
3. Does NOT create a new worker
4. Does NOT update the worker's base commit

**Rationale:**
- **Preserves work**: The worker may have uncommitted changes or state
- **Explicit control**: User can destroy the worker if they want a fresh one at current HEAD
- **Consistent**: Matches the fetch-time behavior (proceed with notice)
- **Transparent**: User is informed of the base commit mismatch

**To start fresh at current HEAD:**
```bash
hs destroy <worker-id>
hs codex  # Creates new worker at current HEAD
```

### Example Scenarios

**Scenario 1: Fetch after HEAD moved**
```bash
# Worker created when HEAD is at commit A
hs codex

# Host moves forward to commit B
git commit -m "new change"

# Worker result is fetched
hs fetch

# Output:
# Notice: source HEAD has moved from abc123... to def456...
# The worker result is based on the original base commit.
# Branch: sandbox/herdr-sandbox-a1b2c3
# Commit: 789abc...
```

**Scenario 2: Reuse after HEAD moved**
```bash
# Worker exists, created at commit A
# (previous session with `hs codex`)

# Host has moved forward to commit B
git log --oneline -1  # shows commit B

# Relaunch in same pane
hs codex

# Output:
# Notice: reusing worker herdr-sandbox-a1b2c3; source HEAD has moved from abc123... to def456....
# The worker is based on the original commit.
# [Worker starts with its existing state]
```

Integration example:
```bash
git log main..sandbox/herdr-sandbox-a1b2c3
git rebase main sandbox/herdr-sandbox-a1b2c3  # if desired
git merge sandbox/herdr-sandbox-a1b2c3        # or merge
```

## Security Invariants

The new state store maintains all existing security invariants:
- Worker clones never share objects with the source repo (no hardlinks, no alternates)
- Metadata updates are atomic and locked to prevent races
- Pane IDs are recorded but never blindly trusted from user-controlled context
- The reuse key prevents a worker from being reused across different repos or agents

## Implementation Details

### Concurrency Safety

The state store uses two mechanisms to prevent corruption:

1. **File locking**: A global lock file (`state.lock`) ensures exclusive access during create/update operations using `fcntl.flock()`
2. **Atomic writes**: Metadata updates write to a temporary file, fsync, then `os.replace()` to ensure atomicity

This prevents two panes launching `hs` simultaneously from corrupting state or generating duplicate IDs.

### ID Generation Algorithm

```python
def generate_worker_id(repo_path):
    slug = sanitize_repo_name(repo.name)  # Alphanumeric, dots, hyphens
    for attempt in range(100):
        random_suffix = secrets.token_hex(3)  # 6 hex chars
        worker_id = f"{slug}-{random_suffix}"
        if valid_and_unique(worker_id):
            return worker_id
```

Generated IDs are validated to be:
- Legal Git refs under both `agent/` and `sandbox/` namespaces
- Unique across all existing workers
- At most 64 characters long

### Clone Without Shared Objects

Worker creation uses:
```python
git("clone", "--no-hardlinks", "--no-checkout", source, clone)
```

After cloning, the implementation explicitly removes any `objects/info/alternates` file
to ensure the worker can never access the source repository's objects, even indirectly.
