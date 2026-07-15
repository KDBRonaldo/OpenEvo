# Desktop/Core Contract v1

Issue #163 defines the first release contract for the exhibition-ready OpenEvo
Desktop. This document fixes ownership and behavior. The checked-in OpenAPI
documents and conformance tests fix the exact JSON schemas.

This is a product boundary, not a new product surface. OpenEvo still ships only
OpenEvo Desktop and OpenEvo Core Backend.

## Ownership

```text
React renderer
  -> Desktop Local API v1
local sidecar
  -> Core Control API v1 through the active SSH tunnel
remote OpenEvo Core Backend
```

The Tauri/Rust host owns the native process lifecycle, Desktop session
credential, macOS Keychain access, native file selection, and secret handoff.
The renderer never receives SSH passwords, key passphrases, backend bearer
tokens, proxy passwords, raw host paths, remote commands, or the Core URL.

The sidecar owns local profiles and drafts, pre-Core SSH/bootstrap operations,
the active tunnel, version negotiation, response validation, error
normalization, and event aggregation. Once Core reports compatible readiness,
the sidecar must not launch science runs or Core child services through SSH.
The host-service layer exports a secret-bearing attachment and a verified tunnel
handle only after bearer-authenticated `/version` and `/v1/status` bind the
tunnel to the attachment generation, release identity, registry identity, and
status proof. The Desktop bridge/release provider owns retaining that handle and
routing later `/v1/*` calls; host-service code does not synthesize provider
handlers, and release startup fails closed until that integration is wired.
For the macOS Desktop path, the local endpoint is an owner-only OpenSSH
streamlocal socket rather than a released-and-reacquired TCP port. The handle's
HTTP connector revalidates its pinned socket inode guards, SSH process, and
control-master authority for each connection before bearer-bearing bytes are
sent.

The composition-independent production implementation lives in
`desktop.sidecar.core_bridge_adapters_v1`. Its host and tunnel protocols share
one in-memory attachment authority bound to the exact active
`DesktopRemoteLifecycle` transport object and profile. Bootstrap delegates to
the verified generation installer/service attachment in
`openevo.deployment.core_control`, but only after a same-transport runtime
preflight and sealed-asset stage. Composition provides exact local wheel and
framework-lock identities; the transport derives the private remote root,
re-hashes both files, verifies their lock binding, and atomically publishes the
bundle before bootstrap. It never depends on a user-preplaced remote file.

The pre-Core SSH phase selects one Linux Python 3.11+ runtime authority before
upload. Selection checks PATH Python 3.13 through 3.11 and then verified `uv`
executables in PATH and standard user locations. If `uv python find 3.11` has no
candidate, Desktop may run `uv python install 3.11` with the configured
HTTP(S)/NO_PROXY environment. If uv is also absent on x86-64 or AArch64 Linux,
Desktop fetches a pinned official uv 0.11.28 archive through that same proxy
environment, enforces a 64 MiB response bound and an embedded platform SHA-256,
and extracts only the exact regular uv member into a private temporary inode.
It never runs `curl | sh` or the upstream installer script.

Candidate stdout is not an authority. The selector canonicalizes and no-follow
opens the absolute executable, checks regular-file ownership, mode, link count,
size and version, hashes its bytes, and directly probes the Linux pidfd syscall
ABI plus boot identity. Python-level `os.pidfd_open` and
`signal.pidfd_send_signal` wrappers are not required because Core uses the same
direct syscall fallback. The resulting private authority binds path, inode,
metadata, digest, version, and a canonical opaque ID. Asset preparation and
finalization, asset consumption, venv creation, wheel installation, and service
bootstrap each reopen and revalidate that authority and execute the held FD;
independent SSH processes never re-resolve PATH. Runtime errors separately name
missing Python, uv provisioning/network failure, unsupported kernel syscalls,
and invalid selection responses. This internal change does not alter frozen
Desktop/Core OpenAPI or event contracts.

The generation installer serializes recovery and installation under a verified
owner-only lock. It moves venv roots through inode-encoded
`pending -> active -> retiring` stage names, keeps a child-inherited authority
lease through ensurepip, wheel install, and staged import verification, and
atomically publishes to `releases/<generation>` only after success. Recovery can
finish a valid-authority retiring inode, then move the empty inode to a random
inode-bound tombstone before removing authority. An authority-less tombstone is
eligible only for an empty-directory removal; it is never traversed. A crash
before authority publication is preserved in `release-quarantine` without
traversal and cannot block the next retry. Unsafe bound
state remains a typed fail-closed error. Exit 73 maps to
`core_bootstrap_install_failed`, whose Desktop projection contains no command,
path, proxy value, output, or secret.

Remote asset finalize owns incoming only after it acquires the exact rsync lease.
A busy lease leaves the directory, marker, lease path, and files unchanged. Once
acquired, the lease FD remains held through verification, publication, and
retirement. Prepare may create another bounded transfer while the first lease is
active, and stale recovery skips held leases.

Network proxy and bootstrap TLS variables are scoped to uv/Python provisioning
and isolated wheel installation. Before the generation interpreter becomes the
long-running Core service, the launcher builds a separate environment containing
only `HOME`, locale, and `PATH`; proxy and proxy-CA variables cannot cross the
service `execve` boundary.

Tunnel publication delegates to the authenticated Core tunnel verifier. The
bridge HTTP transport opens a fresh anonymous socketpair plus `ssh -W` child for
each connection and treats the loopback URL in `CoreTunnelHandleV1` only as
client origin authority, never as a bound TCP listener. It incrementally
delivers small chunked/SSE frames, applies legal request chunk framing, caps
individual endpoint I/O at 60 seconds, and closes through a generation/in-flight
barrier that rejects late socket adoption. The adapter also exposes only exact,
pre-adopted workspace import ownership to `WorkspaceImportStore.resolve`, whose
unlinked read-only snapshot is the sole archive stream. No adapter exposes a
host path.

The packaged `release_runtime.py` instantiates this adapter together with the
durable bridge store, strict Core bridge, adopted-import source, event broker,
event relay, and `DesktopReleaseProvider`. Composition is all-or-nothing before
the Local API advertises the complete release feature set. Project activation
publishes the generation-bound Core session used by Core-owned routes; missing
or stale session authority fails those routes closed. Local doctor, repair,
workspace-sync, and Local operation log/cancellation workflows remain separate
provider operations and are not inferred from bridge availability.

Core owns durable projects, immutable task/workspace snapshots, capabilities,
validation, services, runs and attempts, transcript capture, datasets,
evolution jobs, artifacts, revision transitions, diagnostics, and recovery.

## Compatibility

Both boundaries expose unprefixed `GET /version` and `GET /health` discovery
routes. All other routes use a major-version prefix:

- renderer to sidecar: `/desktop/v1/...`
- sidecar to Core: `/v1/...`

`/version` returns the preferred and supported major versions, the canonical
OpenAPI SHA-256, build version, source commit, build channel, provider kind,
and declared feature flags. Client and server select the highest common major.
No common major returns HTTP 426 with `contract_version_unsupported`; there is
no compatibility fallback to legacy routes.

Desktop Local `/health` also serves as the native-host readiness proof. Tauri
sends a fresh lowercase 32-byte challenge in
`X-OpenEvo-Native-Challenge`; a packaged sidecar returns the closed
`openevo-native-sidecar-v1` protocol, instance ID, and challenge-bound HMAC
proof. The three proof fields are all present or all absent. The Desktop
session token is never part of this unauthenticated response.

Release builds reject providers that report `contract_simulator`, `scaffold`,
`dry_run`, an unknown contract digest, an unverified Core registry, or a Core
connection outside the active project tunnel. Such providers may be used only
by explicit development and test builds.

The Desktop release Core client accepts only `provider_kind=openevo_core`,
`build_channel=release`, and the frozen Core Control API v1 OpenAPI digest
`006fbe0ad33497329912280d9836bd1dce44f49f26fb018a9d9ba6bdf33b62ed`.
It pins the complete first accepted version response. Every bearer-authenticated
`/v1` request requires that pin and fails before transport without it.

The Core client authority is project-bound after project creation. Because
Core assigns a new project's opaque ID, Desktop first creates it through a
one-shot bootstrap authority on the active private tunnel. That authority uses
the same release version pin, idempotency contract, bounded transport, and
response validation as the ordinary client. It verifies every request-owned
field and the fixed initial state of the returned draft against the submitted
`ProjectCreateV1` before deriving the project-bound
connection. The first canonical request and idempotency key are frozen before
transport; an unknown outcome permits only an exact retry. The returned draft
must have the documented initial workspace shape and no prior publication or
active revision. Validation, binding, replay-state commit, and delivery are one
close-generation transaction. A project-bound client cannot call project
creation; it fails before transport rather than creating a Core project it
cannot subsequently adopt.

## Common Protocol

Every JSON model is closed: unknown fields are errors. IDs are opaque UTF-8
strings and must never be parsed for host paths or implementation identity.
Timestamps are UTC RFC 3339 strings. Digests are lowercase SHA-256 hex.
JSON arrays are the wire representation for ordered collection fields even when
the sidecar keeps the validated value as an immutable Python tuple. Request
validation may normalize an actual decoded JSON list to that tuple, but must
reject strings, mappings, scalars, and other container types instead of coercing
them into an array.

Every error uses `ApiErrorV1`:

```json
{
  "schema_version": "1",
  "request_id": "opaque-request-id",
  "code": "stable_machine_code",
  "http_status": 409,
  "message": "User-safe explanation.",
  "severity": "blocking",
  "category": "run",
  "retryable": true,
  "repair_action": "openevo_can_retry",
  "next_action": "Retry after the remote service is ready.",
  "details": {},
  "logs_ref": null
}
```

Create and action requests require `Idempotency-Key`. A provider persists the
fixed `desktop-local-v1` principal, route, resource scope, key, canonical
request digest, typed response, and status. Action digests cover the canonical
body, `If-Match`, and every declared semantic header; callers cannot substitute
an ETag copied into the body. Replaying the same request revalidates the stored
response against the route's exact closed response model before returning it.
Reusing the key for a different request returns `409 idempotency_key_reused`.
Desktop session tokens and other credential-bearing headers are not accepted as
principal or semantic idempotency data. Mutable resources use ETag and
`If-Match`.

The release sidecar stores Local API v1 profiles, project drafts, local
operations, bounded server-side pagination cursors, and idempotency records in
its versioned SQLite provider store. The private state root is a real owner-only
`0700` directory. A non-blocking process-lifetime owner lock prevents two
cooperating sidecars from owning it concurrently. Before opening SQLite and
before each write, the provider uses `lstat`/no-follow checks to require the main
database and any rollback-journal side file to be owner-only, link-count-one
regular files. WAL/SHM files are rejected. SQLite opens the canonical database
path normally and uses the standard `DELETE` rollback-journal and `FULL`
synchronous crash-recovery path; there is no custom VFS, `/dev/fd` database
opening, journal inode pin, or claim that Python's `sqlite3` provides those
properties.

The cursor signing key is fully written to an owner-only temporary file,
fsynced, atomically published with no-replace semantics, and followed by a
state-root directory fsync. Concurrent first initialization cannot replace the
winning key. An invalid-size final key left by an interrupted first
initialization may be recovered only while the database is still the
never-initialized empty file and no SQLite side file exists; an initialized or
ambiguous store fails closed.

This filesystem boundary protects against accidental sharing, symlink or
hard-link setup present at a validation point, and concurrent cooperating
sidecars. It does not isolate the store from a malicious process running as the
same OS user: such a process can race pathname checks or modify owner-readable
state. Desktop relies on the macOS user-account boundary and the owner-only
state directory for that threat boundary.

Schema v5 has an exact canonical `sqlite_schema` fingerprint and retains exact
v1-v4 historical fingerprints. Each historical layout and migration ledger is
validated before the next transactional migration; DDL, ledger, project-copy,
authority publication, and `user_version` changes share one crash transaction.
Forged ledgers, near-match schemas, unknown views/triggers/indexes, and partial
migrations fail closed. Startup performs a database-size-bounded
`integrity_check(1)`, `foreign_key_check`, bounded row/byte reconciliation, and
complete validation of migration, resource, operation, cursor, canonical
JSON/blob, duplicated scalar, timestamp, version, and typed idempotency rows.

The v5 `provider_storage_usage` singleton is the normal-transaction authority
for the complete provider recovery row/byte budget, the 256 KiB per-value and
16 MiB aggregate remote-state budget, and fixed terminal reservations. It also
contains exact idempotency/cursor row counts, a generation, and four modular
remote-content accumulators. Canonical row triggers update it transactionally
and require exactly one affected authority row; its seal is a domain-separated
HMAC under the owner-only signing key. The migration ledger becomes immutable at
v5. The authority singleton rejects every later insert and delete, so `DELETE`
followed by insert and `INSERT OR REPLACE` are rejected even when SQLite
recursive triggers are off. Rollback restores data and authority together.

Each non-null `RemoteProjectStateV1` BLOB has a per-project HMAC-derived content
token over the project ID and exact canonical bytes. Project triggers add and
subtract those tokens from the authenticated accumulators, and guarded project
reads recompute the token before JSON parsing. Normal reads and writes validate
the fixed-size schema and singleton in O(1) relative to provider data; they do
not run table `count`/`sum(length(...))` scans. The process caches the last
committed generation and seal, which rejects replay of an older signed authority
during that process lifetime. Foreground idempotent writes use the singleton's
exact count and first reclaim at most 128 expired cleanup-eligible rows through
the v5 `(cleanup_eligible, expires_at_epoch)` index. The current request's exact
key may then cause one additional primary-key deletion, making the precise
idempotency bound 128 sweep rows plus one exact-key row. A nonterminal operation
remains ineligible until terminal publication atomically changes that state.
Cursor writes have a strict 128-row cleanup maximum through their expiry index.
Thus cleanup work is bounded independently of table size, live action replay
remains available, and later writes converge any remaining expired backlog.

After authenticating the singleton and before any startup mutation, open compares
the configured idempotency and cursor limits with their persisted exact counts.
If either configured limit is lower, open raises
`ProviderCapacityConfigurationError`, performs no cleanup, and commits no startup
state. Repeated incompatible opens therefore cannot enter a rollback-only cleanup
loop. Reopening with each limit at least equal to persisted usage is the recovery
path; later successful writes can commit bounded cleanup. Startup and v4 -> v5
migration may perform one bounded reconciliation of actual table totals, remote
lengths/tokens, exact idempotency/cursor counts, and live reservations before any
remote payload is decoded. After creating the singleton and migration row,
migration validates both the final write budget and configured limits before
seal, `user_version`, and commit; overflow or a lower configured limit rolls the
entire transaction back to v4. The singleton itself consumes a fixed conservative
512-byte recovery reservation, avoiding recursive accounting of its changing
decimal counter representation.

This authentication detects budget-changing partial SQLite edits and remote
content edits while the signing key remains confidential, including equal-length
remote JSON replacement, counter replay, and trigger removal or alteration. Other
same-length, model-valid resource-field edits remain within the owner-only state
directory threat boundary. The authority is not a trusted monotonic clock:
an offline attacker who can restore a complete earlier, internally consistent
database snapshot, or who can read the owner-only signing key and coherently
rewrite all authenticated state, is outside this module's detection boundary.
Detecting that rollback requires a platform-protected monotonic anchor outside
the SQLite database and key file. Database and journal byte limits, SQLite
`max_page_count`, and `journal_size_limit` remain independently enforced before
commit.

The project row stores `RemoteProjectStateV1` canonical JSON in a nullable
private BLOB separate from the canonical `ProjectCreateV1` intent document.
Activation accepts only a ready projection whose active revision project matches
its Core-owned `core_project_id` and whose revision ID matches the local
`current_revision_id`; Local and Core project IDs remain distinct identities.
Activation demotion, target publication, terminal operation result, and
idempotency replay commit in one SQLite transaction. Non-activation completions
cannot publish a remote projection.

Startup atomically recovers process-owned transient state: remote profiles
become disconnected, active project sessions return to draft with stale local
revision pins cleared, and interrupted or now-stale local operations are
cancelled against that authoritative resource state. The remote project value
is retained with its `observed_at` as a historical observation, but is not live
tunnel/Core authority; after local runtime reset it cannot authorize a run or
revision use. Ordinary demote/archive transitions preserve the same history.
Any project intent patch clears both revision and remote state, and demotes
`active`/`blocked` to `draft`, in the same single-version ETag update. This lets
Desktop activate to obtain capabilities, save edited evolution intent, and then
require reactivation. Queued/running/cancelling project operations still block
the patch. Action idempotency stores
the exact `LocalOperationV1`; replay resolves its current authoritative
operation row and cannot return an obsolete connected/active result. Resource,
operation, and idempotency writes commit in one transaction. Local-operation
reconciliation uses bounded keyset batches and fetches each bounded document
row individually; startup never materializes the complete operation BLOB set in
memory.

The store persists only closed Local API fields. Unknown evolution method
config is recursively checked with case- and separator-normalized denylisted
keys for credentials/secrets, host paths, and raw diagnostics. The only
credential data persisted here is Keychain slot status; secret values, native
credential references, commands, raw process output, Core URLs, and
bearer/session tokens are not persisted in resource or idempotency data.
Idempotency responses are retained for a bounded seven-day replay window and
the live record count is bounded; exhaustion fails closed without evicting an
unexpired replay. Pagination uses bounded, signed opaque tokens whose UTF-8 sort
anchors are stored server-side, so all contract-valid names fit without
expanding the public cursor limit.

At most one project row may be active. Activation switches projects atomically.
An active project cannot be patched in place, and a connected profile cannot
change host, port, user, authentication kind, or proxy settings. This prevents
persisted configuration from diverging from the process-owned session that was
admitted from it.

Project evolution config is accepted exactly to the aggregate range enforced
by `ProjectCreateV1`/`ProjectPatchV1`; the store consumes that authoritative
contract budget and adds no divergent per-project method-config limit.

### Release Local provider phase one

The first production provider slice is created by
`desktop.sidecar.create_release_desktop_local_api_app`. It binds an implementation
to the existing contract app by canonical `operation_id`; it does not register a
second route table. Calling `create_contract_app()` without a provider retains
the contract-only 501 behavior used for schema generation, and the release app's
generated OpenAPI document must remain byte-for-byte canonical with that app.

This phase owns the challenge-bound native health proof, Desktop session
authentication, disconnected local state snapshot, and durable profile/project
list/create/get/patch/delete routes. Resource responses carry the same ETag in
the response header and closed response body. Store cursor, idempotency, ETag,
recovery, and restart behavior is surfaced directly, while store failures are
normalized to user-safe `ApiErrorV1` responses without filesystem or SQLite
details.

SSH, Core tunnel, capability validation, operation execution, run, artifact,
service, diagnostic, maintenance, and event providers are not part of this
phase. Their contract routes return typed HTTP 503 rather than fixture 501 or a
synthetic ready/success response. Subsequent providers extend the same
`DesktopLocalApiProviderV1` operation dispatch after they can satisfy the
corresponding SSH/Core ownership and attestation requirements.

List routes use `limit` (maximum 100), `after`, `sort`, and `direction`, and
return `{items, next_cursor, has_more}`. A cursor is bound to the filters and
sort order. Its server-side boundary contains the typed sort value and resource
ID; continuation never re-reads a mutable anchor row, so deleting or editing
that row does not change the next-page boundary. `has_more` is true if and only
if `next_cursor` is non-null. Invalid cursors return 400; expired cursors return
410. The bounded HMAC token carries its version, issued and expiry times, and
query binding. Providers verify the signature, structure, and binding before
expiry, so TTL cleanup of the server-side boundary cannot turn a valid expired
cursor into an unknown-cursor 400.

Core SSE uses closed `SseFrameV1` objects whose wire `id` and versioned `event`
must exactly match `data.id` and `data.event` in the typed `EventEnvelopeV1`.
Providers support `Last-Event-ID`, at-least-once delivery, 15-second heartbeats,
and bounded replay. An expired event cursor returns 410; the renderer reloads
snapshots before subscribing again.

## Desktop Local API v1

Only `GET /version` and `GET /health` are unauthenticated. Tauri returns the
sidecar endpoint, negotiated contract metadata, and a fresh Desktop session
token directly from `start_sidecar`. The token never appears in an HTTP
discovery response. All `/desktop/v1` calls use
`X-OpenEvo-Desktop-Session`.

The release surface is:

```text
GET    /desktop/v1/state
GET    /desktop/v1/profiles
POST   /desktop/v1/profiles
GET    /desktop/v1/profiles/{profile_id}
PATCH  /desktop/v1/profiles/{profile_id}
DELETE /desktop/v1/profiles/{profile_id}
POST   /desktop/v1/profiles/{profile_id}/connect
POST   /desktop/v1/profiles/{profile_id}/disconnect
POST   /desktop/v1/profiles/{profile_id}/host-key/accept

GET    /desktop/v1/projects
POST   /desktop/v1/projects
GET    /desktop/v1/projects/{project_id}
PATCH  /desktop/v1/projects/{project_id}
DELETE /desktop/v1/projects/{project_id}
POST   /desktop/v1/projects/{project_id}/activate
POST   /desktop/v1/projects/{project_id}/doctor
POST   /desktop/v1/projects/{project_id}/repair
POST   /desktop/v1/projects/{project_id}/bootstrap
POST   /desktop/v1/projects/{project_id}/workspace-sync
GET    /desktop/v1/projects/{project_id}/capabilities
POST   /desktop/v1/projects/{project_id}/validate

GET    /desktop/v1/operations/{operation_id}
GET    /desktop/v1/operations/{operation_id}/logs
POST   /desktop/v1/operations/{operation_id}/cancel

GET    /desktop/v1/runs
POST   /desktop/v1/runs
GET    /desktop/v1/runs/{run_id}
DELETE /desktop/v1/runs/{run_id}
POST   /desktop/v1/runs/{run_id}/cancel
POST   /desktop/v1/runs/{run_id}/retry
GET    /desktop/v1/runs/{run_id}/timeline
GET    /desktop/v1/runs/{run_id}/logs
GET    /desktop/v1/runs/{run_id}/context
GET    /desktop/v1/runs/{run_id}/artifacts
GET    /desktop/v1/artifacts/{artifact_id}
GET    /desktop/v1/artifacts/{artifact_id}/content
GET    /desktop/v1/artifacts/{artifact_id}/diff

GET    /desktop/v1/services
POST   /desktop/v1/services/{service_id}/restart
GET    /desktop/v1/services/{service_id}/logs
GET    /desktop/v1/core/operations/{operation_id}
GET    /desktop/v1/core/logs/{logs_ref}
POST   /desktop/v1/diagnostics
GET    /desktop/v1/diagnostics/{diagnostic_id}
DELETE /desktop/v1/diagnostics/{diagnostic_id}
POST   /desktop/v1/maintenance/cache-cleanup
GET    /desktop/v1/events
```

Only sidecar-owned connection, host-key, bootstrap, repair, activation, and
workspace-sync actions return `LocalOperationV1`. Core-owned runs, service
actions, diagnostics, and cleanup resources retain their Core v1 response
shape after strict sidecar validation. Service restart and cache cleanup
return Core `OperationV1`; React observes them through the explicitly
namespaced `/desktop/v1/core/operations/{operation_id}` endpoint and reads any
referenced bounded logs through `/desktop/v1/core/logs/{logs_ref}`. The
sidecar does not synthesize remote progress or replace authoritative Core
state with a local operation.

Local profile responses expose an authentication kind and an opaque native
credential slot status, never a credential reference or secret. Network proxy
URLs must not contain user information; proxy credentials use native slots.
An optional `hugging_face_token` slot supports gated self-deployed models. It is
read from macOS Keychain only for the bounded remote model-preparation action
and is never returned to React or stored in project/Core configuration.
Profile creation defaults an omitted port to `22`, authentication kind to
`ssh_agent`, and proxy configuration to an empty proxy. Execution settings
default omitted capture fields to `capture_mode="transcript"` and
`token_level_metrics_available=false`. Subscription execution carries only
`codex_model`; self-deployed execution carries only the bounded, trimmed
user-provided Hugging Face `hf_model`. The sidecar maps `hf_model` to Core's
stable `agent_model_ref` boundary. `proxy.no_proxy` follows the common ordered
collection rule: React sends a JSON array, the request boundary validates each
bounded string and stores an immutable tuple, and responses serialize it back
to an array.

PATCH request properties are optional but not nullable: omission means the
stored value is unchanged, while an explicit top-level `null` is invalid.
Nullable members inside an included value retain their declared meaning; for
example, `proxy.https_url=null` clears that proxy URL. Response fields with
schema defaults may be omitted on the wire and consumers normalize them to the
declared default. Mutable operation and service responses always carry an
ETag.

Evolution method config is a bounded JSON object whose unknown fields are
preserved losslessly. Desktop does not infer sensitivity or ownership from a
config field name; secret material remains excluded by dedicated closed
credential contracts and Core-owned method schemas.
Project create, patch, response, and validation payloads expose evolution only
as the closed `evolution.targets.<target_id> = {enabled, method, config}`
object. There is no flat target-map compatibility form.
Backend and bootstrap reports are normalized into typed checks, progress, and
user-safe logs. Raw commands, stdout/stderr blobs, PIDs, and remote paths are
not renderer contracts.

Task input contains only the ordinary-user title and objective. A project
source is either a new scratch workspace or a native-folder snapshot. For the
latter, React invokes the Tauri native picker; the host opens the selected
directory, records its device/inode, and sends the path and identity only over
the authenticated private native-to-sidecar route. The sidecar reopens the
identity-bound directory with no-follow semantics, creates the canonical archive
as an unlinked private regular file, and returns only
`WorkspaceImportRefV1 {import_id, content_sha256, byte_size, entry_count,
extracted_byte_size}`.
Neither the picker result nor the Local API contains a host path. Project
creation and workspace sync resolve that opaque import inside the sidecar and
then use the Core workspace-upload protocol.

`POST /desktop/v1/projects/{project_id}/validate` has no renderer-authored
body. `POST /desktop/v1/runs` accepts only `{project_id}`. Both require the
saved local project ETag through `If-Match` and an idempotency key. For every
attempt the sidecar reads the saved project, requires that project's active
SSH tunnel, fetches the current Core project snapshots, verified capabilities,
revision head, and model readiness, calls Core validation, and only then
constructs the Core run-admission request. React never creates or caches an
authoritative snapshot, registry digest, or required revision reference.
The bridge generation is bound to that saved project's ID, profile ID, Local
ETag, and canonical mapped-intent digest. Capability, validation, and run calls
compare the complete saved `ProjectV1` under the same generation lease that
guards their Core transport; drift returns a typed 409 before transport rather
than reusing the prior tunnel. Core-only revision and Core ETag successors do
not imply a Local configuration edit and therefore do not require a new Local
ETag.
Before capability delivery, project validation, or run admission, the bridge
rereads Core `ProjectV1` and compares its canonical project intent and content
snapshots with the session's completed durable mapping. The last validated Core
authority may advance only through an observed direct revision successor with
the corresponding new ETag and strictly newer timestamp; the accepted value
then becomes the next predecessor. Name/spec/task/workspace drift remains a
typed conflict even when Core also reports a successor. Local-to-Core mapping
validation failures are normalized to a closed 422 and never expose Pydantic
exceptions through a bridge method.

Capability responses wrap the complete framework-owned
`EvolutionCapabilitiesV1`; they preserve `supported`, `unsupported`, and
`unavailable`, the evaluated profile, accepted methods, selection resolvers,
identity digests, canonical config JSON, defaults, and all support axes. The
sidecar has no reduced method table. The evaluated profile must equal the
profile selected by the requested release execution mode. The first valid
response pins that complete profile and registry digest as the active-tunnel
client authority. Later capability responses, Core project snapshots,
validation requests/responses, and run requests/responses must match the pin;
a different profile or digest requires a new project-session client. This
binding is order independent: a cached Core project registry digest constrains
the first capability response, and a pinned capability digest constrains every
later project snapshot. `null` and a concrete digest are not equal. Run
admission must also return the request's exact project, task, workspace, and
required-revision references. Project responses expose
typed remote model preparation and active-revision state rather than asking
React to infer them.

The Core client reads bounded JSON into native JSON values, recursively checks
scalar and container types against the response model's generated JSON Schema,
then invokes Pydantic contract validation. This rejects nested coercions such as
`true` for an integer without rejecting a JSON array that encodes a tuple field.
Status and paginated project, run, service, and artifact responses validate in
temporary membership/cache copies and commit only after the complete response
passes; one bad late item leaves the prior cache unchanged.

Client shutdown seals request admission under its state lock, but no response
or transport `close()` runs under that lock or on a request/context-exit thread.
One process-wide fixed-worker, fixed-capacity daemon closer owns all clients.
Each client transport and each request reserve a global close slot before the
resource can exist. The reservation follows a response through normal exit,
late arrival, or shutdown, so queue saturation cannot discard ownership; lack
of capacity rejects the request before transport. An unexpected failed
submission remains bounded client-owned retry work. A close action failure
permanently rejects new leases for that client. `close()` waits only to its hard
deadline, and accepted old resources cannot create replacement-session threads.

Sealing also advances a per-client session generation. A JSON call's generation
admission surrounds its copy-on-write authority/cache transaction. The
transaction's final exit uses the same delivery barrier as `close()` to perform
the deadline/generation check, cache commit or rollback, delivery linearization,
and lease release as one critical section. A winning seal overrides the pending
return with `core_client_closed` and rolls back the transaction. Core SSE is an
explicit iterator whose every `__next__` uses the same atomic transaction exit.
A body or frame rejected by the seal cannot be returned, applied, or recorded as
replay authority for the retired session, and `close()` need not wait for a
stalled application thread.

The sidecar may recover an unknown stale-upload abort only through the strict
client's generation-bound persisted-upload abort transaction. That operation
validates the durable open session, exact ETag, and idempotency key before
transport, then restores upload authority, dispatches abort, validates the
terminal response, and publishes cache/result delivery in the same
copy-on-write generation barrier. A close or project-session switch that seals
the generation rolls back both restored and returned upload authority; bridge
code cannot seed the client's private upload cache directly.

Local SSE carries Desktop state changes and resource invalidations. Every
resource invalidation includes the authoritative ETag or content digest and
an explicit `desktop` or `core` authority, and causes the renderer to reload
the corresponding snapshot. Core project changes are first mapped into the
sidecar-owned composite project and therefore invalidate its Local project
ETag. Timeline, log,
artifact, run, service, and diagnostic payloads are never reconstructed from
partial events by the sidecar.

`DesktopStateV1.core.state` is the renderer's authoritative remote connection
phase: `disconnected`, `connecting`, `host_key_review`, `checking`,
`bootstrapping`, `core_starting`, `online`, `degraded`, `reconnecting`, or
`offline`. Native process startup phases remain Tauri-local and are mapped into
the same renderer state machine; the renderer does not infer remote progress.
Executor admission for project activation publishes a non-readable
`bootstrapping` state with `active_tunnel=false` before the worker start gate is
released. A rejected admission leaves the previous state untouched.

The release owner binds readable state to the exact Local project ID, profile
ID, ETag, and a process-local session generation. SSH profile actions, project
activation, and active-project retirement all admit against that same
generation. Completion must revalidate it before changing Core state or
cleaning a transport, so a late profile result cannot overwrite or disconnect
its replacement and a stale activation cannot publish a Local active project.
Ordinary Core calls and the event relay may invalidate readable state only for
closed local errors proving that the bound client/session no longer exists.
Remote 503 responses, validation or capability failures, and
`core_connection_failed` are not such proof; the last code also covers finite
request deadline expiry. A matching loss atomically publishes `offline` with
`active_tunnel=false`. A callback from an older project or generation cannot
change the replacement session. Successful edit retirement clears the binding
and Core tunnel state atomically; failure keeps the retirement binding together
with its typed diagnostic failure.

The renderer independently requires the selected `ProjectV1` to match the
active project ID, profile ID, and ETag and requires a ready compatible tunnel
before exposing service rows or the inference-service projection. Restart is
resolved only from that gated collection. Therefore selecting B cannot display
or mutate A's services, and the same active project can request reactivation
after its connection becomes unreadable.

### Sidecar Mapping

The adapter between the two v1 contracts is deterministic and fail closed:

| Local intent | Core authority used by the sidecar |
| --- | --- |
| Project name, task, execution, and `evolution.targets` | Core project create/patch; unknown method config is preserved byte-for-byte after canonical validation. |
| `codex_subscription_transcript` | Codex harness, transcript capture, no token metrics, and the user-selected Codex model. |
| `self-deployed` with `hf_model` | Self-deployed harness profile and the same bounded Hugging Face model reference; readiness comes from Core model preparation. |
| Scratch source | Core creates its signed empty workspace snapshot. |
| Native-folder `import_id` | Sidecar resolves the private canonical archive and completes the Core upload session; React never handles archive bytes. |
| Validate current project | Sidecar fetches current Core project refs and verified capabilities, then submits Core project validation. |
| Run current project | Sidecar fetches the active head and any reachable successor, selects the exact Core-required revision, validates, and submits Core run creation. |
| Core SSE change | Sidecar validates the complete Core event, updates its remote snapshot cache, and emits only an ETag/digest-bound Local invalidation. |

Missing tunnel, contract mismatch, stale Local ETag, unavailable registry,
invalid project config, incomplete workspace upload, unprepared model, or a
non-reachable revision produces a typed blocking error. None of these cases may
fall back to an SSH run command, cached capability table, or renderer-generated
reference.

Persisted Desktop mapping, applied-patch, and workspace-finalize revision refs
are monotonic authority. Before mapping reconciliation, same-intent activation,
patch/finalize recovery, or an A-to-B patch can use current mutable authority,
the sidecar requires Core's active revision to be either the exact persisted ref
or its same-project, generation-adjacent successor. A lower generation, a
same-generation ID or manifest rewrite, or an unproven generation skip returns a
typed conflict before recovery workspace mutation, mapping commit, or use of the
reported ETag. An applied imported-draft outcome with no active revision retains
its pre-patch base revision as effective authority. If the base is also empty,
only `null` or a same-project generation-zero first revision is accepted.

Required-revision selection is fixed: if Core reports a reachable queued or
preparing successor for the active head, the new task requires that successor
and remains queued until Core activates it. Otherwise it requires the current
active head. Desktop never skips a pending valid successor to start against
stale context, and it never treats failed or cancelled materialization as an
admissible revision.

## Core Control API v1

Only `GET /version` and `GET /health` are unauthenticated. The sidecar owns a
Core bearer credential and sends it as `Authorization: Bearer`. The renderer
never receives this credential.

The release surface is:

```text
GET    /v1/status
POST   /v1/environment/doctor
POST   /v1/environment/repair
GET    /v1/capabilities

GET    /v1/projects
POST   /v1/projects
GET    /v1/projects/{project_id}
PATCH  /v1/projects/{project_id}
DELETE /v1/projects/{project_id}
GET    /v1/projects/{project_id}/revisions
GET    /v1/projects/{project_id}/revisions/head
GET    /v1/revisions/{revision_id}
POST   /v1/projects/{project_id}/workspace-uploads
GET    /v1/projects/{project_id}/workspace-uploads/{upload_id}
PUT    /v1/projects/{project_id}/workspace-uploads/{upload_id}/chunk
POST   /v1/projects/{project_id}/workspace-uploads/{upload_id}/finalize
POST   /v1/projects/{project_id}/workspace-uploads/{upload_id}/abort
POST   /v1/projects/{project_id}/validate

GET    /v1/runs
POST   /v1/runs
GET    /v1/runs/{run_id}
DELETE /v1/runs/{run_id}
POST   /v1/runs/{run_id}/cancel
POST   /v1/runs/{run_id}/retry
GET    /v1/runs/{run_id}/timeline
GET    /v1/runs/{run_id}/logs
GET    /v1/runs/{run_id}/context
GET    /v1/runs/{run_id}/artifacts
GET    /v1/projects/{project_id}/artifacts/{artifact_id}
GET    /v1/projects/{project_id}/artifacts/{artifact_id}/content
GET    /v1/projects/{project_id}/artifacts/{artifact_id}/diff

GET    /v1/services
GET    /v1/services/{service_id}
POST   /v1/services/{service_id}/restart
GET    /v1/services/{service_id}/logs
GET    /v1/operations/{operation_id}
POST   /v1/operations/{operation_id}/cancel
GET    /v1/logs/{logs_ref}
POST   /v1/diagnostics
GET    /v1/diagnostics/{diagnostic_id}
DELETE /v1/diagnostics/{diagnostic_id}
POST   /v1/maintenance/cache-cleanup
GET    /v1/events
```

The sidecar derives `project_id` only from the active project session and sends
it in every Core artifact read path. Core authorizes metadata, content, and diff
against the live project's signed active revision and its durable typed artifact
authority; bearer possession and an opaque artifact ID are insufficient.
Historical artifacts are available only as a current artifact's verified diff
lineage predecessor. Revision authority is independent of seven-day
idempotency retention, and project deletion cascades it with the revision
ledger.

The sidecar does not issue a current-only artifact-detail request for that
historical predecessor. It requires the current summary lineage to name the
predecessor and consumes the predecessor ID and content digest already bound by
Core's validated `ArtifactDiffV1`; a cached predecessor, when present, must
match but is not required. Content and diff use a dedicated 32 MiB response
limit, covering Core's 2 MiB UTF-8 payload after worst-case six-byte JSON
escaping plus the closed document/hunk/line structure.

`ArtifactDiffV1.document_changes` is the document-level authority; `hunks` may
be empty. Desktop therefore renders rename operations and empty-document
additions/removals from the change object itself instead of treating zero hunks
as no change.

Project specifications carry evolution choices only as
`evolution.targets.<target_id> = {enabled, method, config}` and use Core's
bounded `ProjectEvolutionTargetMap`. The API does not define a second flat or
list-shaped selection format. For a self-deployed project,
`ProjectSpecV1.agent_model_ref` is the bounded Hugging Face model string mapped
losslessly from Desktop's user-owned `hf_model`; it is not an ID in a managed
model table. Project, model-service doctor checks, and inference services report
that reference as `unresolved`, `downloading`, `ready`, or `failed`, including a
typed error and observation time where applicable.
Download progress is either wholly unknown or reports both downloaded and total
bytes. `unresolved` carries no progress, `downloading` carries incomplete known
progress, and known progress on `ready` must satisfy downloaded bytes equal total
bytes. `failed` alone carries the typed error and may preserve paired progress.

Project create, patch, and detail carry a closed `TaskSpecV1` with only title
and objective. There is no task `content_ref`: a caller cannot provide a
Core-owned content ID before Core has accepted content, and v1 has no separate
bounded task-resource upload. Core signs a new immutable task snapshot on
create and whenever the task changes. `current_task_snapshot` is therefore
never null, and run creation must submit that exact Core-owned reference. Task
input does not accept benchmark IDs, host paths, commands, environment, or open
metadata. In `ProjectPatchV1`, `name`, `spec`, `task`, and `workspace` are
optional but non-nullable: omission leaves the stored value unchanged and an
explicit `null` is invalid. `description` remains nullable, so an explicit
`description: null` clears it.

Project responses return the current content-addressed project, task, and
workspace snapshot references plus the active revision reference. Every
reference is a closed object containing its opaque ID and authoritative digest;
callers do not construct IDs by parsing paths. An imported workspace request
contains only its archive SHA-256, byte size, entry count, extracted size, and
frozen format declaration. It never contains a caller-authored Core content ID.
Workspace handoff uses a Core-owned upload session bound to the exact project
snapshot and project ETag observed at creation. Desktop transfers canonical
base64 chunks at explicit bounded offsets. A chunk is accepted only when its
offset equals the session's current `accepted_offset`, and `offset + byte_length`
must not exceed 16 GiB; sparse, overlapping, and out-of-order writes fail.
Finalization requires both the upload `If-Match` and `If-Project-Match`. The body
contains only `content_sha256`; it does not repeat project CAS identity. Provider
conformance requires `If-Project-Match == upload.project_etag == current
project.etag` and requires `upload.project_snapshot` still to equal the current
project snapshot. Core atomically compares both mutable resources, verifies the
archive, and publishes one `WorkspacePublicationV1` binding the exact archive
declaration, first `ContentRefV1`, and workspace snapshot. The finalized upload
and updated `ProjectV1` persist that same publication, the project receives a new
snapshot, and its successful ETag must differ from `upload.project_etag`. A stale
upload cannot overwrite a later project workspace declaration. No workspace
request accepts a host path, URI, command, or setup script.

Every upload representation change is ETag-visible. Initial upload creation
issues an upload ETag distinct from the project ETag used as `If-Match`. An exact
idempotent replay of the complete create response may preserve that upload ETag;
the same upload ID cannot return a different representation as a create replay.
Accepted chunks, abort, and finalize each change the upload representation and
must therefore issue an upload ETag different from the preconditioned session.
For one upload ID, the sidecar binds each observed strong ETag one-to-one to the
canonical DTO representation and rejects both same-ETag/different-state and
same-state/different-ETag responses. Accepted offset, status, and update time are
monotonic, so a fresh ETag cannot make an older upload state authoritative.

`workspace.kind=scratch` is closed during project creation: Core atomically
creates and returns an immutable empty workspace snapshot, so scratch never
depends on an upload. A native-folder, git, or remote snapshot creates a draft
project with a content-addressed archive descriptor and no current workspace;
the project-scoped upload/finalize flow then verifies that exact descriptor and
atomically updates `current_workspace_snapshot`. A project cannot report
`ready` until task/workspace snapshots, active revision, verified registry, and
model readiness are all present.

The only v1 upload format is uncompressed
`openevo_deterministic_tar_v1` (`application/vnd.openevo.workspace-tar`), using
POSIX ustar only. Logical paths are unique NFC UTF-8 POSIX-relative names
without trailing slash. A file header uses the logical path; a directory header
appends `/`. Header paths up to 100 bytes occupy `name`; longer paths split at
the rightmost slash yielding a 1-155 byte `prefix` and 1-100 byte `name`. No
valid split is an error. Every non-root parent directory is emitted once, and
entries sort by encoded header-path bytes.

The 512-byte header offsets are frozen as `name[0:100]`, `mode[100:108]`,
`uid[108:116]`, `gid[116:124]`, `size[124:136]`, `mtime[136:148]`,
`checksum[148:156]`, `typeflag[156:157]`, `linkname[157:257]`,
`magic[257:263]`, `version[263:265]`, `uname[265:297]`, `gname[297:329]`,
`devmajor[329:337]`, `devminor[337:345]`, `prefix[345:500]`, and
`pad[500:512]`. Numeric fields are zero-padded ASCII octal plus NUL; mode is
`0000644\0` or `0000755\0`, zero IDs/devices are `0000000\0`, and size/mtime
use eleven digits plus NUL. Checksum is computed with eight spaces in its field
and encoded as six octal digits, NUL, space. Typeflag is `0` or `5`, magic is
`ustar\0`, version is `00`, and all unused bytes are NUL. Base-256 numbers are
invalid. File bodies are NUL-padded to 512 bytes, followed finally by exactly
two zero blocks and no trailing data.

Absolute names, empty, `.`, or `..` segments, backslashes, NUL, and control
characters are rejected. Symlinks, hardlinks, devices, FIFOs, sparse files,
PAX/GNU extensions, compressed tar, and ZIP are rejected. Limits are 100,000
entries, depth 32, 256 header-path bytes (a directory logical path is therefore
at most 255), `0o77777777777` bytes per file (8,589,934,591, the largest
11-digit octal value), 16 GiB extracted total, 16 GiB archive total, and 8 MiB
per transfer chunk. Core verifies declared counts/sizes and the full archive
digest before extraction and snapshot publication.

Revision resources are read-only. Desktop can page a project's revisions, read
its active head and pending successor transition, and fetch a revision by ID.
There is no public activation, promotion, or partial materialization action;
Core owns readiness and atomic activation. Mutable Core resources use strong
ETags of the exact form `"<lowercase-sha256>"`, and the same type is used for
`If-Match`. Read and action responses expose the ETag required by every
conditional mutation.
A cancelled revision has exactly a `cancelled` transition, and a cancelled
transition cannot be attached to a queued, preparing, active, or failed revision;
the terminal revision and transition states cannot disagree.

`RunCreateV1` references Core-owned immutable project, task, and workspace
snapshot objects, an expected capability registry digest, and a required
revision proven reachable from the active head. Execution and capture modes
come from the authoritative Core project; the create request cannot override
them.
The required revision may still be queued or preparing: Core accepts the run
but keeps it queued until that exact revision is atomically active. A failed or
cancelled revision cannot be required. The request does not accept arbitrary
runtime/model maps, host paths, shell commands, benchmark fields, or a
client-authored admission envelope.

The run state machine is
`queued -> preparing -> running -> succeeded|failed|cancelled`, with the
additional transient `cancelling` state. A queued run includes a closed reason
with code, user-safe summary, and optional retry delay;
`required_revision_uncommitted` means the requested next session cannot start
yet. `admitted_at` records the admission boundary. Before admission both it and
`pinned_revision` are null; Core must not copy the required revision into the pin.
After admission both are non-null, and `preparing`, `running`, `cancelling`, and
every post-admission terminal response retain the exact pin. A cancellation
before admission remains unpinned. A zero-attempt run waiting on a successor may
therefore remain queued with both values null. `required_revision` always preserves the complete
revision, reachable-head ID, and `active|successor` relation. An active required
revision has no successor transition, so `revision_transition` is null; a
successor relation requires a transition whose full predecessor and successor
refs match the required relation. List, detail, and context responses preserve
those identities, current attempt/error, `updated_at`, and strong ETag. A run
has at most 100 attempts; detail embeds all of them in contiguous number order,
and current attempt ID, number, status, error, and run ID must match the run.
Retry creates a new attempt; it never rewrites a terminal attempt.

Evolution is cross-session. A successful task seals its dataset, runs every
enabled target, validates and materializes all outputs, and then atomically
commits one successor revision. Until that revision is active, no follow-up
task may observe any of its outputs. Core reports unavailable transition
features explicitly; Desktop must not infer or simulate activation.

The exhibition artifact union contains `text_memory`, `skill_bundle`, and
`agent_system`. `parametric_memory` remains a reserved typed variant but is not
release-enabled. Artifact summaries identify `project_id` and authoritative
`target_id` independently of artifact type, and include display text, byte size,
producing and membership revisions, lineage, compatibility, scores,
selected/promoted/release state, and type-specific metadata for the three text
products. They never expose raw `file://` URIs or host paths. Content uses one
document-preview shape for every artifact type: at most 128 documents and 2 MiB
of aggregate UTF-8 text, with authoritative totals and truncation state. Diff
uses bounded structured hunks and lines instead of an unbounded unified-text
blob.
Each diff identifies both artifacts and their content digests and contains a
closed `added|removed|modified|renamed` document-change union. Empty documents
may be added or removed with zero hunks. Every returned hunk repeats the
applicable old and/or new document ID, safe relative path, document content
digest, and corresponding artifact/content identity; a hunk cannot drop or
cross-wire its document identity.

Timeline and log records preserve remote sequence, attempt, and service
identity. Service and diagnostic resources report authoritative status, typed
error, update/observation times, and strong ETag. Core exposes only restart for
ordinary service recovery. It deliberately has no service stop action; the
Desktop Local stop route is therefore not forwardable and should be removed in
the later Local-contract convergence rather than implemented through SSH.

Inference services carry `model_preparation` if and only if
`kind=inference`; environment checks carry it if and only if
`kind=model_service`. Diagnostic requests use a closed global, project, or run
target. Global scopes forbid project/run IDs, project scope requires exactly a
project ID, and run scope requires both project and run IDs. Providers still
verify that the run belongs to the project.

Environment repair, service restart, and cache cleanup return HTTP 202 with one
`OperationV1`. The resource is recoverable through
`GET /v1/operations/{operation_id}`, binds a typed original request and successful
result, carries a strong ETag and logs reference, and emits
`operation.updated.v1`. Its descriptor fixes whether the kind is cancellable.
`environment_repair` is cancellable and may move through
`cancelling -> cancelled`; cancel uses `If-Match` plus `Idempotency-Key`.
Service restart and cache cleanup are non-cancellable in v1, and cancel returns
`409 ApiErrorV1` with code `operation_kind_not_cancellable` rather than implying
best-effort cancellation. The generic response also preserves the global
`409 idempotency_key_reused` contract for a conflicting replay. Runs and
diagnostics remain their own recoverable 202 resources.
Any bounded `logs_ref` from an error, check, or operation is readable through
paginated `GET /v1/logs/{logs_ref}`.

Core SSE adds artifact, log, operation, successor-transition, and
revision-activated events. Every non-heartbeat event carries a replay-stable
change ID, exactly one authoritative resource ETag or content digest, and the
applicable parent resource type/ID. Per-event validators bind those values to
the typed payload; an event cannot cross-wire a project, run, service, artifact,
revision, diagnostic, operation, timeline entry, or log entry.
`revision.activated.v1` carries only a revision with `status=active`. A
non-genesis payload with a transition must retain the active transition and the
exact predecessor/successor closure enforced by `RevisionV1`; queued, preparing,
failed, and cancelled revisions are rejected.
The SSE frame ID identifies a concrete stream record and is the replay cursor;
duplicate delivery preserves it. `change_id` identifies one logical mutation and
remains stable if that mutation is retried, replayed, or emitted in a later stream
record with a different frame ID. `Last-Event-ID` remains opaque;
delivery is at least once with a 10,000-event bounded replay window, and an
expired cursor returns HTTP 410 so Desktop reloads snapshots before resuming.
The release relay treats successful Desktop invalidation publication as the
commit point for each non-heartbeat frame. It updates its Core resume cursor
only after publication and only for a contiguous Core event sequence; a
broker/store/publication fault therefore reconnects with the previous
`Last-Event-ID` and accepts replay. Duplicate or out-of-order frame delivery may
produce duplicate invalidation, but cannot regress the cursor or advance it past
an unpublished or missing frame.
Within one active-tunnel client lifetime, including reconnects, the sidecar
binds each SSE frame ID to the digest of canonical validated event bytes. The
same ID and semantic payload may replay with different JSON formatting; the
same ID with different payload fails closed before event authorization state
changes. The client ledger is bounded and fails closed before accepting a new
ID once full. Once an identical replay's canonical digest matches, it is a
no-op: the sidecar yields the replay record but does not reapply resource or
authorization state.

Operation and diagnostic responses are authorization-bearing snapshots. The
sidecar validates immutable identity, parent membership, and all log references
under one lock, then commits the resource member, log references, and snapshot
as one in-memory update. A failed response contributes no member or log access.

## Capability And Mode Rules

Capabilities come only from the active remote Core verified executable
registry. The sidecar has no method table or fallback defaults. Validation and
run creation bind the exact registry digest. Core returns the existing
framework-owned `EvolutionCapabilitiesV1` object directly; the Core Control API
must not copy, rename, narrow, or reinterpret its target, method, resolver,
identity, schema, evaluated-profile, or four-axis support fields. The sidecar
may project that payload into renderer-oriented fields only through a tested,
loss-aware adapter.
The framework's `supported`, `unsupported`, and `unavailable` values are
preserved independently for execution, capture, harness, and runtime support,
including the overall three-state result.

The default Python app in `src/openevo/backend/contracts/v1` remains a
schema-only 501 source. The phase-one provider binds durable project and
workspace publication, verified capabilities and project validation, service
observation, and recoverable SSE to the same operation IDs. Run ownership,
revision activation, service actions, diagnostics, artifacts, and referenced
logs remain explicit typed 503 gaps. See
`docs/architecture/core-control-v1-provider.md` for the exact ownership table.

The v1 release profiles are:

- `codex_subscription_transcript`: remote Codex subscription, mandatory
  transcript capture, no token-level metrics, non-parametric evolution only.
- `self-deployed`: remote Core-managed inference, transcript capture for the
  current three non-parametric targets. A provider must report unavailable
  until the configured model service is genuinely healthy.

## Contract Simulator

A deterministic contract simulator may implement both APIs for renderer,
sidecar, and packaging tests. It must identify itself as
`provider_kind=contract_simulator`, use synthetic IDs and content, and be
excluded from release bundles. Release startup fails closed if a simulator,
fixture-ready state, dry-run transport, legacy route fallback, or development
backend override is active.

Simulator tests prove consumer behavior only. Release evidence requires the
copied macOS app, a real SSH connection, verified Core installation, compatible
Core negotiation, a real Codex transcript run, real artifacts, and reuse by a
later session.

## Change Policy

Additive optional fields are permitted only after updating both OpenAPI
documents and every provider/consumer conformance test. Removing fields,
changing requiredness, changing enum meaning, or changing state transitions
requires a new major contract. Core implementation details may change freely
behind this boundary.

The contract tests must cover canonical OpenAPI digests, strict Python and Zod
validation, malicious upstream responses, bounded payloads, typed errors,
idempotency replay, cursor expiry, SSE replay, feature unavailability, and
release exclusion of simulator/legacy routes.
