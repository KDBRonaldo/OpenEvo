# Core Control Host Service

The release Core Control process is a user-global service on one remote host.
Projects, workspaces, tasks, runs, and evolution revisions are resources owned
inside that Core process. They do not receive independent backend daemons,
service roots, bearer credentials, or fixed-port listeners.

This component is a backend launcher and remote bootstrap primitive. It is not a
new user-facing CLI product. It does not change the frozen Core Control OpenAPI
document or add compatibility routes.

The Desktop-side launcher and its local SSH/rsync subprocesses support both
macOS and Linux hosts. Local leader-exit observation uses Linux
`waitid(..., WNOWAIT)` when available and Darwin `kqueue` process-exit events
otherwise. Darwin closes the pre-registration exit gap with an immediate
non-reaping `ps` PID/PGID snapshot and uses the same bounded snapshot as a
fallback if kqueue registration is unavailable; Linux uses proc status as its
fallback. No portable waiter may reap the leader before group cleanup. All paths
retain the 100 ms descendant-pipe drain and signal the owned process group on
leader exit, timeout, or cancellation. Signal success is not termination
evidence. A bounded Linux `/proc` or portable `ps` observer must still find the
pinned leader and must prove that every member of that PGID is dead or a zombie
before the leader is reaped. Before `Popen`, one subprocess authority is
constructed, inserted in the bounded ownership registry, and given the entered
known-host lease. The
child first publishes its PID, PGID, and SID to that authority's anonymous birth
record FD and then execs the requested command. Losing the `Popen` return to a
`BaseException` therefore leaves the same pre-published owner able to recover
the child within an independent bounded birth-recovery window without a Python
return-to-constructor handoff. This contract covers command/rsync children,
general forwarding tunnels, and per-connection Core tunnels; every production
child receives a new session/process group. Observer construction,
capture, and cleanup failures cannot split the child, registry capacity, or
lease. Unconfirmed cleanup retains that complete owner for later
command/tunnel/close recovery. All waits remain bounded.
This local portability does not extend the remote contract: the Core host must
be Linux and the preflight rejects every other remote platform.

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

Each Desktop bootstrap creates a new random generation. Under the owner-only
`~/.openevo/core/release-staging/` root, the installer moves a new directory
through closed `creating -> pending -> active` names; `pending` records the
exact device and inode but its name alone does not authorize cleanup, and
`active` is entered only after its inode-bound authority file is complete and
durable. It does not create the final release path. A system interpreter
running with `-I` creates the venv there with copied
interpreter binaries, bootstraps pip, installs the uploaded wheel, and imports
the Core service from that staged interpreter. It never imports through
`PYTHONPATH`, writes the user site, or reuses or force-reinstalls an existing
generation. Only then does an atomic no-replace rename publish the same inode as
`~/.openevo/core/releases/<generation>`. The installed generation interpreter then takes the host
bootstrap lock, proves its prefix/import origin/executable metadata, requires the
explicit wheel to be the framework lock's sibling artifact, and verifies the
complete lock-declared Core distribution inventory before entering lifecycle work. A verification
failure leaves the live generation and daemon untouched.

Generation installation is serialized by an owner/inode-verified no-follow
lock. Each active stage also retains a `0600` authority lease through venv,
ensurepip, pip, and import verification; installer children inherit that lease.
Recovery scans at most eight closed-name stage roots and removes only an exact
name-encoded inode through held directory FDs, with fixed node, byte, and depth
budgets. Cleanup first atomically enters `retiring`; the authority is removed
only after all other entries are removed and the still-authorized inode moves
to a random, inode-encoded `discard` tombstone in `release-quarantine`. Recovery
may continue traversing a `retiring` inode only while its exact authority is
valid. It may remove a `discard` tombstone only when it contains that valid
authority alone, or when it is already empty after a durable authority unlink;
it never traverses an authority-less tombstone. A crash before authority
publication, including an authority-free `pending` or legacy `staged-*`
residue, moves that directory without traversal or deletion into owner-only
`release-quarantine`.
`SIGKILL`, ENOSPC, and authority write/fsync failures therefore converge on the
next serialized retry. A busy, malformed active authority, over-budget tree,
replaced bound inode, or otherwise unsafe stage fails closed as
`core_bootstrap_install_failed`; it is never a release and is not deleted by
pathname guesswork.

Concurrent verified generation interpreters serialize daemon attachment and
replacement with the same host bootstrap lock. Direct service ensure calls
additionally hold the lifecycle lock inside that host lock while loading the
verified registry and constructing release identity. The host-locked bootstrap
passes its already-held lock FD to the isolated ensure interpreter, which
validates the owner/mode/inode and held-lock state before entering lifecycle
work. An exact live release
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
PID is never treated as the managed process. CPython's optional
`os.pidfd_open` and `signal.pidfd_send_signal` wrappers are not a release-host
requirement: on x86-64 and AArch64 Linux, Core uses the same kernel pidfd ABI
through a closed, errno-preserving `syscall(2)` adapter when either wrapper is
absent. Unknown architectures, missing kernel syscalls, and failed probes remain
typed startup failures; Core never falls back to PID-only `kill(2)`. Before
`Popen`, the supervisor publishes a generation-specific spawn intent containing
the pinned spawn-lock inode. The child inherits the already locked FD and
replaces that intent with
its PID/boot/start-time claim before loading the registry or starting ASGI.
If the supervisor receives `SIGKILL` anywhere after fork, either the claim is
already recoverable or the inherited lock still identifies the unclaimed child.
Recovery scans `/proc/*/fd` for that exact inode, captures each holder's process
identity, terminates it through pidfd, acquires the spawn lock as a barrier, and
then removes the intent. Invalid or ambiguous ledgers fail closed.

The `/proc/<pid>/fd` asset-consumption anchor and Linux atomic no-replace rename
used below execute on that remote Core host. A macOS Desktop is only the local
SSH/rsync caller; it does not replace or relax those remote Linux primitives.

The Desktop production adapter uses the system Python only as a fixed Ubuntu
bootstrap selector, not as the Core runtime. It searches verified Python 3.13
through 3.11 candidates and may use a verified `uv` to find or install Python
3.11. When uv is absent, x86-64 and AArch64 hosts automatically download a
pinned official uv archive through the configured server proxy, verify its
embedded SHA-256, and execute only its exact uv member from a private unlinked
FD. Selection probes the same direct pidfd syscall ABI as the service,
so uv-managed Python builds without the convenience wrappers remain eligible.
It returns a canonical executable authority that binds path, inode, metadata,
digest, and version. Every later asset and generation command revalidates that
authority and executes its held FD; it never resolves `python3` from PATH again.
Missing Python, failed uv provisioning, and missing kernel syscalls remain
distinct typed failures before any bootstrap claim.

The uv/Python provisioning and wheel-install subprocesses receive the configured
proxy and bootstrap TLS environment. The eventual Core service does not: its
`execve` receives a newly constructed allowlist containing only home, locale,
and PATH values.

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

1. create a fresh generation-scoped isolated venv, install the exact uploaded
   wheel there without user-site mutation, and run the remaining bootstrap with
   that generation's interpreter;
2. under the host lock, verify the generation prefix, import origin, wheel/lock
   binding, and complete lock-declared Core distribution inventory before
   ensuring or replacing the user-global daemon;
3. write the bounded attachment to a unique owner-only file under the pinned
   service root;
4. consume and unlink that file with the same generation interpreter through
   the SSH transport's dedicated `SecretStr` result channel, never
   `RemoteCommandResult.stdout`;
5. for each Core HTTP connection, create a parent-owned `AF_UNIX` socketpair,
   validate both held FDs as anonymous owner-owned `SOCK_STREAM` endpoints with
   stable descriptor identities, and transfer only the peer FD to one new
   `ssh -W` child;
6. check that connection's child authority before and after authenticated status
   traffic, then
   require generation, release, registry, and status-proof equality before
   returning a verified tunnel handle.

The first-stage installer is a closed standard-library script invoked by
`python3 -I`; remote paths use a closed absolute-path grammar and reject the
platform path separator. The script receives no bearer and uses an allowlisted
environment. All Core imports, attachment consumption, verification, lifecycle,
and daemon launch after installation use the generation interpreter with no
`PYTHONPATH` semantics.

The composition-independent adapter adds a transport-owned asset stage before
that plan. Composition supplies only sealed local wheel/framework-lock paths,
sizes, and digests. `openevo.deployment.core_assets` copies those files into a
private no-follow local snapshot, while `SshRemoteExecutorTransport` prepares
the canonical owner-only `~/.openevo/core` subdirectories and performs the
rsync on the same authenticated transport. A remote standard-library verifier
requires the exact two release files plus the owner-bound rsync lease,
owner/mode/link identity, both digests, and the closed lock-to-wheel binding.
Every transfer
uses a unique random incoming authority. Before prepare, the local transport
publishes one of 16 ownership tokens. The same token owns pending receipt,
active upload, and inactive cleanup/reconciliation state until confirmed
publication or discard, and new prepare work is rejected at capacity.
Remote prepare no-follow pins the incoming directory and establishes a fsynced,
inode-revalidated `0600` transfer marker before returning its receipt. A
recognized empty `0700` incoming inode left before marker creation is recovered
immediately; any markerless nonempty or otherwise noncanonical shape fails
closed, so repeated interruption cannot strand all 16 remote slots.
Finalize must acquire and retain the exact lease FD before it owns that incoming directory. A
busy lease leaves the incoming directory, marker, lease, and files unchanged so
a later finalize can retry. After ownership is acquired, finalize copies verified
bytes into an owner-only private
candidate whose inode and pathname were never exposed to rsync. Members are
created as `0400` and populated only through publisher-held writer FDs that are
closed before publication. The candidate directory remains `0700` only while it
is atomically moved from the staging parent to a random private name under the
asset parent, which permits ordinary non-root Linux users to complete the
cross-parent directory rename. There it is sealed to `0500`, reverified through
the pinned FD, and atomically renamed within the same parent to the deterministic
bundle ID. Startup removes bounded, recognized private candidates left in either
parent by a crash. Finalize keeps the verified candidate FD pinned across both
atomic no-replace renames and every pathname verification. Finalize returns a
receipt binding the directory and both member inodes, then retires the incoming
authority while still holding the lease. Only this owner may rename incoming to
its retired name and remove the lease marker. The SSH transport holds that receipt for bootstrap: under the
publication lock it revalidates modes, identities, and digests, then substitutes a
`/proc/<wrapper-pid>/fd/<pinned-bundle-fd>` root while the generation installer
and its nested pip child run. Same-name replacement cannot redirect consumer
reads, and post-consumption revalidation fails closed on mutation or pathname
replacement. An already published exact sealed bundle is an idempotent retry.
The transport retains each prepared transfer until confirmed publication or
discard. An interruption immediately before or after the first prepared
authority update leaves that same token inactive and retryable instead of
stranding pending or active capacity. Upload cleanup has an independent
10-second deadline. Once finalize
starts, timeout, cancellation, authenticated failure, or malformed receipt first
replays the exact finalize transaction and validates its receipt; only a
definitive non-publication result permits incoming discard. A later staging call
retries retained authority before prepare. Upload-to-finalize authority handoff,
remote finalize, and local receipt publication remain one active owned operation,
so concurrent cleanup skips it. A surrounding `finally` always retires that
active marker: interruption before the finalize state is recorded leaves an
inactive upload authority that can be discarded, while interruption after the
record leaves an inactive finalize authority that must be reconciled. Under the
same publication lock,
prepare also reclaims closed incoming attempts inactive for more than 600
seconds only after acquiring the exact transfer lease exclusively. The rsync
server holds that lease across exec, so process restart can recover unlocked
stale capacity without touching a continuously writing or orphaned
cross-process upload or an exact published bundle. Directory mtime alone is
never cleanup authority.
Remote paths are outputs of this verifier, never Desktop configuration or
user-preplaced `/srv` inputs.

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

Core authentication does not use a filesystem pathname created by OpenSSH and
does not reserve a temporary TCP port. For every HTTP connection the Desktop
parent creates a fresh anonymous `AF_UNIX`/`SOCK_STREAM` socketpair and validates
the declared and kernel socket type, effective UID, empty local/peer names, and
initial identity of both held FDs. The identities are rechecked after child
creation, and the parent endpoint is checked again before it is returned. The
parent retains the HTTP endpoint; exactly one
`ssh -W 127.0.0.1:<port>` child receives the peer as stdin/stdout through the
exact `pass_fds` set. There is no `fchmod`: anonymous socket mode and link count
are not an authority boundary, and Darwin may return `EINVAL` for that operation.
There is also no `-L` listener, control master, control socket, pathname pre-pin
window, or same-UID unlink/rebind target. The HTTP layer rechecks child authority
after reading each bearer-authenticated response. A `poll()` exception is an
authority failure rather than evidence that the SSH child remains alive.

Tunnel authentication preserves retryable deadline and daemon-exit failures.
Every path that does not return the verified handle, including cancellation or
another `BaseException`, executes the tunnel's bounded terminate/wait/kill close
path in `finally`. Trust leases are released only after every connection child
exit is confirmed; otherwise process-local orphan quarantine retains ownership
and retries on later tunnel operations or matching trust mutation. Every setup
or authority failure permanently marks the endpoint closing and registers
quarantine ownership under its state lock before either anonymous socket is
closed. Each socket close and the bounded child cleanup then run independently,
so `EBADF` cannot replace the original typed failure or skip process ownership
cleanup. Concurrent opens observe the poison and cannot create another child.
Confirmed whole-process-group exit and leader reap complete endpoint closure;
otherwise quarantine retains the endpoint until a later bounded retry can prove
exit. The production authority enters a single endpoint-owned pending slot before
`Popen`, generation advancement, or registered-child map insertion. A
cancellation or insertion failure retains that exact authority under the
poisoned endpoint. Close deduplicates pending and
registered references by identity and cannot release trust or finalize until
bounded terminate/wait/kill proves every owned child exited.

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

`desktop.sidecar.core_bridge_adapters_v1` now provides that downstream primitive
adapter without performing app composition. It uses only the exact transport
currently owned by `DesktopRemoteLifecycle`, converts the attachment into the
bridge's secret-bearing host authority, and computes a domain-separated
bearer-HMAC identity over the profile and complete release/registry/generation/
status tuple. Its tunnel method requires the same transport object and exact
remote port, calls `open_core_control_tunnel`, and publishes a bridge handle only
after authenticated attachment matching succeeds. The paired HTTPX transport
opens all traffic through `VerifiedCoreControlTunnel.open_verified_socket`; its
synthetic loopback origin never creates a local TCP listener. Deadline,
transport replacement, SSH, bootstrap, and identity failures are normalized to
closed renderer-safe bridge errors without paths, commands, output, or bearer
values. Its HTTP transport uses incrementally decoded response reads for open
SSE streams, valid chunk encoding for unknown-length requests, a 60-second
endpoint I/O ceiling, and a generation/in-flight close barrier that prevents
late socket adoption or bearer transmission. Release provider/app composition
remains intentionally out of scope.
