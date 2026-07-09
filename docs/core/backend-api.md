# OpenEvo Core Backend API

Tracked by #121.

`openevo-backend serve` starts the remote Core Backend HTTP API that OpenEvo
Desktop controls through an SSH tunnel. Desktop should call this API instead of
starting gateway, rollout, evolution worker, or model-serving processes directly.

## Launcher

```bash
openevo-backend serve --host 127.0.0.1 --port 8765
```

The package also keeps `openevo-backend run` as a server-side maintenance and
automation entrypoint for experiment snapshots. It is not an ordinary-user
product surface.

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

This phase introduces the typed API scaffold and in-memory project/run/artifact
state so Desktop can integrate against a stable contract. It does not yet
supervise real gateway, rollout, worker, model server, or runtime container
processes. `/status` includes `supervision_mode: "scaffold"` to make that
boundary visible. Service operations are represented by typed placeholders and
will be connected to the existing Core service management paths in later
productization phases.

The `/capabilities` route is backed by Core method metadata from
`openevo.capabilities`. The backend API must not define a second evolution
method registry.
