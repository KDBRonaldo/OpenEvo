# Desktop Sidecar

The sidecar owns the renderer-facing Desktop Local API and the process-owned
connection to remote OpenEvo Core. The canonical public contract is defined once
in `contracts/v1/app.py`; release implementations must use its provider injection
point instead of registering another route table.

## Release Local Provider

`release_app.create_release_desktop_local_api_app()` creates the real Local API v1
application. It owns one `DesktopProviderStore` for the process lifetime and
requires the native host to supply a Desktop session token, native instance ID,
readiness key, source commit, and private state root.

The current provider implements:

- public `GET /version` with `provider_kind=desktop_sidecar` and the canonical
  OpenAPI digest;
- challenge-bound `GET /health` using HMAC-SHA256 over
  `protocol NUL instance_id NUL challenge`;
- constant-time Desktop session authentication for every `/desktop/v1/*` route;
- `GET /desktop/v1/state` with the process-owned SSH/Core lifecycle state;
- profile and project list/create/get/patch/delete through
  `DesktopProviderStore`, including durable idempotency, signed cursors, ETags,
  and restart recovery;
- profile connect/disconnect plus explicit SSH host-key review and acceptance.
  The sidecar probes without trusting, repeats the probe before confirmation,
  gives credential resolution, trust-store load/probe/confirmation, transport
  construction, and the trusted SSH check one shared 12-second deadline,
  stores only the confirmed fingerprint in Local API resources, and owns the
  trusted known-host file under its private state root. Unconfirmed candidates
  remain only in the process-owned review state and restart recovery removes any
  candidate persisted by an older interrupted implementation.

Connection mutations atomically reserve idempotency capacity, two fixed terminal
response slots for the operation and idempotency documents, profile action
ownership, and a running operation before external SSH work. One process-wide
action lock serializes that full reservation, SSH invocation, and finalization
cycle across every profile, route, and idempotency key. Replacing profile A with
B therefore closes and durably disconnects A before invoking B. Disconnect is
non-displacing: its reservation does not publish `connecting` or alter another
profile, and the sidecar rejects a profile that does not own the process
lifecycle before calling the transport. Success, error, and recovery
cancellation finalize within the reserved slots without another capacity or
request-ETag check. If completion reports an error before commit, the running
reservation retains its terminal capacity until failure is durable. If commit
succeeded before returning an error, the frozen success remains authoritative
and its transport stays open even if concurrent CRUD consumed the released
capacity. Failure finalization resolves the same return ambiguity with a
read-only observation bound to the exact idempotency envelope and reserved
operation. It retries only a proven `running` state. A durable failed operation
authorizes cleanup only while the profile remains durably disconnected and the
process transport still has that profile as owner; exact failed replay repeats
this check so interrupted cleanup converges without closing another owner's
transport. Failed operations retain their bounded `ApiErrorV1`, so exact replays
return the same error and do not repeat remote work. Once any operation is
terminal, its body and ETag are immutable; a late complete/fail call only returns
that terminal and may close the transport owned by its own stale result. Restart
only cancels truly nonterminal reservations, updating their operation and
idempotency documents in the same recovery transaction.
Profile deletion checks for queued, running, or cancelling profile operations in
the same write transaction as the delete, so even a non-displacing disconnect on
an already-disconnected profile retains its resource authority through terminal
publication. Terminal historical operations do not prevent later deletion.

The production credential resolver currently supports `ssh_agent`. Profiles
that select native private-key or password authentication fail closed with
`ssh_credential_unavailable` until the Tauri credential broker supplies an
ephemeral `SSHAuthConfig`; credential values must never enter the Local API or
provider store. Profile proxy URLs and `no_proxy` are projected into the remote
profile, but user information in proxy URLs is rejected by the contract.

Core bootstrap/tunnel operations, activation, validation, runs, artifacts,
services, diagnostics, maintenance, and events remain unavailable in this
provider slice and return a closed `ApiErrorV1` with HTTP 503. They never return
fixture data or a synthetic ready/success state. A successful SSH check reports
Core as `offline` with `core_not_started`; it does not claim a live tunnel.

`core_bridge_v1.py` now provides the strict active-project bridge needed by the
next provider slice. It injects a host-global `CoreHostService`, a tunnel
factory, an opaque adopted-archive source, and a durable persistence adapter.
The bridge owns exactly one generation-linearized project tunnel and
`CoreControlClientV1`; switching or closing seals the previous client before a
new session can publish. Candidate and active generations also own tunnel,
archive-context, and blocking-adapter cleanup. Core and adapter calls pass a
generation/deadline gate before and after external work. Tunnel close is
bounded, observable, and retryable: a timeout or callback failure leaves the
handle and bridge unclosed and blocks a replacement session. A close future
that succeeds at the timeout boundary is consumed as success and is not
resubmitted; only a callback exception permits a new attempt. The tunnel factory
receives only the profile identity and remote Core port, while the bearer
remains between the host service and the strict client and is excluded from
dataclass representations and normalized errors.

Activation negotiates version and verified capabilities, performs an exact
idempotent Core project create only when no durable mapping exists, publishes a
native-folder workspace through the bounded chunk protocol, validates the
authoritative project/head, and persists the host-bound Core mapping. Scratch
projects use Core's signed initial empty workspace. Imported projects accept
only `WorkspaceImportRefV1` and a read-only stream from the archive source; the
bridge contract contains no host path. A lost create response can be retried
only with the persisted canonical create request, its digest, and its
idempotency key. Durable create state distinguishes `pre_create`, `unknown`,
and `bound`: a proven pre-transport failure may accept a new Local action key,
unknown outcome requires exact replay, and a bound project resumes without
another create. If mapping commit is interrupted and the Local draft is edited,
the bound operation first verifies the original request against that Core
project and then converges the new intent through a versioned patch.

Mapped Local edits use Core `patch_project`, the freshly read project ETag, and
a deterministic old/new request key. The mapping records canonical mapped
intent and immutable project/task/workspace content snapshots separately from
mutable Core authority: project ETag, active revision, project `updated_at`, and
registry digest. A legitimate cross-session successor may change only that
mutable authority. After capabilities, project/head agreement, and validation
succeed, compare-and-swap commit increments the mapping generation and retains
the previous version in adapter-owned history; an authority-only version may
repeat the predecessor request digest. Core must sign the required new snapshots
before Desktop accepts task, model/execution, evolution, or workspace changes.
Imported workspace upload IDs are additionally bound to the exact Core project
snapshot, so a workspace revision cannot reuse an earlier upload session.

Host, tunnel, archive open/read/close, and persistence callbacks run through a
fixed bounded executor. A deadline stops result delivery, while any callback
still running remains owned by the cancelled generation. Successful close or
switch waits for that work and all resources; if bounded retirement cannot
prove completion, it returns a typed retryable error instead of announcing the
transition.

Run creation accepts only the active local project ID. The bridge rereads Core
project snapshots, capabilities, validation, and revision head, chooses a
reachable nonterminal successor before the active head, and builds Core's
`RunCreateV1`. Other run, artifact, service, Core operation, log, diagnostic,
maintenance, and event methods preserve the strict Core DTOs and project
membership checks.

This module is not yet wired into `DesktopReleaseProvider` or
`DesktopProviderStore`. The store has no durable Core mapping/create-operation
schema, and the release app has no production host-service, tunnel-factory, or
adopted-archive adapter. Consequently the provider routes above intentionally
remain typed 503 and the release feature flags remain unchanged. Tests use
fake adapters and `httpx.MockTransport`; those fakes are not a release provider.

## Provider Extension

`DesktopLocalApiProviderV1.invoke()` receives the canonical OpenAPI
`operation_id` and the already validated endpoint arguments. The release
provider has a small handler map only for implemented operations; unknown
operations fail closed. Later SSH and Core providers should add verified
handlers behind this interface while keeping the decorators and signatures in
`contracts/v1/app.py` authoritative.

Provider and request-validation failures are normalized by `release_app.py`.
Error responses must remain user-safe: do not include local paths, SQLite
messages, credentials, session tokens, remote commands, or backend URLs.

## Core Control API v1 Client

`core_client_v1.py` is the strict post-bootstrap transport from the Desktop
sidecar to remote Core. A `CoreTunnelConnectionV1` is valid only for one active
project session and one explicit `http://127.0.0.1:<port>` or
`http://[::1]:<port>` SSH-tunnel origin. The caller must issue its bearer with a
CSPRNG at 256 bits or stronger and must replace the connection when the active
project session changes.

The client creates its own `httpx.Client`; tests may inject only a transport.
Environment proxy discovery and redirects are disabled. Discovery calls are
unauthenticated, while every `/v1` request attaches the bearer only to the
fixed origin. The client first validates and pins one release `openevo_core`
`/version` response whose OpenAPI digest is exactly
`315dc90907f14347d07f7903d360009b271372302b38a1e4adca5bc14486497a`.
Every authenticated `/v1` call fails before transport until that negotiation
succeeds; simulator, scaffold, dry-run, development, and changed release
identities are rejected. Mutations require their contract-declared idempotency
and ETag precondition headers. Public list methods expose only each route's
closed query set and runs are always filtered to the active project.

Core owns newly created project IDs, while an ordinary `CoreControlClientV1`
is already bound to exactly one project. New-project setup therefore uses the
narrow `CoreProjectBootstrapClientV1`: after the same release `/version`
negotiation, it may submit one idempotent `ProjectCreateV1`, verifies that the
returned draft exactly matches the requested project snapshot, and returns a
new `CoreTunnelConnectionV1` bound to Core's generated ID. An exact replay of a
delivered success is local. The first request and idempotency key are frozen
before transport, so an unknown network outcome can only be retried exactly;
a different request or key is rejected even when no response was delivered.
Initial draft validation rejects an already published imported workspace and
requires the documented scratch/imported workspace snapshot shape. Result
validation, connection binding, replay-state commit, and delivery share the
same generation barrier as `close()`; lock wait and transport share one deadline.
The project-bound client rejects `create_project` before transport, preventing
an orphan project followed by an active-project mismatch.

Requests are exact Pydantic v1 DTOs. JSON responses and `ApiErrorV1` bodies are
read with route-class byte limits before contract-model validation. A generic
model-generated JSON Schema pass recursively rejects scalar coercion and
unknown object fields before Pydantic validation while preserving JSON arrays
as valid encodings of tuple fields. The first valid capabilities response pins
the client lifetime's exact release execution profile and registry digest.
Later capability reads, project validation requests/responses, project
snapshots, and run requests/responses must match that authority, in addition to
the run snapshot and required-revision bindings. Capability and cached Core
project registry digests are compared exactly regardless of which response
arrives first; a missing project digest does not match a pinned capability
digest. Malformed, oversized,
redirected, cross-project, or connection failures become closed local errors
without raw bodies, headers, URLs, paths, or credentials.

Every client requires a finite positive timeout, and every component of an
`httpx.Timeout` must be finite. The timeout is also the hard wall-clock budget
for the complete public operation, capped at 300 seconds. The same deadline
covers transport send, redirects, bounded JSON/error reads, nested client calls,
and the full SSE stream window; trickle traffic cannot renew it. Synchronous
transport calls run on one process-wide, fixed eight-thread daemon executor, so
a transport that ignores cancellation cannot create unbounded owner threads.
Queued work is cancelled at deadline when possible, and a late response is
closed through the bounded resource closer. Mutations are submitted exactly
once and are never replayed automatically after timeout or connection failure.

The shared HTTP client is safe for concurrent calls; `close()` is idempotent and
immediately seals the client against new leases. Every response and transport
close, including ordinary response-context exit and a response that arrives
after sealing, is submitted outside the state lock to one process-wide bounded
queue served by exactly four prestarted daemon workers. Creating more clients
does not create closer or ownership threads. An uninterruptible synchronous
close cannot exceed the caller's wait bound. Each client transport and each
outbound response reserves one globally bounded close-ownership slot before
network I/O. The reservation makes the later close submission non-droppable;
when capacity is exhausted, the next request fails before transport. Failed
close actions permanently seal that client against new leases, and an
unexpected failed submission remains client-owned for bounded retry. A closed
connection cannot send its bearer after Desktop switches to another project
session or tunnel.

The close seal increments a client session generation. Each public JSON call owns
one generation token and a copy-on-write authority/cache transaction. Network
I/O, bounded body reads, response-model validation, nested public calls, and
cache validation do not hold the close state lock. After all validation succeeds,
generation admission surrounds the copy-on-write cache transaction. On the
transaction's final exit, one delivery-barrier critical section shared with
`close()` performs the deadline/generation check, cache commit or rollback,
delivery linearization, and lease release. If the seal starts first, the
transaction rolls back and the pending return is replaced with
`core_client_closed`; if delivery commits first,
close linearizes after it. `close()` need not wait for a stalled request thread,
and after it returns no uncommitted result from the sealed generation can be
delivered.

SSE parsing and cache validation likewise happen outside the close state lock.
The stream is an explicit iterator; every `__next__` owns one generation
admission around one replay-ledger/cache transaction. Its final exit uses the
same atomic delivery-barrier commit and lease release as JSON. A seal that wins
replaces the pending return with `core_client_closed` and rolls back replay
authority. `close()` returning is a hard boundary: no uncommitted old frame may
be yielded afterward.

Before URL/request construction, path segments (including their decoded form),
query values, cursors, caller-provided headers, and decoded request bodies are
recursively checked for the bearer, fixed Core tunnel URL/origin, and private
Desktop session identity. The active project identity is checked when the
connection is created. The same recursive check applies after JSON/error/SSE
decoding, so percent or JSON Unicode escapes cannot bypass credential
sanitization or place private values in a request URL/access log. Release providers currently generate
`Idempotency-Key`, `Last-Event-ID`, and SSE `id` values as visible ASCII. The
client rejects non-ASCII or control characters instead of percent-encoding
them; this is a temporary release implementation constraint, not a broader Core
opaque-ID contract change.

Core SSE declares `SseFrameV1` as its wire contract. The client bounds each
frame and each reconnectable stream window, accepts only `id`, `event`, and
`data`, validates `data` as the closed `EventEnvelopeV1`, and then strictly
validates the complete `SseFrameV1` before yielding it. A client-lifetime,
bounded ledger binds every SSE ID to the canonical validated event digest across
reconnects. Exact semantic replays are accepted even if JSON formatting differs;
after their canonical digest matches, they are no-ops and do not reapply
authorization or resource state. An ID reused for different event data, or a
ledger that reaches its bound, fails closed. The client does not reconstruct event payloads. Workspace publication,
document-change artifact diffs, and
operation request/result/cancellation are likewise validated only through
their Core-owned response models. Strict project, run, service, artifact,
operation, and diagnostic reads establish opaque project-membership bindings.
Operation and diagnostic identity, parent membership, and every log reference
are validated under one lock before any authorization cache entry is committed.
Status and paginated project, run, service, and artifact snapshots validate into
temporary cache copies and publish as one update; a late invalid item leaves no
membership or resource-cache residue.
Events without a direct project identity are yielded only when their declared
run or service parent is already bound to the active project; otherwise the
stream fails closed with snapshot-refresh-required semantics.

Workspace upload snapshots bind each strong ETag one-to-one to one canonical
representation for that upload: neither the same ETag with different state nor
the same state with a different ETag is accepted. Offset, status, and update
time cannot move backward. A newly created upload must issue an ETag distinct
from the project `If-Match`; an exact idempotent replay of the complete create
response may retain its upload ETag. Chunk, abort, and finalize responses change
upload state and therefore must issue a new upload ETag. Finalization
independently requires the returned project ETag to differ from the upload's
frozen project ETag.
