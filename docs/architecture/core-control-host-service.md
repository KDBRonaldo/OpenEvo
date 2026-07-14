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

The root contains the single lifecycle lock, bearer, process/release ledger,
pending-start record, readiness record, loopback port, log, and Core provider
state directory. Managed files are owner-only, link-count-one regular files.
State publication writes and fsyncs a private temporary file, atomically
renames it under the pinned root FD, then fsyncs the directory. Reads are
bounded and verify pathname/inode binding before and after the exact read.

The release identity covers:

- exact framework-lock bytes;
- the verified registry digest;
- every verified distribution artifact digest and installed inventory digest;
- the release source commit.

Concurrent bootstrap calls take the same lifecycle lock. An exact live release
performs an authenticated status check and attaches to the existing daemon. A
different live identity returns `core_service_identity_mismatch` unless the
caller explicitly requests controlled replacement. Replacement stops the exact
pidfd-bound process, rotates the bearer, and starts one successor. No request
may attach to a stale or partially verified release.

## Process And Readiness

Linux supervision binds process state to PID, kernel boot ID, and `/proc` start
time. Signalling uses a pidfd after rechecking that full identity, so a reused
PID is never treated as the managed process. A pending ledger permits restart
to terminate an exact child left between spawn and final publication. Invalid
or ambiguous ledgers fail closed.

The supervisor binds and listens on one IPv4 loopback socket before spawning
Core. Release bootstrap requests port `0`, so the kernel chooses an available
ephemeral port; that selected port is pinned in the service ledger and reused
by exact attach callers rather than fixed per project. The supervisor then
passes that socket and a one-way readiness FD to the launcher. The
launcher accepts only that listening `127.0.0.1` socket, loads the externally
verified framework lock, constructs `create_core_control_app` with
`build_channel=release`, and waits for Uvicorn's ASGI startup and socket startup
to complete before writing the readiness FD.

The supervisor does not publish `ready.json` yet. It first calls `/version` and
bearer-authenticated `/v1/status`, verifies release provider/channel/source and
the registry digest, and computes a bearer-HMAC status proof. Only then does it
publish the ready record followed by the authoritative service ledger. A
listener collision, early child exit, malformed response, response-size
violation, duplicate JSON key, or total deadline expiry leaves no visible ready
service.

## Remote Bootstrap Boundary

`openevo.deployment.core_control` provides the API intended for a future
`DesktopReleaseProvider` integration. Its plan is limited to:

1. verify whether the installed wheel and inventory already match the external
   framework lock;
2. only when verification fails, install the exact uploaded Core wheel and
   repeat full verification;
3. ensure or attach the user-global Core daemon;
4. parse a bounded private attachment carrying the bearer and selected loopback tunnel
   metadata;
5. open a tunnel to that exact loopback port when requested.

The attachment keeps the bearer out of `repr` and has no general serializer or
renderer-facing response model. Its loopback host/port, release identity, and
authenticated status proof, and explicit `execution_mode=subscription` plus
`capture_mode=transcript` are sufficient for the Desktop sidecar to open and
pin the active project tunnel; only sidecar process memory may retain the bearer.
Typed errors
contain no bearer, remote path, raw command, stdout, or stderr. The private SSH
transport response necessarily carries the bearer to the sidecar process; it
must never be persisted in Desktop resources, logs, operations, or renderer
responses.

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
