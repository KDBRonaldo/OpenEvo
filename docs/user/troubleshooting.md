# OpenEvo Desktop Troubleshooting

OpenEvo Desktop starts in setup-required state until a real local sidecar and
remote OpenEvo Core Backend are reachable. It should not show a demo project,
fake ready services, or promoted artifacts before configuration is saved.

## First Launch Shows Setup Required

This is expected on a fresh install. Configure:

- project name and task objective;
- remote GPU server host, port, user, and auth reference;
- workspace root;
- proxy, pip index, and Hugging Face mirror settings if the server needs them;
- execution mode and model setting.

After saving configuration, run workspace sync, remote bootstrap, service start,
and then the science run.

## Backend Is Not Reachable

If Desktop reports that the backend facade requires an active remote backend
tunnel, the local sidecar is running but it has no healthy tunnel to
`openevo-backend` on the remote server. Re-run bootstrap or service start from
Desktop. The services action starts the remote `openevo_backend` service on
`127.0.0.1:8765` and then opens the SSH local-forward used by the Desktop
facade. If the error persists, inspect service logs from the Desktop UI.

The setup error is typed:

```json
{
  "code": "backend_tunnel_not_configured",
  "message": "Desktop has no active tunnel to the remote OpenEvo backend.",
  "severity": "blocking",
  "category": "service",
  "retryable": true,
  "repair_action": "openevo_can_reconfigure",
  "details": {},
  "logs_ref": null
}
```

If the sidecar tried to open the tunnel but failed, the error is also typed:

```json
{
  "code": "backend_tunnel_failed",
  "message": "Desktop could not open a tunnel to the remote OpenEvo backend.",
  "severity": "blocking",
  "category": "service",
  "retryable": true,
  "repair_action": "openevo_can_retry",
  "details": {"error_type": "TimeoutError"},
  "logs_ref": "services/openevo_backend"
}
```

The local sidecar preserves typed backend errors. For example:

```json
{
  "code": "backend_unavailable",
  "message": "Remote backend is not reachable.",
  "severity": "blocking",
  "category": "service",
  "retryable": true,
  "repair_action": "openevo_can_retry",
  "details": {},
  "logs_ref": "services/openevo_backend"
}
```

Desktop maps `repair_action` to the next user action:

- `openevo_can_retry`: retry from Desktop.
- `openevo_can_install`: allow OpenEvo to install or repair user-level files.
- `openevo_can_reconfigure`: update project, remote, proxy, or model settings.
- `user_action_required`: fix the remote server or account outside Desktop.
- `unsupported`: the current release cannot repair this case.

## Evolution Timeline Is Empty

The timeline remains planned until a run produces trajectories or transcripts
and Core returns datasets, evolution jobs, promoted artifacts, and evaluation
events. Desktop renders Core-provided timeline and artifact metadata; it does
not create synthetic memory, skill, or instruction updates for display.
