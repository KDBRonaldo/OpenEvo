# Desktop Sidecar

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
the generation lease exits through a dedicated delivery barrier shared with
`close()`. Cache transaction commit and lease exit are one linearization: if the
seal starts first, the transaction rolls back and the pending return is replaced
with `core_client_closed`; if delivery commits first, close linearizes after it.
`close()` need not wait for a stalled request thread, and after it returns no
uncommitted result from the sealed generation can be delivered.

SSE parsing and cache validation likewise happen outside the close state lock.
The stream is an explicit iterator; every `__next__` owns a generation lease and
one replay-ledger/cache transaction whose exit uses the same delivery barrier.
A seal that wins replaces the pending return with `core_client_closed` and rolls
back replay authority. `close()` returning is a hard boundary: no uncommitted old
frame may be yielded afterward.

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
