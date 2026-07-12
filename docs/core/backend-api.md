# OpenEvo Core Backend API

> Target contract: the current backend implements only part of this surface.
> Workstream B converges the models, routes, tests, and this document together;
> undocumented or unimplemented routes must not be presented as released.

OpenEvo Core Backend is the remote server process that OpenEvo Desktop controls
through a localhost SSH tunnel. Desktop owns local app state, SSH bootstrap, and
typed request forwarding. Core owns projects, environment doctor and repair,
service supervision, run lifecycle, transcripts, datasets, evolution jobs,
artifacts, diagnostics, and runtime injection.

The External Beta target contract is intentionally black-box: Desktop talks to
Core through HTTP APIs and never bypasses them for run or service operations
after bootstrap.

## Launcher

Release bootstrap starts Core with the installed release artifact:

```bash
openevo-backend serve --host 127.0.0.1 --port 8765 --state-root /home/openevo/.openevo/core-state
```

`OPENEVO_STATE_ROOT` is the remote root for Core-owned state. The backend must
report its version, descriptor SHA256, artifact SHA256, source commit, and state
schema through `/version` and `/status`.

## Loopback Binding

External Beta does not expose Core as a public service. The supported route is:

```text
OpenEvo Desktop -> localhost sidecar -> SSH tunnel -> remote localhost Core
```

Core and sidecar bind only to `127.0.0.1` or `::1`. Binding to `0.0.0.0`, a
LAN interface, or a public interface is a release-mode override rejection unless
an internal maintainer-only test flag is active. `/status` reports sanitized
bind mode and never returns token values.

## Auth

Every Desktop mutation request to the local sidecar uses
`X-OpenEvo-Desktop-Session: <token>`. Every request forwarded from the sidecar
to Core uses `Authorization: Bearer <backend-api-token>`.

The backend API token is created during bootstrap, stored under
`${OPENEVO_STATE_ROOT}/backend/auth/api-token` with `0600` permissions, and
referenced locally through Keychain-backed secret refs. Tokens are redacted from
logs, diagnostics, screenshots, timeline events, and release evidence.

Core returns typed auth errors:

- `desktop_session_token_invalid` for missing, malformed, stale, or
  cross-session Desktop session tokens.
- `backend_api_token_invalid` for missing, malformed, stale, or wrong-profile
  backend bearer tokens.
- `backend_resource_forbidden` for authenticated requests that target a
  forbidden profile, project, run, or artifact.

## Route Surface

Core exposes typed JSON routes. Minimum release endpoint surface:

```text
GET  /version
GET  /health
GET  /status
GET  /projects?state=<state>&after=<cursor>&limit=<n>&sort=<field>
POST /projects
GET  /projects/{project_id}
PATCH /projects/{project_id}
DELETE /projects/{project_id}?dry_run=<bool>&delete_remote_state=<bool>&delete_workspace_snapshots=<bool>&delete_diagnostics=<bool>
POST /projects/{project_id}/workspace/sync
GET  /projects/{project_id}/workspace/{snapshot_id}
POST /environment/doctor
POST /environment/repair
GET  /capabilities
GET  /runs?project_id=<project_id>&state=<state>&after=<cursor>&limit=<n>&sort=<field>
POST /runs
GET  /runs/{run_id}
DELETE /runs/{run_id}?dry_run=<bool>&delete_artifacts=<bool>&delete_logs=<bool>
POST /runs/{run_id}/cancel
POST /runs/{run_id}/retry
GET  /runs/{run_id}/context?attempt_id=<attempt-id>
GET  /runs/{run_id}/timeline?attempt_id=<attempt-id>&after=<cursor>&limit=<n>
GET  /runs/{run_id}/logs?attempt_id=<attempt-id>&source=<source>&tail=<n>
GET  /runs/{run_id}/artifacts?attempt_id=<attempt-id>&type=<type>&state=<promotion_state>
GET  /artifacts/{artifact_id}
GET  /artifacts/{artifact_id}/content?path=<path>&max_bytes=<n>
GET  /artifacts/{artifact_id}/diff?against=<artifact_id>
POST /diagnostics/bundle
GET  /diagnostics/bundles/{bundle_id}
DELETE /diagnostics/bundles/{bundle_id}
GET  /services
GET  /services/{service_id}/logs?tail=<n>
POST /services/{service_id}/restart
POST /services/{service_id}/stop
POST /maintenance/cache/cleanup
```

## Version

`/version` returns Core package version, source commit, descriptor SHA256,
artifact SHA256, state/API contract versions, Python version, and supported API
version. Its Pydantic model and black-box tests become authoritative when the
route is implemented. Desktop must fail setup if the backend version is
incompatible with the bundled Core descriptor.

## Health

`/health` is a cheap liveness/readiness endpoint. It reports whether Core can
read its state root, authenticate requests, and return typed JSON. It does not
claim that model serving, Codex subscription, or remote runtime dependencies
are ready.

## Status

`/status` returns backend state root, bind mode, active project count, active
run count, service summary, state schema version, migration status, token
generation, sanitized capability metadata, and `state_identity`. Its typed
backend model and API tests define the implemented response.

`state_identity` includes `state_root`, `attempt_evolution_root` when an active
attempt exists, `runtime_session_root`, `runtime_evolution_projection`,
`session_completed_event_type`, and projection/symlink safety status. It must
match `RunAttempt`, run context, diagnostics, and release evidence. `/status`
must not include plaintext secrets or raw environment dumps. Migration journal
entries use a typed Core model once migration support is implemented.

## Doctor

`POST /environment/doctor` checks remote OS, Python, workspace permissions,
state root, network/proxy settings, Codex subscription readiness,
Self-Deployed Reference model-serving readiness when configured, runtime paths,
disk space, and GPU availability when needed. Doctor results use typed check
IDs and user actions.

## Repair

`POST /environment/repair` performs user-level repairs that OpenEvo is allowed
to do: reinstall the verified Core artifact, recreate a remote venv, refresh a
backend token, repair directory permissions under the configured workspace,
restart managed services, or re-run network bootstrap with configured proxy
settings. System package changes, Docker daemon changes, global shell profile
edits, and SSH private-key edits are out of scope.

## Runs

`POST /runs` creates a science run from `RunCreateRequest`. Core validates the
task schema, execution mode, capture mode, method IDs, runtime settings,
context artifact allowlist, and idempotency key. Ordinary-user science requests
must reject benchmark-only fields at any nesting level. Clients do not submit
`token_level_metrics_available`; Core derives it from the verified capture path
and rejects client capability claims as unknown request fields.

The `RunCreateRequest`, `RunAttempt`, and response Pydantic models plus API tests
define required fields, enum values, defaults, forbidden fields, success
responses, and validation errors. `RunAttempt` persists `execution_mode`,
`capture_mode`, enabled and disabled artifact families, `method_ids`,
runtime/model config, `context_artifact_ids`,
server-derived `token_level_metrics_available`, and `state_identity`.

`POST /runs/{run_id}/cancel` asks Core to cancel a queued, preparing, or running
attempt. `POST /runs/{run_id}/retry` creates a new attempt from the immutable
project and task snapshot. Core rebuilds timeline and artifact summaries from
Core-owned event and artifact state behind the fixed read APIs, not through a
separate release endpoint. `DELETE /runs/{run_id}` performs a documented
cleanup path and must support dry-run preview through run deletion planning and
the maintenance cleanup API.

## Run Context

`GET /runs/{run_id}/context?attempt_id=<attempt-id>` returns the persisted or
reconstructible context resolution and runtime injection outcome for one
attempt. The response includes `run_id`, `attempt_id`, `context_id`,
`selected_artifact_ids`, `rejected_artifact_ids`, `selection_policy`,
candidate ordering evidence, compatibility decisions,
`runtime_injection_manifest_ref`, `runtime_injection_manifest_sha256`, staged
paths, target write status, environment variables, warnings, pre-task probe
refs, and `state_identity`. The implemented response is governed by its Core
model and API tests.

The context response must survive backend restart without replaying raw logs.
It is redacted before reaching Desktop, diagnostics, or release evidence:
workspace-local secret paths, tokens, proxy credentials, and raw transcript
content are not returned. The endpoint returns typed errors for unknown run or
attempt (`404`), unauthorized profile/project/run access (`403`), active
attempts whose context is not resolved yet (`409 context_not_ready`), missing
runtime injection manifest (`409 injection_manifest_missing`), and stale
artifact references (`409 stale_context_artifact`). Desktop uses this route to
explain why memory, skill bundle, or agent-system artifacts were selected and
whether they actually reached the harness.

## Logs

`GET /runs/{run_id}/logs` and `GET /services/{service_id}/logs` return bounded,
redacted log windows with cursors. Responses include source, timestamp, stream,
line count, truncation state, and redaction summary.

## Artifacts

`GET /runs/{run_id}/artifacts` returns Core-registered artifact summaries:
artifact ID, type, URI ref, manifest, lineage, compatibility, scores, tags,
promotion state, payload hash, and display metadata. Content and diff routes
must enforce artifact compatibility and redaction rules before returning data.

## Diagnostics

`POST /diagnostics/bundle` produces a diagnostics archive with redacted local
facade metadata, Core logs, doctor output, run timeline, artifact summaries,
service state, and schema versions. `GET /diagnostics/bundles/{bundle_id}`
downloads or inspects a generated bundle, and
`DELETE /diagnostics/bundles/{bundle_id}` deletes it. Diagnostics must work in
setup-failed, backend-unreachable-after-bootstrap, run-failed, and
artifact-display-failed states. Raw secrets, bearer tokens, SSH private keys,
provider keys, proxy credentials, and unredacted environment dumps are
forbidden.

## Services

Core owns service start, stop, restart, health, and log APIs for internal
services required by a run. `/services` returns typed service ID, kind, state,
generation, process identity when running,
managed-child relationship, health, restartability, last transition, logs ref,
and user-safe next action. Desktop can start the backend process during
bootstrap and then forwards service requests to Core. Release tests must prove
Desktop does not execute gateway, rollout, worker, model-server, benchmark, or
run commands directly after Core is healthy.

## Cleanup And Deletion

Run and project deletion APIs own user data cleanup. `POST
/maintenance/cache/cleanup` removes approved Core caches, model caches, runtime
caches, and failed bootstrap leftovers after producing a dry-run summary and
cleanup journal. Cleanup must not delete promoted artifacts or datasets unless
the request explicitly includes them and Core records the deletion in state.

## Typed Errors

All user-visible failures use `BackendError`:

```json
{
  "code": "backend_api_token_invalid",
  "http_status": 401,
  "message": "The backend API token is missing, stale, or invalid.",
  "severity": "blocking",
  "category": "auth",
  "retryable": true,
  "repair_action": "openevo_can_reconfigure",
  "next_action": "refresh_backend_token",
  "details": {},
  "logs_ref": "services/openevo_backend"
}
```

Desktop-facing error codes in this API must appear in the release typed-error
catalog and troubleshooting docs. The catalog includes
`state_migration_required`, `state_migration_failed`,
`state_schema_too_new`, `core_artifact_sha_mismatch`,
`context_not_ready`, `injection_manifest_missing`, and
`stale_context_artifact`.

`repair_action` is one of:

- `openevo_can_retry`
- `openevo_can_install`
- `openevo_can_reconfigure`
- `user_action_required`
- `unsupported`

Desktop maps typed errors to user actions without parsing shell output.

## Capabilities

`GET /capabilities` is backed by Core method metadata. It advertises supported
execution modes, capture modes, artifact families, method IDs, required remote
checks, and user-visible labels. Core must not define a second evolution method
registry for Desktop.
