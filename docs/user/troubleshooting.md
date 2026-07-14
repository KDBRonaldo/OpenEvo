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

## Remote Python bootstrap

OpenEvo Desktop automatically looks for remote Python 3.13, 3.12, or 3.11. On a
typical Ubuntu 22.04 GPU server whose system Python is 3.10, Desktop also checks
an existing `uv` in PATH, `~/.local/bin/uv`, or `~/.cargo/bin/uv`. If uv is not
installed, Desktop automatically downloads a pinned, SHA-256-verified official
uv build on x86-64 and AArch64 Linux, then uses it to install Python 3.11.
Configured HTTP proxy, HTTPS proxy, and no-proxy values apply to both downloads.
You do not need to SSH into the server, change PATH, install uv, or replace the
system Python.

The following activation failures have different repairs:

- `core_python_runtime_unavailable`: the server CPU/platform has no supported
  automatic runtime path. Use a supported x86-64 or AArch64 Ubuntu server.
- `core_python_runtime_provision_failed`: the pinned uv download, integrity
  check, or Python download failed. Check the configured server proxy, remote
  network, TLS certificates, and available home-directory space, then use the
  Desktop retry action.
- `core_supervisor_kernel_unsupported`: the Linux kernel does not support the
  pidfd syscalls required for safe Core process supervision. Use a newer
  supported Linux server/kernel.
- `core_supervisor_runtime_unsupported`: Linux boot/process identity could not
  be verified. Check that `/proc` and the kernel boot ID are available.

OpenEvo probes kernel syscalls directly. A uv Python that lacks the convenience
attributes `os.pidfd_open` or `signal.pidfd_send_signal` is still supported and
does not need to be replaced.
