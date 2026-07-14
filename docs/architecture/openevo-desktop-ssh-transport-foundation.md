# Release-incompatible foundation note

This document preserves pre-External-Beta foundation context. It is not External Beta release behavior.
Direct run commands, dry-run transports,
developer override env vars, legacy token headers, package-relative Core
artifacts, and command-based service facades are superseded by
`docs/maintainer/productization/spec.md`.

# OpenEvo Desktop SSH Transport Foundation

Tracked by #43 and hardened for the provider-owned host-key boundary in #163.

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
- `ssh-keyscan` available on `PATH` for the controlled host-key probe;
- `rsync` available on `PATH` for local-folder workspace uploads;
- network access from the local machine to the remote SSH server.

Remote `host` values support DNS names, IPv4 addresses, and IPv6 literals. IPv6
rsync destinations use the required bracketed form. Host values that can be
interpreted as local option or destination injection are rejected before any
subprocess starts.

Desktop does not read or modify the user's `~/.ssh/known_hosts`. It also disables
global known-host lookup for every SSH subprocess.

## Provider-Owned Host-Key Trust

`ProviderKnownHostStore` owns enrollment and persistence. Its root must be a
direct child of a sidecar-owned secure ancestor. The ancestor must be an
owner-controlled, non-symlink directory that is not group/world writable; the
store root must be an owner-controlled, non-symlink directory with mode `0700`.
The store holds no-follow ancestor/root descriptors and a stable lock inode for
the binding lifetime. Every operation rechecks pathname-to-descriptor identity
and uses a shared/exclusive cross-process `flock`. Each profile is mapped to an
opaque SHA-256 filename, and each known-host file must be a link-count-one,
owner-controlled regular file with mode `0600`. Reads are descriptor-relative
and no-follow. Publication writes and fsyncs a private temporary file, publishes
it with an atomic no-replace hard link, removes the temporary name, and fsyncs
the directory. Existing records are immutable unless an explicit compare-and-
swap rotation succeeds. Store paths reject whitespace, quoting, backslashes,
and OpenSSH `%` token expansion.

OpenSSH accepts a pathname rather than an already-verified file descriptor. For
each command, rsync, or tunnel spawn, Core therefore copies the validated record
to a random `0700` lease directory directly under the held secure ancestor. The
lease file is `0600`, is independent of the mutable trust-store root, and is
created while the shared store lock is held. Synchronous command/rsync leases
remain present through subprocess completion; tunnel leases remain present
until the tunnel closes. Replacing or renaming the trust-store root after
validation cannot redirect that in-flight pathname to a different key.

A stored record contains both canonical metadata and an OpenSSH known-host line.
The metadata binds all of:

- remote profile ID;
- exact host and port;
- selected host-key algorithm;
- full public key, including key type and encoded key blob;
- independently computed `SHA256:` fingerprint.

The fingerprint is not a substitute for the public key. Loading a binding
recomputes the fingerprint from the stored key and requires the metadata and
OpenSSH line to be byte-canonical.

Enrollment is explicitly two phase:

1. `probe(profile)` runs `ssh-keyscan` with an argv list and a closed request set
   of Ed25519, NIST P-256 ECDSA, and RSA keys. It accepts only exact host/port
   lines and rejects markers, hashed hosts, host lists, duplicate algorithms,
   comments, extra fields, malformed key blobs, and unsupported algorithms.
2. The caller independently verifies and submits an exact algorithm and
   fingerprint from the pending result. `confirm(...)` requires the current
   profile ID/host/port to match, repeats the probe, and requires the complete
   candidate set to be unchanged before it persists the selected full key.

The second `ssh-keyscan` only detects a change between the two observations. It
does not authenticate the server, because an active attacker can answer both
probes consistently. A TOFU enrollment must display the fingerprint for
independent verification through an administrator, provider console, or another
authenticated channel before confirmation.

RSA known-host records use the OpenSSH public-key type `ssh-rsa`; the transport
pins `rsa-sha2-512` as the corresponding host-key signature algorithm. Probe
results reject RSA moduli smaller than 2048 bits and never mutate trust state,
so first contact is not silent TOFU.

Persisted trust is loaded only with an expected fingerprint retained by the
profile owner. Recreating the same profile ID/host/port without that expected
fingerprint does not inherit the old binding. `revoke(profile,
expected_fingerprint=...)` and `rotate(profile,
expected_old_fingerprint=..., confirmed_pending=...)` are atomic compare-and-
swap operations. Before either operation, the caller must close every existing
tunnel and invalidate every transport built from the old binding. An already
running process intentionally retains its lease; revoking the store record is
not a remote-session termination mechanism.

`SshRemoteExecutorTransport` requires a `TrustedKnownHostsBinding` and revalidates
it before building every command. A release call without a binding fails closed.
This slice deliberately does not add a UI, native host route, contract DTO, or
Core API for confirmation; those callers must be wired separately before release
SSH actions can proceed.

## Supported Auth Modes

Supported:

- `ssh_agent`: uses OpenSSH agent/default identity lookup without loading user
  SSH configuration.
- `private_key`: passes `-i <private_key_path>` as a subprocess argv element,
  clears default identities with `IdentityFile=none`, sets `IdentitiesOnly=yes`,
  and disables agent use with `IdentityAgent=none`.

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
ssh -F /dev/null -p <port> \
  [-o IdentityFile=none -i <key> \
   -o IdentitiesOnly=yes -o IdentityAgent=none] \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=<private-lease-file> \
  -o GlobalKnownHostsFile=/dev/null \
  -o UpdateHostKeys=no \
  -o CheckHostIP=no \
  -o VerifyHostKeyDNS=no \
  -o KnownHostsCommand=none \
  -o HashKnownHosts=no \
  -o HostKeyAlgorithms=<confirmed-algorithm> \
  -o BatchMode=yes -l <user> -- <host> <remote-command>
```

`-F /dev/null` prevents `IdentityFile`, `Match`, or `Include` entries in user SSH
configuration from extending private-key authentication. The same base argv is
used for remote commands, the rsync remote shell, and SSH tunnels. Neither
keyscan nor transport subprocesses use `shell=True`.

OpenSSH transport exit `255`, host-key mismatch, process-start, and rsync
failures are converted to typed renderer-safe errors. Raw local SSH/rsync stderr
is only emitted through the controlled deployment logger and is never placed in
the execution result or raised error text; known trust paths are redacted from
non-transport remote-command stderr as a defense in depth.

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
3. creates the remote target directory with `mkdir -p` through the trusted SSH
   binding;
4. runs `rsync -az --delete -e "ssh ... <provider trust options> -l <user>"`
   from `local_path/` to
   `<host>:remote_path/`.

The trailing slash semantics intentionally upload the contents of the local
folder into the prepared remote workspace path.

## Limitations

This slice does not include:

- password or passphrase vault integration;
- OpenSSH config editing;
- UI/native/sidecar contract wiring for host-key confirmation;
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
