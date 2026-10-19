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

Worker reuse is based on a three-part key: **(pane ID, source repo, agent)**.

### Reuse Rules

1. **Missing or empty `HERDR_PANE_ID`** → always create a new worker
   - Popup panes do not receive `HERDR_PANE_ID`
   - Shells outside Herdr do not receive `HERDR_PANE_ID`
   - Never fall back to `focused_pane_id` from `HERDR_PLUGIN_CONTEXT_JSON`

2. **All three keys must match** for reuse:
   - Same pane ID
   - Same source repository (resolved to absolute path)
   - Same agent

3. **Different directory, different agent** → new worker
   - A pane that changes directory into another repo gets a new worker
   - A pane that switches from `hs codex` to `hs claude` gets a new worker

### Pane ID Stability (Verified with Herdr 0.9.5)

From the spike findings in `docs/herdr-pane-lifecycle.md` (PR #16):

- **Pane IDs are stable** across Herdr server restarts
- **Pane IDs are never reused** after a pane is closed
- **No collision risk** across sessions or workspaces
  - The pane ID counter persists across restarts
  - New panes always get a higher ID than any previous pane
  - Format is `w<workspace-number>:p<pane-number>`

**Conclusion**: Pane IDs alone are sufficient as a stable, unique reuse key. No additional session identity or disambiguation is needed.

### Workspace ID Considerations

The `workspace_id` is recorded for diagnostics and potential future use, but is not strictly required for the reuse key because:
- Pane IDs already include workspace scope (`w1:p1`, `w2:p1`, etc.)
- Different workspaces cannot have the same pane ID
- The spike verified no collision risk

## HEAD-Moved Behavior

When a worker's result is fetched and the source repository HEAD has moved past the worker's base commit:

### Decision: Proceed as-is with notice

The plugin:
1. Fetches the worker branch into `refs/heads/sandbox/<id>` as-is
2. Prints a one-line notice showing both SHAs:
   ```
   Notice: source HEAD has moved from abc123def456 to 789abc012def.
   The worker result is based on the original base commit.
   ```
3. Does NOT auto-rebase
4. Does NOT touch the host branch

### Rationale

- **Predictable**: The user knows exactly what commit the worker started from
- **Safe**: No automatic changes to the worker's result or the host branch
- **Flexible**: The user can rebase, merge, or cherry-pick manually as needed
- **Transparent**: The notice provides both SHAs so the user can assess the delta

### Example Scenario

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

The user can then review and integrate as appropriate:
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
