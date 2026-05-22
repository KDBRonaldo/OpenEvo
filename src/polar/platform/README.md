# Polar Dashboard

A read-only observability dashboard for the Polar rollout stack. It bundles a
React SPA and exposes a single FastAPI service that proxies (read-only) to the
rollout and gateway processes.

The launch command (`polar dashboard -c topology.yaml [--port 8090]`) is
documented in the [top-level README](../../../README.md#cli-interface);
defaults: bind `127.0.0.1:8090`, rollout URL and `save_dir` pulled from
topology.

## Frontend build

Production build (required for the wheel to ship a real UI; missing `web/dist/`
falls back to a small JSON placeholder):

```
cd web && npm install && npm run build      # writes web/dist/
```

Dev loop with hot reload — runs at <http://127.0.0.1:5173/> and proxies
`/api/*` to `http://127.0.0.1:8090/`:

```
cd web && npm install && npm run dev
```

## API surface (under `/api`)

The dashboard is read-only. The only state-changing endpoint is the cancel
proxy, so a running session can be aborted from the Session detail page.

| Path | Method | Purpose |
| --- | --- | --- |
| `/api/health` | GET | Service health + upstream reachability |
| `/api/topology` | GET | Static topology + live `/health` per gateway |
| `/api/tasks` | GET | List tasks (filesystem + live rollout overlay) |
| `/api/tasks/{id}` | GET | Single task + session summaries |
| `/api/sessions/{id}` | GET | Session detail |
| `/api/sessions/{id}/trajectory` | GET | Built trajectory traces |
| `/api/sessions/{id}/completions` | GET | Completion records (gateway then disk) |
| `/api/sessions/{id}/evaluation` | GET | Evaluator outcome / strategy |
| `/api/sessions/{id}/raw` | GET | Raw on-disk session payload |
| `/api/sessions/{id}` | DELETE | Cancel a running session |
| `/api/events` | GET (SSE) | Fan-out of rollout + gateway events |

## Polar-side additions (read-only)

The dashboard depends on a small set of read-only endpoints added to the other
Polar services:

- Rollout: `GET /tasks`, `GET /tasks/{id}/sessions`, `GET /events` (SSE).
- Gateway: `GET /sessions`, `GET /sessions/{id}/completions`, `GET /events` (SSE).
- Gateway: completion records persist to
  `<save_dir>/task_<task_id>/sessions/<sid>/completions/<NNNN>-<id>.json`
  via the `CompletionWriter` background task. Controlled by
  `gateway.completion_persistence` in topology.yaml.

## Task submission

Submission stays in the existing channels — `polar submit`, the example
scripts under `examples/<task>/`, or any client that posts to the rollout
server's `POST /rollout/task/submit`. The dashboard surfaces tasks as soon as
they appear in the rollout's memory or in `<save_dir>/`.
