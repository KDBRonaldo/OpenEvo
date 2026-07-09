# OpenEvo Core Backend API

Tracked by #121.

`openevo-backend serve` starts the remote Core Backend HTTP API that OpenEvo
Desktop reaches through a sidecar-managed SSH tunnel. Desktop uses this API for
typed backend status, timeline, log, artifact-summary, content, and diff reads.
In this phase, the Desktop sidecar still owns remote lifecycle startup for
gateway, rollout, evolution worker, and model-serving processes through the
service plan; those service commands are not called from React.

## Launcher

```bash
openevo-backend serve --host 127.0.0.1 --port 8765 --state-root /remote/openevo/runs/<project>/<task>
```

The package also keeps `openevo-backend run` as a server-side maintenance and
automation entrypoint for experiment snapshots. Desktop launches it with
`--output-dir <state_root>/runs/<run-id>` and
`--artifact-root <state_root>/evolution/artifacts`, matching the remote
Evolution Backend store. It is not an ordinary-user product surface.

## Route Surface

The backend exposes typed JSON routes:

```text
GET  /health
GET  /status
GET  /environment
POST /environment/doctor
POST /environment/repair

POST /projects
GET  /projects
GET  /projects/{project_id}
PATCH /projects/{project_id}

POST /runs
GET  /runs
GET  /runs/{run_id}
POST /runs/{run_id}/cancel
POST /runs/{run_id}/retry

GET  /runs/{run_id}/timeline
GET  /runs/{run_id}/logs
GET  /runs/{run_id}/artifacts

GET  /artifacts/{artifact_id}
GET  /artifacts/{artifact_id}/content
GET  /artifacts/{artifact_id}/diff

GET  /services
GET  /services/{service_id}/logs
POST /services/{service_id}/restart
POST /services/{service_id}/stop

GET  /capabilities
```

`POST /runs` accepts the same execution mode IDs advertised by
`GET /capabilities`: `codex_subscription_transcript` and `self-deployed`.

## Desktop Local Facade

OpenEvo Desktop does not expose the remote Core Backend directly to React. The
native host starts the local sidecar and React calls sidecar routes under
`/openevo-api`. After Desktop service startup succeeds, the sidecar starts
`openevo-backend serve` on the remote server as the `openevo_backend` managed
service, opens a session-scoped SSH local-forward to remote port `8765`, and
forwards typed backend requests through a `BackendClient` facade. The forwarding
surface is:

```text
GET  /openevo-api/backend/health
GET  /openevo-api/backend/status
POST /openevo-api/backend/environment/doctor
POST /openevo-api/backend/environment/repair

GET  /openevo-api/backend/runs/{run_id}/timeline
GET  /openevo-api/backend/runs/{run_id}/logs
GET  /openevo-api/backend/runs/{run_id}/artifacts

GET  /openevo-api/backend/artifacts/{artifact_id}/content
GET  /openevo-api/backend/artifacts/{artifact_id}/diff

GET  /openevo-api/backend/services/{service_id}/logs
```

All facade routes require the same `X-OpenEvo-Sidecar-Token` that Desktop
receives from `GET /openevo-api/desktop/shell`. The sidecar normally creates
the backend tunnel from the saved SSH profile during
`POST /openevo-api/desktop/services`. For development or smoke tests,
`--backend-base-url` or `OPENEVO_DESKTOP_BACKEND_BASE_URL` can override the
dynamic tunnel; the sidecar strips a trailing slash before constructing the
backend client.

The local facade preserves the remote backend JSON response body. If the remote
backend returns a `BackendError`, Desktop receives the same typed error object
and HTTP status, not an opaque sidecar exception or raw shell output.

The remote backend is started with the same bootstrap `state_root` used by the
sidecar run supervisor. `openevo-backend run` writes canonical run summaries
under `<state_root>/runs/<run-id>/summary.json` and registers evolution
artifacts whose files live under `<state_root>/evolution/artifacts`. The backend
facade reads those Core-owned files when serving timeline, log,
artifact-summary, content, and diff requests. The sidecar does not parse
`summary.json` and does not maintain a second artifact registry.

The facade is intentionally narrow. It does not define a second method registry,
does not execute evolution methods, and does not interpret artifact lineage
outside the typed Core response fields. When no remote backend tunnel is active,
facade routes return a typed `backend_tunnel_not_configured` setup error
instead of inventing ready service state.

## Error Model

All user-visible errors use `BackendError`:

```json
{
  "code": "project_not_found",
  "message": "Project project-1 was not found.",
  "severity": "blocking",
  "category": "project",
  "retryable": false,
  "repair_action": "user_action_required",
  "details": {},
  "logs_ref": null
}
```

`repair_action` is one of:

- `openevo_can_retry`
- `openevo_can_install`
- `openevo_can_reconfigure`
- `user_action_required`
- `unsupported`

Desktop should map this field to the next action shown to users.

## Current Scope

This phase introduces the typed API scaffold plus canonical `state_root` reads
so Desktop can integrate against a stable contract. The Desktop sidecar starts
this backend as a remote managed service and connects to it through SSH. The
backend implementation can expose run summaries and promoted artifact previews
created by `openevo-backend run`, but it does not yet supervise gateway,
rollout, worker, model server, or runtime container processes. `/status`
includes `supervision_mode: "scaffold"` to make that boundary visible. Service
operations are represented by typed placeholders and will be connected to the
existing Core service management paths in later productization phases.

The `/capabilities` route is backed by Core method metadata from
`openevo.capabilities`. The backend API must not define a second evolution
method registry.
