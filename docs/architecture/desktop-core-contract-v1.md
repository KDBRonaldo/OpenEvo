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

## Common Protocol

Every JSON model is closed: unknown fields are errors. IDs are opaque UTF-8
strings and must never be parsed for host paths or implementation identity.
Timestamps are UTC RFC 3339 strings. Digests are lowercase SHA-256 hex.

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
principal, route, resource scope, key, canonical request digest, response, and
status. Replaying the same request returns the same result; reusing the key for
a different request returns `409 idempotency_key_reused`. Mutable resources use
ETag and `If-Match`.

List routes use `limit` (maximum 100), `after`, `sort`, and `direction`, and
return `{items, next_cursor, has_more}`. A cursor is bound to the filters and
sort order. Invalid cursors return 400; expired cursors return 410.

SSE event frames include an SSE `id`, a versioned event name, and a closed
`EventEnvelopeV1`. Providers support `Last-Event-ID`, at-least-once delivery,
15-second heartbeats, and bounded replay. An expired event cursor returns 410;
the renderer reloads snapshots before subscribing again.

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
POST   /desktop/v1/diagnostics
GET    /desktop/v1/diagnostics/{diagnostic_id}
DELETE /desktop/v1/diagnostics/{diagnostic_id}
POST   /desktop/v1/maintenance/cache-cleanup
GET    /desktop/v1/events
```

Only sidecar-owned connection, host-key, bootstrap, repair, activation, and
workspace-sync actions return `LocalOperationV1`. Core-owned runs, service
actions, diagnostics, and cleanup resources retain their Core v1 response
shape after strict sidecar validation. The sidecar does not synthesize remote
progress or replace authoritative Core state with a local operation.

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
stable `agent_model_ref` boundary.

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
latter, React invokes the Tauri native picker; the host creates the canonical
archive in private storage, hands it to the sidecar, and returns only
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

Capability responses wrap the complete framework-owned
`EvolutionCapabilitiesV1`; they preserve `supported`, `unsupported`, and
`unavailable`, the evaluated profile, accepted methods, selection resolvers,
identity digests, canonical config JSON, defaults, and all support axes. The
sidecar has no reduced method table. Project responses expose typed remote
model preparation and active-revision state rather than asking React to infer
them.

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
GET    /v1/artifacts/{artifact_id}
GET    /v1/artifacts/{artifact_id}/content
GET    /v1/artifacts/{artifact_id}/diff

GET    /v1/services
GET    /v1/services/{service_id}
POST   /v1/services/{service_id}/restart
GET    /v1/services/{service_id}/logs
POST   /v1/diagnostics
GET    /v1/diagnostics/{diagnostic_id}
DELETE /v1/diagnostics/{diagnostic_id}
POST   /v1/maintenance/cache-cleanup
GET    /v1/events
```

Project specifications carry evolution choices only as
`evolution.targets.<target_id> = {enabled, method, config}` and use Core's
bounded `ProjectEvolutionTargetMap`. The API does not define a second flat or
list-shaped selection format. For a self-deployed project,
`ProjectSpecV1.agent_model_ref` is the bounded Hugging Face model string mapped
losslessly from Desktop's user-owned `hf_model`; it is not an ID in a managed
model table. Project, model-service doctor checks, and inference services report
that reference as `unresolved`, `downloading`, `ready`, or `failed`, including a
typed error and observation time where applicable.

Project create, patch, and detail carry a closed `TaskSpecV1` with title,
objective, and an optional content-addressed provenance reference. The task
object is self-contained; Core signs a new immutable task snapshot on create
and whenever the task changes. `current_task_snapshot` is therefore never null,
and run creation must submit that exact Core-owned reference. Task input does
not accept benchmark IDs, host paths, commands, environment, or open metadata.

Project responses return the current content-addressed project, task, and
workspace snapshot references plus the active revision reference. Every
reference is a closed object containing its opaque ID and authoritative digest;
callers do not construct IDs by parsing paths. Workspace handoff uses a
Core-owned upload session. Desktop creates a session with the total byte size
and SHA-256, transfers canonical base64 chunks at explicit bounded offsets,
then finalizes or aborts with `If-Match`. Finalization returns the authoritative
workspace snapshot reference. No workspace request accepts a host path, URI,
command, or setup script.

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
POSIX ustar headers, zero-padded bodies, exactly two 512-byte zero terminator
blocks, and no trailing bytes. Entries are unique and sorted by UTF-8 path
bytes. Paths are NFC UTF-8 POSIX-relative names; absolute names, empty, `.`, or
`..` segments, backslashes, NUL, and control characters are rejected. Only
regular files and directories are allowed. Files use normalized `0644` or
`0755`, directories use `0755`, uid/gid/mtime are zero, and uname/gname are
empty. Symlinks, hardlinks, devices, FIFOs, sparse files, PAX/GNU extensions,
compressed tar, and ZIP are rejected. Limits are 100,000 entries, depth 32,
1,024 path bytes, 8 GiB per file, 16 GiB extracted total, 16 GiB archive total,
and 8 MiB per transfer chunk. Core verifies declared counts/sizes and the full
archive digest before extraction and snapshot publication.

Revision resources are read-only. Desktop can page a project's revisions, read
its active head and pending successor transition, and fetch a revision by ID.
There is no public activation, promotion, or partial materialization action;
Core owns readiness and atomic activation. Mutable Core resources use strong
ETags of the exact form `"<lowercase-sha256>"`, and the same type is used for
`If-Match`. Read and action responses expose the ETag required by every
conditional mutation.

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
yet. A run that has not passed admission has a null `pinned_revision`; Core must
not copy the required revision into that field. List, detail, and context
responses include the immutable refs, required and nullable pinned revisions,
current attempt and error, complete successor transition, `updated_at`, and
strong ETag. Retry creates a new attempt; it never rewrites a terminal attempt.

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

Timeline and log records preserve remote sequence, attempt, and service
identity. Service and diagnostic resources report authoritative status, typed
error, update/observation times, and strong ETag. Core exposes only restart for
ordinary service recovery. It deliberately has no service stop action; the
Desktop Local stop route is therefore not forwardable and should be removed in
the later Local-contract convergence rather than implemented through SSH.

Core SSE adds artifact, log, successor-transition, and revision-activated
events. Every non-heartbeat event carries a replay-stable change ID and an
authoritative resource ETag or content digest. `Last-Event-ID` remains opaque;
delivery is at least once with a 10,000-event bounded replay window, and an
expired cursor returns HTTP 410 so Desktop reloads snapshots before resuming.

## Capability And Mode Rules

Capabilities come only from the active remote Core verified executable
registry. The sidecar has no method table or fallback defaults. Validation and
run creation bind the exact registry digest. Core returns the existing
framework-owned `EvolutionCapabilitiesV1` object directly; the Core Control API
must not copy, rename, narrow, or reinterpret its target, method, resolver,
identity, schema, evaluated-profile, or four-axis support fields. The sidecar
may project that payload into renderer-oriented fields only through a tested,
loss-aware adapter.

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
