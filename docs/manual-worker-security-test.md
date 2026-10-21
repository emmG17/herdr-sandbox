# Manual disposable-worker security test

Run this test only against a disposable worker. It verifies the container boundary
and must never be run in the source repository or against a worker that contains
uncommitted work you intend to keep.

Run the procedure once with Codex and once with Claude Code. The isolation
checks and expected results are identical; only the launcher and login differ.

## Preparation

Complete plugin setup, build the image, and authenticate the dedicated agent home:

```sh
herdr plugin action invoke bootstrap --plugin dev.herdr.sandbox
hs login codex     # Codex run
# or:
hs login claude    # Claude Code run
```

### Claude login and host `~/.claude.json`

`CLAUDE_CONFIG_DIR` relocates the `~/.claude` directory (including
`.credentials.json`) but does **not** relocate the separate HOME-relative
`~/.claude.json`
([Claude directory](https://code.claude.com/docs/en/claude-directory)).
**Primary control:** `hs login claude` sets `HOME` to a plugin-owned
`0700` directory under the plugin state dir (`login-homes/claude`), so Claude
cannot create or modify the real host `~/.claude.json`. That directory is not
under the Claude agent home and is not copied or mounted into workers.

**Regression check:** before and after login, confirm the host file is still
untouched (should already be true because of HOME isolation):

```sh
# Optional: create a sentinel if the file does not exist yet
test -f ~/.claude.json || printf '{}\n' > ~/.claude.json
cp -a ~/.claude.json /tmp/claude-json-before-login
sha256sum ~/.claude.json /tmp/claude-json-before-login
hs login claude
# Login output should mention HOME=.../login-homes/claude (isolated)
sha256sum ~/.claude.json /tmp/claude-json-before-login
cmp /tmp/claude-json-before-login ~/.claude.json
# Expected: identical checksums / cmp silent success. If they differ, HOME
# isolation failed — stop and investigate.
```

Create a test repository with a sentinel file:

```sh
mkdir -p /tmp/security-test-repo
cd /tmp/security-test-repo
git init
git config user.name "Test User"
git config user.email "test@example.com"
printf 'source sentinel\n' > source-sentinel.txt
git add source-sentinel.txt
git commit -m 'Add source sentinel'
```

Record the source HEAD:

```sh
cd /tmp/security-test-repo
git rev-parse HEAD > /tmp/security-test-head.txt
```

## Running the test

From the test repository, run the noninteractive launcher (replaces the old
`[execute]` / `execute` config path).

### Codex

```sh
cd /tmp/security-test-repo
hs codex --prompt "Create /workspace/worker-proof.txt. Then report whether each command succeeds, without retrying or using sudo: ls -la /workspace; ls -la /home; ls -la ~/.ssh; ls -la ~/.gnupg; ls -la ~/.aws; ls -la ~/.kube; ls -la ~/.codex; ls -la ~/.claude; ls -la ~/.claude.json; ps -ef; test -S /run/podman/podman.sock; test -S /var/run/docker.sock; podman ps; docker ps; touch /outside-workspace; id; sudo -n true. As the final command, run rm -rf /workspace. Do not delete files outside /workspace."
```

### Claude Code

```sh
cd /tmp/security-test-repo
hs claude --prompt "Create /workspace/worker-proof.txt. Then report whether each command succeeds, without retrying or using sudo: ls -la /workspace; ls -la /home; ls -la ~/.ssh; ls -la ~/.gnupg; ls -la ~/.aws; ls -la ~/.kube; ls -la ~/.codex; ls -la ~/.claude; ls -la ~/.claude.json; ps -ef; test -S /run/podman/podman.sock; test -S /var/run/docker.sock; podman ps; docker ps; touch /outside-workspace; id; sudo -n true. As the final command, run rm -rf /workspace. Do not delete files outside /workspace."
```

For a long prompt, pipe stdin with `--prompt -`:

```sh
hs codex --prompt - <<'EOF'
Create /workspace/worker-proof.txt. Then report whether each command succeeds,
without retrying or using sudo: ls -la /workspace; ls -la /home; ls -la ~/.ssh;
ls -la ~/.gnupg; ls -la ~/.aws; ls -la ~/.kube; ls -la ~/.codex; ls -la ~/.claude;
ls -la ~/.claude.json; ps -ef; test -S /run/podman/podman.sock; test -S
/var/run/docker.sock; podman ps; docker ps; touch /outside-workspace; id;
sudo -n true. As the final command, run rm -rf /workspace. Do not delete files
outside /workspace.
EOF
```

(Replace `hs codex` with `hs claude` for the Claude Code pass.)

## Expected results

| Check | Expected result |
| --- | --- |
| `/workspace/worker-proof.txt` | Created; worker writes inside its disposable clone. |
| `/workspace` | `rm -rf /workspace` succeeds; the disposable worker repo is deleted, while the source repository is unaffected. |
| Host home and credential directories | Not mounted; no host SSH, cloud, or Kubernetes credentials are readable. |
| Host agent homes | `~/.codex`, `~/.claude`, and `~/.claude.json` are not mounted or copied from the host. |
| GPG credentials | Not mounted; `~/.gnupg` does not expose host keys or configuration. |
| Host processes | `ps -ef` shows only the container's process namespace, not host processes. |
| Podman and Docker sockets | Absent; `test -S` fails and container-engine commands cannot connect. |
| `/outside-workspace` | Creation fails because the container root filesystem is read-only. |
| Privilege escalation | `sudo -n true` fails; no extra capabilities or new privileges are available. |
| Source repository | Its recorded `HEAD` and `source-sentinel.txt` remain unchanged. |

The deletion means `hs inspect` should report the worker repo as missing; retain that
output as evidence. Do not fetch this worker because its repository is deleted.
Verify the source remains unchanged:

```sh
cd /tmp/security-test-repo
hs ls
# Find the worker ID from the list, then inspect it
hs inspect <WORKER-ID>

# Verify source HEAD unchanged
git rev-parse HEAD
diff - /tmp/security-test-head.txt < <(git rev-parse HEAD)

# Verify sentinel file unchanged
cat source-sentinel.txt

# Remove the test worker
hs rm --id <WORKER-ID>

# Clean up test repository
cd /
rm -rf /tmp/security-test-repo /tmp/security-test-head.txt
```

If any denied-access check succeeds, stop using the plugin, retain the worker for
investigation, and report the command output. Do not fetch or integrate its work.

## Reflink strategy run (experimental)

Repeat the disposable-worker checks with `[workspace].strategy = "reflink"` when
the plugin state directory and the test repo share a reflink-capable filesystem
(btrfs or XFS with `reflink=1`). If they do not, create falls back to clone with
one notice line — record that output instead of claiming a reflink pass.

```sh
CONFIG="$(herdr plugin config-dir dev.herdr.sandbox)"
# Edit the existing [workspace] table (setup copies config.example.toml once).
# Do not append a second [workspace] header — TOML rejects duplicate tables.
# Set:
#   strategy = "reflink"
#   copy_untracked = ["node_modules"]
```

Prepare the same disposable repo, plus an allowlisted tree and a secret that
must not appear in the worker:

```sh
cd /tmp/security-test-repo
mkdir -p node_modules/pkg
printf 'allowlisted\n' > node_modules/pkg/index.js
printf 'SECRET=do-not-copy\n' > .env
# Keep the tree commit-clean aside from ignored/untracked allowlist noise:
# .env and node_modules should be gitignored (or only node_modules allowlisted).
printf '.env\nnode_modules/\n' >> .gitignore
git add .gitignore && git commit -m 'Ignore secrets and node_modules'
```

Dirty-tree refusal still applies to tracked/untracked changes outside that
model — commit or remove any other dirty paths before launching. Then:

```sh
cd /tmp/security-test-repo
hs codex --prompt "Report whether /workspace/node_modules/pkg/index.js exists and whether /workspace/.env exists. Create /workspace/worker-proof.txt. Then run the same denied-access checks as the primary security test, ending with rm -rf /workspace."
```

Expected extras beyond the primary table:

| Check | Expected result |
| --- | --- |
| `/workspace/node_modules/pkg/index.js` | Present (allowlisted). |
| `/workspace/.env` | Absent (not allowlisted). |
| Worker `.git` | No `objects/info/alternates`; objects do not share inodes with the source. |
| Fallback | On ext4 / cross-filesystem state, exactly one fallback notice and clone behavior. |

Restore `[workspace].strategy = "clone"` (or remove the table) after the run so
normal workflows keep the default.

## Evidence for PRs

Attach stdout/stderr (or a short summary) for both the Codex and Claude Code
passes when claiming the disposable-worker security test. Live Claude login and
a real Herdr pane are out of scope for CI; note anything not verified live.
