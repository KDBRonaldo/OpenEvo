# OpenEvo Desktop Current Troubleshooting Notes

> Pre-release status: error codes and actions will be completed alongside the
> real Core/Desktop workflow. Do not treat this page as a released support
> catalog.

Desktop starts in setup-required state until a real local sidecar and remote
Core Backend are reachable. It must not show a demo project, fake ready
services, or promoted artifacts before configuration is saved.

Current typed errors use this shape:

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

Desktop maps `repair_action` to retry, OpenEvo-managed install/repair,
reconfiguration, a required external user action, or unsupported behavior. The
full release guide must be generated or checked against the final typed-error
catalog and packaged UI.
