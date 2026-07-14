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
fixed origin. Mutations require their contract-declared idempotency and ETag
precondition headers. Public list methods expose only each route's closed query
set and runs are always filtered to the active project.

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

The shared HTTP client is safe for concurrent calls; `close()` is idempotent and
immediately seals the client against new leases. Every response and transport
close, including ordinary response-context exit and a response that arrives
after sealing, is submitted outside the state lock to that client's
fixed-capacity daemon closer. Each client prestarts a dedicated ownership worker;
additional bounded workers may be started on submission. An uninterruptible
synchronous close therefore cannot exceed the caller's total wait bound. On
timeout the client remains permanently closed while the bounded closer retains
accepted old resources until their close calls return. Enqueue and worker
retirement share one lock, so an idle worker rechecks the queue before exiting.
If an additional worker fails to start, the action remains queued and the
prestarted owner executes it; the caller never runs the close action. The owner
is sealed after the closed client has no remaining leases. A closed connection
cannot send its bearer after Desktop switches to another project session or tunnel.

The close seal increments a client session generation. Each public JSON call owns
one generation token and a copy-on-write authority/cache transaction. Network
I/O, bounded body reads, response-model validation, nested public calls, and
cache validation do not hold the close state lock. After all validation succeeds,
the call takes that lock only long enough to linearize its cache transaction and
normal return against close. If the seal linearizes first, the transaction rolls
back and the call returns `core_client_closed`; if the result linearizes first,
close may subsequently seal while the calling thread is rescheduled after the
return point.

SSE parsing and cache validation likewise happen outside the close state lock.
The replay-ledger/cache transaction and frame delivery share one final, short
generation linearization point. A seal that wins that point rejects the frame
without cache or replay authority. If delivery wins first, `close()` may return
before Python resumes the generator at `yield`; the consumer may then observe
that already-linearized frame, which is defined as pre-seal delivery. No frame
whose delivery linearization occurs after the seal is yielded.

After JSON decoding, every nested string key and value is checked for the
bearer, fixed Core tunnel URL/origin, and private Desktop session identity. The
same check applies to decoded SSE data, so JSON Unicode escapes cannot bypass
credential sanitization. Release providers currently generate
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
