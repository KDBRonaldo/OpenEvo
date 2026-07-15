# OpenEvo Core Backend API

> Target contract: the current backend implements only part of this surface.
> [Desktop/Core Contract v1](../architecture/desktop-core-contract-v1.md)
> defines the release boundary and versioning rules. Workstream B converges the
> models, routes, tests, and this document together; undocumented or
> unimplemented routes must not be presented as released.

OpenEvo Core Backend is the remote server process that OpenEvo Desktop controls
through a localhost SSH tunnel. Desktop owns local app state, SSH bootstrap, and
typed request forwarding. Core owns projects, environment doctor and repair,
service supervision, run lifecycle, transcripts, datasets, evolution jobs,
artifacts, diagnostics, and runtime injection.

The External Beta target contract is intentionally black-box: Desktop talks to
Core through HTTP APIs and never bypasses them for run or service operations
after bootstrap.

## Launcher

Release bootstrap uses the internal `openevo-core-service` maintenance entry
point after staging the exact release artifact. It supplies the canonical
user-global root, external framework lock, and source identity; the supervisor,
not a project/run command, chooses and pre-binds the loopback listener:

```bash
openevo-core-service ensure \
  --service-root /home/openevo/.openevo/core \
  --framework-lock /home/openevo/.openevo/releases/framework-lock.json \
  --source-commit 0123456789abcdef0123456789abcdef01234567
```

This is launcher/maintenance automation, not an ordinary-user CLI surface.
`openevo-backend serve` is supervisor-only and requires inherited socket and
readiness descriptors; it cannot choose a public bind address or per-run state
root.

Maintenance `stop` remains an explicit unconditional operation. Callers that
own only one returned attachment must instead use the internal
`stop_core_service_if_generation` API with that attachment's generation and
release identity. It acquires the bootstrap and lifecycle locks, validates the
current ledger, and stops or removes state only on an exact match; a missing or
replacement generation returns without mutation.

The external framework lock names the exact installed Core wheel and pins its
version and SHA-256. Startup fails before serving capabilities when the lock,
wheel, installed inventory, or entry points do not match.

Release startup also proves that the provider owns exactly the frozen operation
IDs before listening. Route discovery traverses both eagerly expanded
`APIRoute` entries and FastAPI's deferred included-router representation; this
keeps clean installs on supported FastAPI versions from silently leaving nested
`/v1` routes unbound. Unknown route container types remain excluded and the
exact-set check fails startup rather than weakening provider ownership.

The canonical host service root is `~/.openevo/core`; project and task state is
owned inside that one Core instance. The backend must
report its version, descriptor SHA256, artifact SHA256, source commit, and state
schema through `/version` and `/v1/status`.

## Loopback Binding

External Beta does not expose Core as a public service. The supported route is:

```text
OpenEvo Desktop -> localhost sidecar -> SSH tunnel -> remote localhost Core
```

The formal Core host service binds only to IPv4 `127.0.0.1` through a
supervisor-owned socket. The kernel selects an available dynamic port, which is
returned only in private sidecar tunnel attachment metadata. It has no release override for `0.0.0.0`, a LAN
interface, or a public interface. Service bootstrap metadata never returns a
public Core URL.

## Auth

Every Desktop mutation request to the local sidecar uses
`X-OpenEvo-Desktop-Session: <token>`. Every request forwarded from the sidecar
to Core uses `Authorization: Bearer <backend-api-token>`.

The Core bearer is created during bootstrap and stored at
`~/.openevo/core/bearer-token` with `0600` permissions. It is transferred only
in the private bootstrap attachment and retained in sidecar process memory;
it is not a renderer response or durable Desktop resource. Tokens are redacted from
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
GET  /v1/status
GET  /v1/projects
POST /v1/projects
GET  /v1/projects/{project_id}
PATCH /v1/projects/{project_id}
DELETE /v1/projects/{project_id}
POST /v1/projects/{project_id}/workspace-sync
POST /v1/projects/{project_id}/validate
POST /v1/environment/doctor
POST /v1/environment/repair
GET  /v1/capabilities?execution_mode=<codex_subscription_transcript|self-deployed>
GET  /v1/runs
POST /v1/runs
GET  /v1/runs/{run_id}
DELETE /v1/runs/{run_id}
POST /v1/runs/{run_id}/cancel
POST /v1/runs/{run_id}/retry
GET  /v1/runs/{run_id}/context
GET  /v1/runs/{run_id}/timeline
GET  /v1/runs/{run_id}/logs
GET  /v1/runs/{run_id}/artifacts
GET  /v1/projects/{project_id}/artifacts/{artifact_id}
GET  /v1/projects/{project_id}/artifacts/{artifact_id}/content
GET  /v1/projects/{project_id}/artifacts/{artifact_id}/diff
POST /v1/diagnostics
GET  /v1/diagnostics/{diagnostic_id}
DELETE /v1/diagnostics/{diagnostic_id}
GET  /v1/services
GET  /v1/services/{service_id}/logs
POST /v1/services/{service_id}/restart
POST /v1/services/{service_id}/stop
POST /v1/maintenance/cache-cleanup
```

Artifact inspection is explicitly project-scoped. Core first verifies the live
project and its signed active revision, then requires the artifact to appear in
that revision's durable typed artifact authority. A predecessor artifact is not
directly readable after it leaves the current head; diff may resolve it only
when the current artifact's authoritative lineage names it. The authority is
revision-owned and survives idempotency replay-record retention.
Authority-table migration signs a row only when the old durable activation
binding and idempotency closure still prove it, or when a legacy ledger has one
unambiguous retained response closure. Missing or ambiguous inputs stop startup
with an explicit restore-or-rebuild maintenance action. Migration accounts row
count, per-value length, and aggregate bytes before exact-length guarded row
reads; rejected data is never decoded into Python first.

Content inspection uses a separate small no-follow scanner budget before
hashing, verifies digest, size, complete inventory, and UTF-8 for every returned
document, and never returns artifact URI, scanner handle, or host path. Diff
applies line and comparison budgets before its bounded matcher. Unavailable
managed authority uses the declared retryable HTTP 503 error contract. The
2 MiB returned UTF-8 budget can expand sixfold under legal JSON control-character
escaping, so Desktop reserves a separate 32 MiB artifact-response envelope for
content and diff rather than applying its ordinary 4 MiB JSON response limit.

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

`/v1/status` returns backend state root, bind mode, active project count, active
run count, service summary, state schema version, migration status, token
generation, sanitized capability metadata, and `state_identity`. Its typed
backend model and API tests define the implemented response.

`state_identity` includes `state_root`, `attempt_evolution_root` when an active
attempt exists, `runtime_session_root`, `runtime_evolution_projection`,
`session_completed_event_type`, and projection/symlink safety status. It must
match `RunAttempt`, run context, diagnostics, and release evidence. `/v1/status`
must not include plaintext secrets or raw environment dumps. Migration journal
entries use a typed Core model once migration support is implemented.

## Doctor

`POST /v1/environment/doctor` checks remote OS, Python, workspace permissions,
state root, network/proxy settings, Codex subscription readiness,
Self-Deployed Reference model-serving readiness when configured, runtime paths,
disk space, and GPU availability when needed. Doctor results use typed check
IDs and user actions.

## Repair

`POST /v1/environment/repair` performs user-level repairs that OpenEvo is allowed
to do: reinstall the verified Core artifact, recreate a remote venv, refresh a
backend token, repair directory permissions under the configured workspace,
restart managed services, or re-run network bootstrap with configured proxy
settings. System package changes, Docker daemon changes, global shell profile
edits, and SSH private-key edits are out of scope.

## Runs

`POST /v1/runs` creates a science run from `RunCreateV1`. The request contains
only Core-owned immutable project, task, and workspace snapshot IDs, the
expected verified-registry digest, the required revision ID, and the release
execution/capture modes. Runtime maps, model maps, host paths, commands,
benchmark fields, context allowlists, and client-authored admission envelopes
are rejected. Project method selections are read from the immutable project
snapshot, whose sole evolution shape is
`evolution.targets.<target_id> = {enabled, method, config}`.

The required revision may be active, queued, or preparing. For queued or
preparing revisions Core persists the run with
`required_revision_uncommitted`; it starts only after that exact revision is
atomically active. Failed or cancelled revisions are rejected.

`POST /v1/runs/{run_id}/cancel` asks Core to cancel a queued, preparing, or running
attempt. `POST /v1/runs/{run_id}/retry` creates a new attempt from the immutable
project and task snapshot. Core rebuilds timeline and artifact summaries from
Core-owned event and artifact state behind the fixed read APIs, not through a
separate release endpoint. `DELETE /v1/runs/{run_id}` performs a documented
cleanup path and must support dry-run preview through run deletion planning and
the maintenance cleanup API.

## Run Context

`GET /v1/runs/{run_id}/context` returns the pinned revision, successor
transition, and selected typed artifact references used by the run. It exposes
no host path, runtime environment, artifact URI, secret, or scanner handle.

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

`GET /v1/runs/{run_id}/logs` and `GET /v1/services/{service_id}/logs` return bounded,
redacted log windows with cursors. Responses include source, timestamp, stream,
line count, truncation state, and redaction summary.

## Artifacts

`GET /v1/runs/{run_id}/artifacts` returns typed Core-registered artifact
summaries with lineage, compatibility, scores, selection state, revision
membership, payload digest, and display metadata. It never returns `file://`
URIs or host paths. Content and diff routes return bounded verified text views
and enforce compatibility and redaction before returning data.

## Diagnostics

`POST /v1/diagnostics` produces a diagnostics archive with redacted local
facade metadata, Core logs, doctor output, run timeline, artifact summaries,
service state, and schema versions. `GET /v1/diagnostics/{diagnostic_id}`
returns its status and bounded download metadata. Diagnostics must work in
setup-failed, backend-unreachable-after-bootstrap, run-failed, and
artifact-display-failed states. Raw secrets, bearer tokens, SSH private keys,
provider keys, proxy credentials, and unredacted environment dumps are
forbidden.

## Services

Core owns service start, stop, restart, health, and log APIs for internal
services required by a run. `/v1/services` returns typed service ID, kind, state,
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

`GET /v1/capabilities?execution_mode=<release-mode>` projects
`EvolutionCapabilitiesV1` from the startup-verified executable registry used by
planning and dispatch. The response is target-rooted and includes Core version,
registry digest, evaluated generic profile, descriptor identities, configured
and effective defaults, schemas, ordered inputs, and four-axis support reasons.

The required query accepts `codex_subscription_transcript` or `self-deployed`;
Core performs the sole mapping to framework execution/capture/harness axes.
Missing or invalid values return the normal typed validation error. A Core
process without a verified registry returns `evolution_registry_unavailable`
with HTTP 503 and never falls back to method metadata. Both 422 and 503 typed
errors are declared in the endpoint's OpenAPI contract.
