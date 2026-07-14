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
`HOME`, `PATH`, or `CODEX_HOME` fail before credential staging. Core fixes those
values to the managed home, managed binary path, and
`/openevo/credentials/codex`; subscription Codex is invoked as the fixed
`/home/openevo/.local/bin/codex` absolute path. The credential mount is backed
by a dedicated private host directory that is not below the session, workspace,
artifact, or log tree.

The gateway pins every component from the absolute filesystem anchor to the
remote user's `~/.codex/auth.json` and the private credential root, opens the
leaf with `O_NOFOLLOW`, and requires a user-owned, link-count-one regular file
with private permissions and a bounded size. Before runtime creation, it first
durably journals the private credential root, then copies the bytes into a
random `0700` staging child inside that managed root. Size, digest, UTF-8 JSON,
redactor construction, owner, mode, link count, inode, and source/staging path
chains are all verified there. Linux
`renameat2(RENAME_NOREPLACE)` then publishes the complete `0600` inode as the
credential root's final `auth.json`. The empty staging inode's device/inode is
durably added to the cleanup journal before any secret bytes are copied; after
publication, its final full identity is durably recorded before staging returns.

DockerRuntime reopens and revalidates the root and exact auth identities. It
creates a separate empty home view plus an empty target placeholder and pins all
four objects. Docker daemons cannot reliably resolve descriptors in the client's
PID namespace, so Docker receives two Core-owned, daemon-visible sources: the
empty view is bound read-only at `CODEX_HOME`, while the sibling exact auth file
is bound read-only over the view's `auth.json`. Keeping source auth outside the
view prevents a host rename from moving the target mountpoint and revealing a
replacement at the original source pathname. The managed container fixes
`restart=no`; after create, Core requires both mount records' source,
destination, and `RW=false` to match. It rechecks every host pathname-to-FD
binding before create, after create, and before start. Docker starts only the
trusted inert command, then Core compares the adopted view/auth identities inside
a stable exact container before any prepare or agent command. A mismatch stops
the container and fails closed.

Verified auth JSON supplies a bounded set of exact sensitive leaf values. The
gateway redacts those values from stdout/stderr and transcript logs and performs
a bounded no-follow scan only of the unmounted Core log authority before export.
Ordinary workspace inputs, workspace outputs, and artifacts are never redaction
write targets. The scanner preflights the complete per-file, aggregate-byte,
node, and depth budgets before changing a Core capture; a limit breach fails
finalization explicitly and leaves all original bytes unchanged. Result objects
receive a separate recursive in-memory redaction before persistence or delivery.
Credential-capable dispatcher shutdown/cancel/stage handling, Docker create
reconciliation, initialization, execution/postprocess, export, cleanup, and
teardown exception logs never include `exc_info` or a raw traceback. They retain
the exception type and include exception text only after verified credential
redaction.
The Codex harness remains a necessary trusted consumer of the credential.
OpenEvo cannot prevent that process from actively transforming or transmitting a
secret; that behavior is outside this boundary.

The session root is pinned by device, inode, and owner before runtime startup.
Teardown walks only from that descriptor, never follows links, restores only
owner access needed to enter `000` directories, and enforces depth and node
limits. The final redaction scan rechecks every recursive directory pathname
against its opened inode and then rechecks the full absolute root chain. A
replaced root, changed nested entry, or foreign-owned object fails closed.

For subscription sessions, post-run commands finish first. If an evaluator is
configured, Core builds the trajectory and runs that evaluator while its live
runtime references are still valid; only that evaluator path runs before
runtime stop. Core then removes every credential-capable container and proves
absence by pinned container ID. Sessions without an evaluator defer trajectory
construction until after that proof. In both cases, only after absence proof
does Core perform the final capture scan and permit result delivery. Core pins
the full absolute chain of an unmounted, node-scoped Core log authority. Step
stdout/stderr is first written there through a held root descriptor into a
bounded exclusive `0600` regular inode, then published without replacement.
An agent-precreated symlink, FIFO, socket, hard link, or replacement therefore
cannot redirect or block Core's writer. The final reader opens the fixed
`logs/agent/step.xx.stdout.log` components relative to held directory
descriptors with `O_NOFOLLOW | O_NONBLOCK` and accepts only an owned,
link-count-one, bounded regular leaf. Root, directory, and leaf descriptor and
pathname identities are rechecked after write and read.

Verified transcript bytes are passed directly to the existing builder; it does
not reopen the pathname. Post-absence transcript recovery/build uses a separate
bounded finalization deadline, so an exhausted execution budget does not discard
stdout captured before timeout/cancel or rewrite that terminal status as a
finalization error. A configured evaluator still runs under the execution
deadline before runtime teardown. The resulting trajectory or `SessionResult` is
recursively redacted again before registry, evolution export, or callback
delivery. Docker ownership records live in a node-private root outside agent and
evaluator mounts. Startup reclaims abandoned records by exact immutable ID;
Docker always follows removal with an absence `inspect`, including after
successful `rm -f`. Subscription teardown retries failed stop/absence checks a
fixed number of times in the post-run worker. A still-unproven runtime is moved
to periodic reconciliation instead of occupying that worker indefinitely.
The private v6 cleanup journal contains runtime/container and pinned-root
ownership, the exact staged auth-file identity, an explicit recovery phase, and a redacted, closed finalization
authority: request identity, agent terminal state, optional terminal result,
pending status, and timer marks. A terminal agent result is fsynced into that
authority before it becomes the live in-memory terminal state. Once a result
exists, the journal also stores its canonical digest and monotonic
`export_succeeded`/`callback_succeeded` proofs. A required evolution export binds
the normalized backend URL, timeout, fail-open policy, and their canonical
identity digest; the timeout must be finite and positive before entering the
canonical journal. It does not contain auth bytes. Journal transitions are
copy-on-write: a durable pending marker, candidate fsync, atomic replace, and
directory fsync complete before the new phase or proof enters live memory.
Replace/fsync failures roll back the old authority and retain the pending marker,
so restart cannot treat an uncertain transition as permission to delete storage
or roots; live pending status/error likewise remain unchanged until persistence
succeeds. An exact live retry can finish the transition and clear the marker.
The journal directory has a separate immutable root marker in its private parent.
That marker binds the normalized absolute path, every no-follow ancestor
device/inode identity, and the journal-root identity. Restart therefore rejects a
renamed/replaced root or symlinked ancestor while retaining displaced records.
Recovery preflights row count, filename bytes, metadata bytes, per-record size,
and aggregate record bytes for the complete directory before reading any record
content. Successful cleanup removes the session record but retains the empty
journal root and marker as the node's persistent identity authority.
Evolution export uses the stable session source-event identity, and callbacks
carry a result-derived `Idempotency-Key` and digest header. A failed or unknown
response leaves that phase pending; a successful phase is fsynced before the
next cleanup decision and is skipped on retry. Recovery requires the persisted
phase and matching authority. Legacy/malformed records without an explicit
phase retain all data, and missing, disabled, or drifted evolution export config
keeps a required export pending instead of converting it to a no-op. Startup reloads the authority,
re-verifies staged auth only when transcript rebuilding is still needed, proves
every owned container absent, and resumes pending publication. Completion
storage, session/transcript, credential, log roots, and the journal are removed
only after every required export and callback phase is durably successful. Once
authorized, cleanup first uses a separate bounded, recursive, no-follow scan to
locate the journal-bound auth inode by stable device/inode. Same-root and nested
renames are therefore scrubbed without treating an `auth.json` replacement as
authority. A scan limit, race, or missing exact inode fails before ordinary
recursive deletion and retains the root and journal. Historical
publication-handoff records without an auth identity scrub every owned regular
file in this dedicated root before deletion; this is cleanup authority only.
Credential-bearing v5 terminal-finalization journals still fail closed rather
than rebuilding a redactor from a replacement pathname. The
live retry loop applies the same state machine between restarts.

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
