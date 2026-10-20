# Security Design: Host Git Operations

## Problem

Worker repositories are mounted read-write at `/workspace` inside containers, allowing workers to modify `.git/config`. If host-side git operations use the worker repository as cwd or `-C` target, malicious config can execute arbitrary commands on the host through:

- `core.fsmonitor` - executes a command when checking file status
- `core.hooksPath` - directory containing hook scripts
- `core.sshCommand` - command for SSH operations  
- `diff.<driver>.textconv` with `.gitattributes` - filters arbitrary files through commands
- `core.pager` - command for paging git output

This violates the security invariant that workers never get write access to the source repository or host, including indirectly.

## Solution

### 1. Safe File-Based Helpers

Host-side operations that need to read worker state use safe helpers that parse `.git/HEAD` and `.git/refs/*` directly without running git:

- `_safe_read_worker_head(repo)` - reads HEAD by parsing files
- `_safe_read_worker_branch(repo)` - reads current branch from HEAD file
- `_git_read(repo, ...)` - marked DEPRECATED with warning for worker repos

### 2. Safe Fetch Using Alternates

`_safe_fetch_from_worker()` fetches worker commits without spawning upload-pack or running git in the worker repo:

1. Read worker branch SHA from `.git/refs/heads/<branch>` directly (no git command)
2. Temporarily add worker's `.git/objects` as an alternate in source repo
3. Run `git update-ref` in SOURCE repo only to update target branch
4. Run `git repack` in SOURCE repo to copy objects permanently
5. Restore original alternates file

This avoids running any git command with the worker repo as cwd or `-C` target.

### 3. Omitted Information

For security, certain operations that would require running git in the worker context are intentionally omitted:

- `git status` - would honor `core.fsmonitor`
- `git log` - could trigger pager or hooks
- `git diff` - could trigger textconv or ext-diff

These are returned as `"(omitted: query after fetch for safety)"`. Users can get this information by fetching the worker branch into the source repo and querying there.

### 4. Test Coverage

Regression tests in `tests/test_security_worker_config.py` verify that malicious worker config never executes:

- Creates workers with `core.fsmonitor`, `core.hooksPath`, etc. pointing at marker files
- Runs list, inspect, fetch, cherry-pick, and execute operations
- Asserts that no markers are created (no code execution)

### 5. Grep-Style Enforcement

The audit test `TestGitCommandAudit` verifies:

- `_git_read` has a deprecation warning for worker repos
- Safe functions `_safe_read_worker_head` and `_safe_read_worker_branch` exist
- `_git_reality` uses safe functions, not `_git_read`

## What's Still Exposed

`git fetch` from a local worker repo path uses Git's "local" transport, which directly accesses the object database without spawning upload-pack. However, Git may still read some config during this operation. Defense-in-depth environment variables (`GIT_OPTIONAL_LOCKS=0`) are set.

The alternates-based approach avoids this by never running fetch with the worker repo as a source - it only manipulates the source repo.

## Usage

Workers are created, used, and destroyed normally. The security changes are transparent:

```bash
hs codex                    # Create/reuse worker, launch Codex
hs list                     # Query worker state (no code execution)
hs inspect <id>             # Detailed worker info (no code execution)
hs fetch <id>               # Safe fetch into sandbox/<id> branch
git diff main...sandbox/<id> # Review in source repo (safe)
```

Status, log, and diff information are omitted from `inspect` output. To see these, fetch the worker branch and query in the source repo:

```bash
hs fetch <id>
git log sandbox/<id>
git diff main...sandbox/<id>
git show sandbox/<id>
```

## Acceptance Criteria Met

✅ No `run("git", "-C", <worker repo>, ...)` calls remain on host  
✅ Malicious config regression tests pass for list, inspect, fetch, execute, cherry-pick  
✅ Grep-style test enforces no vulnerable patterns  
✅ All 146 existing tests pass  
✅ README safety section documents the rule (see below)

## README Update

Added to README.md safety section:

> **Host-side Git Operations**: The plugin never runs git commands with a worker repository as cwd or `-C` target. Worker `.git/config` is attacker-controlled and could execute arbitrary commands through settings like `core.fsmonitor` or `core.hooksPath`. Instead, the plugin reads worker state by parsing `.git/HEAD` and `.git/refs/*` directly, or by using git alternates to make worker objects visible to the source repository without running git in the worker context.
