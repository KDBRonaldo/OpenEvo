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
read with route-class byte limits before strict contract validation. Malformed,
oversized, redirected, cross-project, or connection failures become closed
local errors without raw bodies, headers, URLs, paths, or credentials. The
shared HTTP client is safe for concurrent calls; `close()` is idempotent,
prevents new calls, and closes active HTTP/SSE transports.

Core SSE declares `SseFrameV1` as its wire contract. The client bounds each
frame and each reconnectable stream window, accepts only `id`, `event`, and
`data`, validates `data` as the closed `EventEnvelopeV1`, and then strictly
validates the complete `SseFrameV1` before yielding it. It does not reconstruct
event payloads. Workspace publication, document-change artifact diffs, and
operation request/result/cancellation are likewise validated only through
their Core-owned response models.
