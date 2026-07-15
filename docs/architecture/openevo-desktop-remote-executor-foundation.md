# Release-incompatible foundation note

This document preserves pre-External-Beta foundation context. It is not External Beta release behavior.
Direct run commands, dry-run transports, developer override env vars, legacy
token headers, package-relative Core artifacts, and command-based service
facades are superseded by
`docs/maintainer/productization/spec.md`.

# OpenEvo Desktop Remote Executor Foundation

Tracked by #41.

This document defines the first remote executor contract for OpenEvo Desktop.
It consumes a `SidecarSciencePlan`, runs remote preflight and workspace
preparation through a small transport protocol, and returns structured reports
that Desktop can render.

This layer is not a full remote backend. The first concrete SSH transport is
documented in `docs/architecture/openevo-desktop-ssh-transport-foundation.md`.
The remote bootstrap and lifecycle-status layer above this executor is
documented in
`docs/architecture/openevo-desktop-remote-bootstrap-lifecycle-foundation.md`.
Credential vaults, Docker Compose lifecycle, vLLM lifecycle, remote OpenEvo
service startup, and UI still sit above these contracts. Those components
should consume the executor and bootstrap contracts instead of reimplementing
sidecar plan parsing.

## Transport Boundary

`RemoteExecutorTransport` is intentionally fakeable:

```python
class RemoteExecutorTransport(Protocol):
    def run(command, *, cwd=None, env=None, timeout_seconds=30.0) -> RemoteCommandResult: ...
    def upload_dir(local_path, remote_path) -> None: ...
```

Concrete transports include the dry-run CLI transport and the subprocess-backed
SSH transport. Unit tests still use in-memory fakes for deterministic coverage.
Command failures should be represented as `RemoteCommandResult` values whenever
possible. Transport exceptions are caught by executor boundaries and turned into
structured failure reports.

## Workspace Execution

`execute_workspace_plan()` consumes `SidecarSciencePlan.workspace.actions`:

| Action | Executor behavior |
|---|---|
| `upload_dir` | Calls `transport.upload_dir(source, target)` and records pass/fail. |
| `git_clone` | Calls `transport.run(command, env=dict(plan.proxy_env))` and records stdout, stderr, and return code. |
| `use_remote_path` | Does not call the transport; records a skipped action because the remote path is assumed to exist by contract. |

Workspace reports are strict, frozen Pydantic models. Action collections are
tuple-backed, and computed `ready` values are included in JSON output for Desktop
display.

## Preflight Gating

`execute_sidecar_plan()` runs `run_preflight(transport, plan.preflight)` before
workspace execution unless `run_remote_preflight=False`.

If preflight fails or a transport exception occurs during preflight, workspace
execution is blocked and the report contains an empty workspace action list.
This prevents upload or clone attempts when the remote machine is not ready.

If preflight is skipped or passes, workspace actions execute normally.

`PreflightReport.checks` is tuple-backed so callers cannot mutate checks after
validation and change readiness in place.

## Validation

The executor contract is exercised through Python tests and the Desktop sidecar
API. Dry-run validation uses fake transports and does not open network
connections:

```bash
PYTHONPATH=src:. python -m pytest tests/openevo/remote/test_executor.py -q
```

The dry-run transport can build the same report shape without opening network
connections, so it remains useful for Desktop/backend integration tests and
local inspection.

SSH transport behavior is covered separately:

```bash
PYTHONPATH=src:. python -m pytest tests/openevo/remote/test_ssh_transport.py -q
```

The SSH transport is documented separately and requires local OpenSSH plus
rsync.

Preflight skip behavior is tested through the same executor APIs:

```bash
PYTHONPATH=src:. python -m pytest tests/openevo/remote/test_executor.py::test_execute_workspace_plan_uploads_local_folder -q
```

The bootstrap layer above workspace execution adds:

```bash
PYTHONPATH=src:. python -m pytest tests/openevo/remote/test_bootstrap.py -q
```

That layer prepares the remote run directory, writes `experiment.json` and
`bootstrap.json`, verifies the remote user-site `openevo` package and
`openevo-backend` launcher exactly match the local packaged version, installs
only the uploaded bundled OpenEvo wheel when one is available, checks
subscription-mode Codex readiness, pulls custom runtime images, pulls and
digest-verifies managed OpenEvo Science release images (with an explicit
digest-pinned development build fallback), and prefetches the HF model for managed
local inference.

## Limitations

This foundation layer still does not own:

- the Desktop native credential vault and Keychain broker, which are supplied by
  the release native-host/sidecar composition;
- host-wide remote dependency installation or repair beyond bootstrap's
  user-site `openevo` package and `huggingface_hub` installs;
- Docker daemon or Compose lifecycle management;
- vLLM/model-serving lifecycle management;
- remote OpenEvo backend startup;
- Desktop UI composition.

The bootstrap layer adds run-directory preparation and process-scoped model
download attempts. Full lifecycle managers can be added behind these protocols
without changing the executor transport boundary.

## Verification

Focused validation for this slice:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/deployment src/openevo/backend/launcher.py tests/openevo/remote tests/openevo/test_cli.py
git diff --check
```
