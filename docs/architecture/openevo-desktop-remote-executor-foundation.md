# OpenEvo Desktop Remote Executor Foundation

Tracked by #41.

This document defines the first remote executor contract for OpenEvo Desktop.
It consumes a `SidecarSciencePlan`, runs remote preflight and workspace
preparation through a small transport protocol, and returns structured reports
that Desktop can render.

This layer is not a full remote backend. The first concrete SSH transport is
documented in `docs/architecture/openevo-desktop-ssh-transport-foundation.md`,
but credential vaults, Docker Compose lifecycle, vLLM lifecycle, remote OpenEvo
service startup, and UI still sit above this contract. Those components should
consume this executor contract instead of reimplementing sidecar plan parsing.

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

## CLI

The foundation CLI is:

```bash
openevo sidecar execute science.yaml --remote-profile remote.yaml --json
```

By default, the CLI uses a local dry-run transport. It can build the same report
shape without opening network connections, so it remains useful for
Desktop/backend integration tests and local inspection.

To opt into real SSH execution:

```bash
openevo sidecar execute science.yaml --remote-profile remote.yaml --transport ssh --json
```

The SSH transport is documented separately and requires local OpenSSH plus
rsync.

To skip preflight and inspect workspace execution:

```bash
openevo sidecar execute science.yaml --remote-profile remote.yaml --skip-preflight --json
```

Without `--json`, the report is printed as YAML.

## Limitations

This foundation still does not include:

- local credential vault or keychain integration;
- remote dependency installation or repair;
- Docker daemon or Compose lifecycle management;
- vLLM/model-serving lifecycle management;
- remote OpenEvo backend startup;
- Desktop UI.

The next release slices can add lifecycle managers behind the protocol
introduced here.

## Verification

Focused validation for this slice:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/remote src/openevo/cli.py tests/openevo/remote tests/openevo/test_cli.py
git diff --check
```
