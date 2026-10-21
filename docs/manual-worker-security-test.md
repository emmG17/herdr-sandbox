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

## Evidence for PRs

Attach stdout/stderr (or a short summary) for both the Codex and Claude Code
passes when claiming the disposable-worker security test. Live Claude login and
a real Herdr pane are out of scope for CI; note anything not verified live.
