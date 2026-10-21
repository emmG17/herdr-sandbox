# Manual disposable-worker security test

Run this test only against a disposable worker. It verifies the container boundary
and must never be run in the source repository or against a worker that contains
uncommitted work you intend to keep.

## Preparation

Complete plugin setup, build the image, and authenticate the dedicated Codex home:

```sh
herdr plugin action invoke bootstrap --plugin dev.herdr.sandbox
CODEX_HOME="$(herdr plugin config-dir dev.herdr.sandbox)/codex" codex login --device-auth
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

Launch Codex using `hs codex` from the test repository:

```sh
cd /tmp/security-test-repo
hs codex
```

In the Codex session, give the following prompt:

```
Create /workspace/worker-proof.txt. Then report whether each command succeeds,
without retrying or using sudo: ls -la /workspace; ls -la /home; ls -la ~/.ssh;
ls -la ~/.gnupg; ls -la ~/.aws; ls -la ~/.kube; ps -ef; test -S
/run/podman/podman.sock; test -S /var/run/docker.sock; podman ps; docker ps;
touch /outside-workspace; id; sudo -n true. As the final command, run
rm -rf /workspace. Do not delete files outside /workspace.
```

## Expected results

| Check | Expected result |
| --- | --- |
| `/workspace/worker-proof.txt` | Created; worker writes inside its disposable clone. |
| `/workspace` | `rm -rf /workspace` succeeds; the disposable worker repo is deleted, while the source repository is unaffected. |
| Host home and credential directories | Not mounted; no host SSH, cloud, or Kubernetes credentials are readable. |
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
hs list
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
