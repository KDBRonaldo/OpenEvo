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
read with route-class byte limits before contract-model validation. The models'
strict and closed configuration rejects coercion and unknown fields while JSON
arrays remain valid encodings of tuple fields. Capabilities must carry the
profile selected by the requested release execution mode; project validation
and run admission must echo the request's expected registry digest, in addition
to the run snapshot and required-revision bindings. Malformed, oversized,
redirected, cross-project, or connection failures become closed local errors
without raw bodies, headers, URLs, paths, or credentials.

The shared HTTP client is safe for concurrent calls; `close()` is idempotent and
immediately seals the client against new leases. Response and transport close
calls move to daemon resource owners so an uninterruptible synchronous close
cannot exceed the caller's total wait bound. On timeout the client remains
permanently closed while detached owners retain the old resources until their
close calls return. A closed connection cannot send its bearer after Desktop
switches to another project session or tunnel.

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
an ID reused for different event data, or a ledger that reaches its bound, fails
closed. The client does not reconstruct event payloads. Workspace publication,
document-change artifact diffs, and
operation request/result/cancellation are likewise validated only through
their Core-owned response models. Strict project, run, service, artifact,
operation, and diagnostic reads establish opaque project-membership bindings.
Operation and diagnostic identity, parent membership, and every log reference
are validated under one lock before any authorization cache entry is committed.
Events without a direct project identity are yielded only when their declared
run or service parent is already bound to the active project; otherwise the
stream fails closed with snapshot-refresh-required semantics.

Workspace upload mutations bind ETags to representation changes. A newly
created upload must issue an ETag distinct from the project `If-Match`; an exact
idempotent replay of the complete create response may retain its upload ETag.
Chunk, abort, and finalize responses change upload state and therefore must
issue a new upload ETag. Finalization independently requires the returned
project ETag to differ from the upload's frozen project ETag.
