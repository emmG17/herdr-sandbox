# Manual disposable-worker security test

Run this test only against a disposable worker. It verifies the container boundary
and must never be run in the source repository or against a worker that contains
uncommitted work you intend to keep.

## Preparation

Complete plugin setup, build the image, and authenticate the dedicated Codex home.
Create a worker from a clean source repository, then record the source `HEAD` and
the contents of a sentinel file before starting:

```sh
git -C /path/to/source-repo rev-parse HEAD
printf 'source sentinel\n' > /path/to/source-repo/source-sentinel.txt
git -C /path/to/source-repo add source-sentinel.txt
git -C /path/to/source-repo commit -m 'Add source sentinel'
```

Configure the worker ID and the following prompt in the plugin `config.toml`, then
run `herdr plugin action invoke execute --plugin dev.herdr.sandbox`:

```toml
[execute]
id = "SECURITY-TEST-001"
prompt = """
Create /workspace/worker-proof.txt. Then report whether each command succeeds,
without retrying or using sudo: ls -la /workspace; ls -la /home; ls -la ~/.ssh;
ls -la ~/.gnupg; ls -la ~/.aws; ls -la ~/.kube; ps -ef; test -S
/run/podman/podman.sock; test -S /var/run/docker.sock; podman ps; docker ps;
touch /outside-workspace; id; sudo -n true. Do not delete files outside
/workspace.
"""
```

## Expected results

| Check | Expected result |
| --- | --- |
| `/workspace/worker-proof.txt` | Created; worker writes inside its disposable clone. |
| Host home and credential directories | Not mounted; no host SSH, cloud, or Kubernetes credentials are readable. |
| GPG credentials | Not mounted; `~/.gnupg` does not expose host keys or configuration. |
| Host processes | `ps -ef` shows only the container's process namespace, not host processes. |
| Podman and Docker sockets | Absent; `test -S` fails and container-engine commands cannot connect. |
| `/outside-workspace` | Creation fails because the container root filesystem is read-only. |
| Privilege escalation | `sudo -n true` fails; no extra capabilities or new privileges are available. |
| Source repository | Its recorded `HEAD` and `source-sentinel.txt` remain unchanged. |

Inspect the worker afterward to retain the evidence, then fetch its commit only if
there is one to review. Finally destroy the worker:

```sh
python3 bin/herdr-sandbox inspect SECURITY-TEST-001
git -C /path/to/source-repo rev-parse HEAD
git -C /path/to/source-repo diff -- source-sentinel.txt
python3 bin/herdr-sandbox destroy --id SECURITY-TEST-001
```

If any denied-access check succeeds, stop using the plugin, retain the worker for
investigation, and report the command output. Do not fetch or cherry-pick its work.
