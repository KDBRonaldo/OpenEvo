# Gateway Service

`openevo.gateway` is the per-worker FastAPI service that runs a session. It accepts
a dispatch from the rollout server, prepares a runtime, runs the agent harness,
**transparently proxies the agent's LLM calls** to a local inference server
(capturing every one), then builds and evaluates a trajectory and reports the
result back.

## Mental model

The agent never knows OpenEvo is in the middle. Before running it, the gateway
injects proxy endpoints as environment variables — `OPENAI_BASE_URL`,
`ANTHROPIC_BASE_URL`, `GOOGLE_API_URL`, with the **API key set to the session
id**. The agent thinks it's calling OpenAI/Anthropic/Google, but every request
lands on the gateway's catch-all route, which:

1. **detects** the API family from the path/headers/body (`detection.py`),
2. **transforms** the request to the served model and adds training fields
   (`transform/`),
3. forwards it to the configured inference server (`engine.py` handles
   SGLang/vLLM specifics),
4. **captures** the request + response as a completion record, and
5. transforms the response back into the shape the agent expects.

Streaming is **synthetic**: even when the agent asks for a token stream, the
gateway makes one non-streaming backend call and replays the full answer as
well-formed SSE — simpler, and enough for capture.

A session moves through staged worker pools: **INIT** (start runtime + run the
prepare recipe) → **READY** (wait for a run slot) → **RUNNING** (harness setup +
run) → **POST-RUN** (build trajectory, evaluate, tear down, call back). Terminal
statuses are `COMPLETED`, `ERROR`, or `TIMEOUT`.

## Main files

- `server.py`: the FastAPI app — the catch-all LLM proxy route, the
  session/admin/health/events endpoints, and synthetic streaming.
- `node.py`: `GatewayNodeManager` — stage handlers, runtime prepare, trajectory
  build + eval, rollout registration/heartbeat, result callback, and the agent
  env injection.
- `dispatcher.py`: stage-isolated worker pools and the
  INIT→READY→RUNNING→POST-RUN transitions.
- `session.py`: in-memory session registry, id validation, and resolving the
  session id from an incoming proxied request.
- `detection.py`: API-family detection (`anthropic` / `openai_chat` /
  `openai_responses` / `google`).
- `transform/`: per-API request/response transformers (see
  [transform](transform/README.md)).
- `engine.py`: inference-backend strategy (SGLang / vLLM) — injects
  token-id/logprob params and canonicalizes responses.
- `proxy.py`: `InferenceClient`, the HTTP client to the inference server, with
  pause/resume generation gating.
- `storage.py`: in-memory completion-record store (the authoritative copy).
- `completion_writer.py`: background task that persists completions to disk off
  the hot path.

## Subscription credentials and session cleanup

Codex subscription sessions are admitted only for an exact Core-managed runtime
profile/image, Docker host-user execution, and literal `capture_mode=transcript`.
Custom images, runtime loaders/options, entrypoints, and caller-supplied
`CODEX_HOME` fail before credential staging. Core fixes `CODEX_HOME` to
`/openevo/credentials/codex`, backed by a dedicated private host directory that
is not below the session, workspace, artifact, or log tree. Runtime prepare and
workspace upload finish while this directory is still empty.

The gateway opens the remote user's `~/.codex/auth.json` with `O_NOFOLLOW` and a
stable descriptor, then requires a user-owned, link-count-one regular file with
private permissions and a bounded size. It creates `auth.json` exclusively as
`0600` under the private `0700` credential root. Source and target size, digest,
identity, link count, and change times are rechecked against their open file
descriptors and final pathnames. A mismatch scrubs the staged inode before
cleanup and aborts without logging credential contents.

Verified auth JSON supplies a bounded set of exact sensitive leaf values. The
gateway redacts those values from stdout/stderr and transcript logs and performs
a bounded no-follow scan of session workspace, artifact, and log files before
export; captures over the content budget are replaced by a redaction marker.
This prevents OpenEvo's own sync, scanner, and capture paths from automatically
copying known credential bytes. The Codex harness remains a necessary trusted
consumer of the credential. OpenEvo cannot prevent that process from actively
transforming or transmitting a secret; that behavior is outside this boundary.

The session root is pinned by device, inode, and owner before runtime startup.
Teardown walks only from that descriptor, never follows links, restores only
owner access needed to enter `000` directories, and enforces depth and node
limits. A replaced root, changed nested entry, or foreign-owned object stops
cleanup without deleting the replacement. Docker teardown marks a runtime
destroyed only after `rm -f` succeeds or a follow-up inspect proves the
container absent. Kill/remove failures remain retryable and retain both session
and credential roots, so cleanup never races a live container.

## What it captures

Each proxied call is stored as a `CompletionRecord` that keeps both the agent's
original request and the served request, plus the response. Records live in
memory (used to build the trajectory) and, when `gateway.completion_persistence`
is enabled, are also written to
`<save_dir>/task_<id>/sessions/<sid>/completions/<NNNN>-<id>.json`.

## Pause and resume

`POST /admin/inference/pause` and `/resume` gate the gateway's **outbound
generation** (the calls in `InferenceClient`). A training bridge pauses new
generation while it syncs weights, lets in-flight calls drain, then resumes —
this pauses inference, not the gateway process.
