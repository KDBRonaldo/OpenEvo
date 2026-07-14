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
POST   /desktop/v1/services/{service_id}/stop
GET    /desktop/v1/services/{service_id}/logs
POST   /desktop/v1/diagnostics
GET    /desktop/v1/diagnostics/{diagnostic_id}
DELETE /desktop/v1/diagnostics/{diagnostic_id}
POST   /desktop/v1/maintenance/cache-cleanup
GET    /desktop/v1/events
```

Bootstrap, workspace sync, connection, repair, diagnostics, and other long
actions return HTTP 202 with `LocalOperationV1`; the UI observes them through
operation snapshots and events. HTTP requests do not remain open for the life
of those operations.

Local profile responses expose an authentication kind and an opaque native
credential slot status, never a credential reference or secret. Network proxy
URLs must not contain user information; proxy credentials use native slots.
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

`DesktopStateV1.core.state` is the renderer's authoritative remote connection
phase: `disconnected`, `connecting`, `host_key_review`, `checking`,
`bootstrapping`, `core_starting`, `online`, `degraded`, `reconnecting`, or
`offline`. Native process startup phases remain Tauri-local and are mapped into
the same renderer state machine; the renderer does not infer remote progress.

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
POST   /v1/projects/{project_id}/workspace-sync
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
list-shaped selection format.

`RunCreateV1` references Core-owned immutable project, task, and workspace
snapshots, an expected capability registry digest, and a required revision.
The required revision may still be queued or preparing: Core accepts the run
but keeps it queued until that exact revision is atomically active. A failed or
cancelled revision cannot be required. The request does not accept arbitrary
runtime/model maps, host paths, shell commands, benchmark fields, or a
client-authored admission envelope.

The run state machine is
`queued -> preparing -> running -> succeeded|failed|cancelled`, with the
additional transient `cancelling` state. A queued run includes a typed reason;
`required_revision_uncommitted` means the requested next session cannot start
yet. Retry creates a new attempt; it never rewrites a terminal attempt.

Evolution is cross-session. A successful task seals its dataset, runs every
enabled target, validates and materializes all outputs, and then atomically
commits one successor revision. Until that revision is active, no follow-up
task may observe any of its outputs. Core reports unavailable transition
features explicitly; Desktop must not infer or simulate activation.

The exhibition artifact union contains `text_memory`, `skill_bundle`, and
`agent_system`. `parametric_memory` remains a reserved typed variant but is not
release-enabled. Artifact responses expose content, diff, compatibility,
lineage, scores, selected/promoted state, and revision membership without raw
`file://` URIs or host paths. Desktop content responses are bounded previews:
at most 128 documents and 2 MiB of aggregate UTF-8 text, with explicit total
document count and truncation state.

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
