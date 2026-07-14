# Core Control Host Service

The release Core Control process is a user-global service on one remote host.
Projects, workspaces, tasks, runs, and evolution revisions are resources owned
inside that Core process. They do not receive independent backend daemons,
service roots, bearer credentials, or fixed-port listeners.

This component is a backend launcher and remote bootstrap primitive. It is not a
new user-facing CLI product. It does not change the frozen Core Control OpenAPI
document or add compatibility routes.

## Host Identity

The caller explicitly supplies or resolves the canonical service root for the
remote OS user, `~/.openevo/core`; the service rejects alternate roots so two
callers cannot create independent daemons for the same user. Path traversal opens every
existing component with no-follow directory semantics. The managed root and
its managed directory children must already be owned by the effective user and
have mode `0700`; startup rejects broader modes and foreign ownership without
changing permissions first.

The root contains the host bootstrap lock, lifecycle lock, spawn lock, bearer,
process/release ledger, pending-start record, readiness record, loopback port,
log, and Core provider state directory. Managed files are owner-only,
link-count-one regular files.
State publication writes and fsyncs a private temporary file, atomically
renames it under the pinned root FD, then fsyncs the directory. Reads are
bounded and verify pathname/inode binding before and after the exact read.

The release identity covers:

- exact framework-lock bytes;
- the verified registry digest;
- every verified distribution artifact digest and installed inventory digest;
- the release source commit.

Concurrent Desktop bootstrap calls take the same host bootstrap lock across
installed-inventory verification, any user-site wheel mutation, repeated
verification in a fresh interpreter, release identity construction, and the
service lifecycle operation. This prevents two Desktop processes from running
concurrent `pip` mutations or verifying an inventory while another process is
changing it. Direct service ensure calls additionally hold the lifecycle lock
inside that same host lock while loading the verified registry and constructing
release identity. The host-locked bootstrap passes its already-held lock FD to
the fresh ensure interpreter, which validates the owner/mode/inode and held-lock
state before entering lifecycle work. An exact live release
performs an authenticated status check and attaches to the existing daemon. A
different live identity returns `core_service_identity_mismatch` unless the
caller explicitly requests controlled replacement. Replacement stops the exact
pidfd-bound process and starts one successor. Every newly spawned generation,
including a same-release restart after process death, rotates the bearer before
Core starts. An old attachment therefore cannot authenticate to a successor.
No request
may attach to a stale or partially verified release.

## Process And Readiness

Linux supervision binds process state to PID, kernel boot ID, and `/proc` start
time. Signalling uses a pidfd after rechecking that full identity, so a reused
PID is never treated as the managed process. Before `Popen`, the supervisor
publishes a generation-specific spawn intent containing the pinned spawn-lock
inode. The child inherits the already locked FD and replaces that intent with
its PID/boot/start-time claim before loading the registry or starting ASGI.
If the supervisor receives `SIGKILL` anywhere after fork, either the claim is
already recoverable or the inherited lock still identifies the unclaimed child.
Recovery scans `/proc/*/fd` for that exact inode, captures each holder's process
identity, terminates it through pidfd, acquires the spawn lock as a barrier, and
then removes the intent. Invalid or ambiguous ledgers fail closed.

The supervisor binds and listens on one IPv4 loopback socket before spawning
Core. Release bootstrap requests port `0`, so the kernel chooses an available
ephemeral port; that selected port is pinned in the service ledger and reused
by exact attach callers rather than fixed per project. The supervisor then
passes that socket and a one-way readiness FD to the launcher. The
launcher accepts only that listening `127.0.0.1` socket, loads the externally
verified framework lock, constructs `create_core_control_app` with
`build_channel=release`, and waits for Uvicorn's ASGI startup and socket startup
to complete before writing the readiness FD.

The supervisor does not publish `ready.json` yet. The launcher binds generation
and release identity headers to Core responses. The supervisor sends the bearer
to both `/version` and `/v1/status`, requires the response headers to match the
spawned generation and release, verifies provider/channel/source and registry
identity, and computes a bearer-HMAC status proof over those values. Only then does it
publish the ready record followed by the authoritative service ledger. A
listener collision, early child exit, malformed response, response-size
violation, duplicate JSON key, or total deadline expiry leaves no visible ready
service.

## Remote Bootstrap Boundary

`openevo.deployment.core_control` provides the API intended for a future
`DesktopReleaseProvider` integration. Its plan is limited to:

1. run one host-locked remote bootstrap operation that verifies the installed
   inventory, conditionally installs the exact uploaded wheel, verifies again
   in a fresh interpreter, and ensures the user-global daemon;
2. write the bounded attachment to a unique owner-only file under the pinned
   service root;
3. consume and unlink that file through the SSH transport's dedicated
   `SecretStr` result channel, never `RemoteCommandResult.stdout`;
4. start one `ExitOnForwardFailure` OpenSSH control master that forwards an
   owner-only Unix-domain stream socket to the selected remote loopback port;
5. pin the forwarding and control socket inodes with private hard-link guards,
   require exact owner/mode/path identity plus a successful control-master
   authority check, and call `/version` and `/v1/status` through a UDS-backed
   HTTP connection with the bearer;
6. recheck that same socket/process/control authority after authentication and
   require generation, release, registry, and status-proof equality before
   returning a verified tunnel handle.

The bootstrap helper itself is imported directly from the uploaded wheel via a
command-scoped `PYTHONPATH`; it does not depend on an earlier uncoordinated
installation of that release. Child verification and lifecycle interpreters
remove that bootstrap `PYTHONPATH`, so they verify and execute the installed
inventory rather than the uploaded archive.

The attachment keeps the bearer out of `repr` and has no general serializer or
renderer-facing response model. Its loopback host/port, release identity, and
authenticated status proof, and explicit `execution_mode=subscription` plus
`capture_mode=transcript` are sufficient for the Desktop sidecar to open and
pin the active project tunnel; only sidecar process memory may retain the bearer.
Typed errors contain no bearer, remote path, raw command, stdout, or stderr.
The secret result has no general serializer or diagnostic projection, and its
`repr` is redacted. The bearer may exist only in the dedicated transport result,
attachment, and verified tunnel handle in sidecar process memory; it must never
be persisted in Desktop resources, logs, operations, or renderer responses.

The local forwarding endpoint is never selected by binding and then closing a
temporary TCP socket. Each Core tunnel receives a short random directory under
`/tmp`, held and repeatedly validated as an owner-owned `0700` directory. OpenSSH
creates `forward.sock` and `control.sock` as owner-owned `0600` stream sockets
with unlink-on-bind disabled. The sidecar hard-links each socket to a private
guard name, preventing inode reuse while the tunnel is live, and requires both
names to retain the same pinned inode before and after every local connect. It
also requires the original SSH child to remain alive and `ssh -O check` to
authenticate the exact control socket. The connected AF_UNIX socket is returned
to the HTTP layer only after the post-connect authority check, so process exit or
endpoint replacement cannot redirect a bearer request to another listener.

Tunnel authentication preserves retryable deadline and daemon-exit failures.
Every path that does not return the verified handle, including cancellation or
another `BaseException`, executes the tunnel's bounded terminate/wait/kill close
path in `finally`. Endpoint paths and trust leases are released only after SSH
exit is confirmed; otherwise existing orphan quarantine retains ownership.

The exhibition release path uses this attachment for subscription execution and
transcript capture. After attach, this bootstrap layer does not start a science run, Gateway,
rollout server, evolution worker, or vLLM process over SSH. Those are Core-owned
resources and services reached through the active tunnel and formal `/v1/*`
Core Control operations. The helper never calls or extends legacy
`/openevo-api` routes.

## Current Limits

This slice establishes host service ownership, startup identity, crash-safe
attach metadata, and the remote bootstrap API. It does not repair or attest the
current provider/store/workspace baseline, implement the cross-session run
owner, or prove serving/evolution successor readiness. Those components retain
their existing review status and must be integrated separately before release
claims extend beyond the launcher boundary.

`openevo.deployment.core_control` owns the exported attachment and verified
tunnel primitives. The Desktop bridge and release-provider branches own session
storage, reconnect policy, provider handlers, and routing Core operations over
that handle. This branch deliberately does not fabricate a release-provider
handler or modify those integration files; release startup remains fail closed
until that downstream wiring is complete.
