# OpenEvo Desktop SSH Transport Foundation

Tracked by #43.

This document describes the first concrete transport behind the OpenEvo Desktop
sidecar executor contract. It lets the local Desktop sidecar run remote
preflight commands and prepare task workspaces on a remote GPU server through
system OpenSSH and rsync.

This is still not the full remote backend. It does not start a long-running
remote daemon, manage Docker Compose, manage vLLM, or perform Desktop UI
orchestration. Remote dependency installation is limited to the bootstrap
layer's user-site Python package checks.

## Transport Contract

Desktop chooses between dry-run and SSH transports through the sidecar API. The
transport contract is covered by focused tests:

```bash
PYTHONPATH=src:. python -m pytest tests/openevo/remote/test_ssh_transport.py -q
```

`dry-run` remains the default. It never opens network connections and is useful
for Desktop integration tests, config validation, and local report rendering.

`ssh` opts into real remote access. It uses the same sidecar execution report
shape as dry-run, including nested `preflight.ready`, `workspace.ready`, and
top-level `ready`.

## Transport Boundary

`SshRemoteExecutorTransport` implements the existing `RemoteExecutorTransport`
protocol:

```python
class RemoteExecutorTransport(Protocol):
    def run(command, *, cwd=None, env=None, timeout_seconds=30.0) -> RemoteCommandResult: ...
    def upload_dir(local_path, remote_path) -> None: ...
```

The implementation is subprocess-backed and uses argv lists, not local shell
strings. This keeps local command construction testable and avoids local shell
injection risks.

Remote commands still execute through the remote user's shell because OpenSSH
receives a remote command string. The transport owns the remote shell quoting for
environment variables, `cwd`, and rsync-created remote paths.

## External Requirements

The local machine running OpenEvo Desktop must have:

- `ssh` available on `PATH`;
- `rsync` available on `PATH` for local-folder workspace uploads;
- network access from the local machine to the remote SSH server;
- a known-hosts policy configured by the user or operating system.

Remote `host` values in this slice support OpenSSH host aliases, DNS names, and
IPv4-style addresses that do not contain `@` or `:`. IPv6 literal hosts are not
supported yet because rsync destination syntax needs separate bracket handling.

The transport does not disable host-key checking. Users should configure
`~/.ssh/known_hosts` or their OpenSSH config before first real execution.

## Supported Auth Modes

Supported:

- `ssh_agent`: uses the local OpenSSH agent or default OpenSSH identity lookup.
- `private_key`: passes `-i <private_key_path>` as a subprocess argv element.

Unsupported in this slice:

- `password_ref`
- `passphrase_ref`

Those fields are references, not secrets. OpenEvo Desktop needs a credential
vault before it can safely resolve them. Until that vault exists, the SSH
transport rejects these auth modes with actionable errors instead of prompting,
logging, or guessing.

When Desktop is served with `--transport ssh`, `GET /openevo-api/desktop/shell`
reports `sidecar.transport.supports_password_ref=false` and
`sidecar.transport.supports_passphrase_ref=false`. The packaged Desktop UI uses
those capability flags to preserve saved config round-tripping but disable
workspace sync, bootstrap, and run launch until the active profile uses
`ssh_agent` or a private key without a secret reference. The sidecar API applies
the same guard to direct mutating requests and returns `409` before constructing
the SSH transport.

## Command Execution

`run(command, env=..., cwd=...)` builds:

```text
ssh -p <port> [-i <key>] -o BatchMode=yes -l <user> -- <host> <remote-command>
```

`env` is injected into the remote command as POSIX assignments:

```text
env HTTPS_PROXY=http://127.0.0.1:7890 HF_ENDPOINT=https://hf-mirror.com <command>
```

This environment is for the remote task command. It is not an SSH connection
proxy. Users who need SSH jump hosts or SSH-level proxying should configure
OpenSSH separately.

If `cwd` is provided, it must be an absolute remote path using only the SSH
transport safe path characters (`/`, letters, digits, `.`, `_`, `-`, `@`, `%`,
`+`, `=`, `,`). It is emitted as:

```text
cd <quoted-cwd> && <command>
```

Non-zero remote exit codes are returned as `RemoteCommandResult` values. They do
not raise. Timeouts raise `TimeoutError`, which the executor converts into
structured failure reports during preflight or workspace execution.

## Workspace Upload

`upload_dir(local_path, remote_path)`:

1. verifies the local path exists and is a directory;
2. verifies the remote path is absolute and uses only the SSH transport safe
   path characters;
3. creates the remote target directory with `mkdir -p`;
4. runs `rsync -az --delete -e "ssh ... -l <user>"` from `local_path/` to
   `<host>:remote_path/`.

The trailing slash semantics intentionally upload the contents of the local
folder into the prepared remote workspace path.

## Limitations

This slice does not include:

- password or passphrase vault integration;
- OpenSSH config editing;
- Windows-specific rsync packaging;
- host-wide remote dependency installation or repair beyond bootstrap's
  user-site `openevo` and `huggingface_hub` installs;
- remote sidecar daemon startup;
- Docker daemon, Docker Compose, or vLLM lifecycle management;
- dynamic adapter/model lifecycle;
- Desktop UI wiring.

These should build on top of the transport/report contract rather than changing
the sidecar science plan schema.

## Verification

Focused validation:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/science tests/evolution/test_models.py --collect-only -q >/tmp/openevo-ssh-transport-collect.txt
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/deployment src/openevo/backend/launcher.py tests/openevo/remote tests/openevo/test_cli.py
git diff --check openevo/stable...HEAD
```
