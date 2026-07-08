# OpenEvo Desktop Remote Bootstrap Lifecycle Foundation

Tracked by #45. Remote lifecycle controls are tracked by #120.

This document defines the first remote bootstrap and lifecycle-status contract
for OpenEvo Desktop. It sits above the sidecar planner, remote executor, and SSH
transport layers. The goal is to let a local Desktop app prepare a remote GPU
server for a Science run, return a structured readiness report, and expose data
models that the UI can render while later service supervisors are built.

Bootstrap itself prepares run state and dependencies. The Desktop service
lifecycle endpoint starts the OpenEvo backend, gateway, rollout server,
evolution worker, and optional vLLM server after bootstrap readiness. No current
Desktop path starts a Docker Compose stack.

## Input Boundary

Bootstrap consumes an existing `SidecarSciencePlan`. It does not parse Science
YAML directly and does not reimplement Science compilation rules.

The flow is:

```text
ScienceProjectConfig + RemoteProfileConfig
  -> build_sidecar_science_plan()
  -> build_remote_bootstrap_plan()
  -> execute_remote_bootstrap_plan()
  -> RemoteBootstrapReport
```

This keeps subscription mode and self-deployed inference aligned with the
existing Science compiler:

- `codex_subscription_transcript` remains Codex subscription auth with explicit
  transcript capture.
- `self-deployed` remains Codex harness plus proxy auth against a remote
  model-serving path.

The legacy value `codex_managed_local_inference` is accepted only as an input
alias at config boundaries and normalizes to `self-deployed`.

No bootstrap path directly calls model APIs.

## Bootstrap Plan

`RemoteBootstrapPlan` is a strict, frozen Pydantic model. It records:

- `remote_profile_id`, `project_name`, and `task_id`;
- `proxy_env` derived from the remote profile;
- `preflight` settings copied from `SidecarSciencePlan.preflight`;
- `workspace_root`, where source workspaces are prepared by the sidecar
  executor;
- `state_root`, a per-run directory under the remote OpenEvo area;
- `experiment_snapshot`, the compiled OpenEvo experiment config;
- ordered `RemoteBootstrapStep` entries.

For default profiles with workspace root `/home/<user>/.openevo/workspaces`,
the state root is:

```text
/home/<user>/.openevo/runs/<project-slug>/<task-slug>
```

If the workspace root ends in `/workspaces`, bootstrap uses the sibling
`/runs` directory. Otherwise it writes under
`<workspace_root>/.openevo-runs/<project-slug>/<task-slug>`.

## Bootstrap Steps

The current builder emits idempotent remote steps:

| Step | Purpose |
|---|---|
| `ensure_workspace_root` | Creates the remote workspace root. |
| `ensure_state_root` | Creates the per-run state directory. |
| `write_experiment_snapshot` | Writes `<state_root>/experiment.json`. |
| `write_bootstrap_manifest` | Writes `<state_root>/bootstrap.json`. |
| `ensure_openevo_cli` | Ensures the remote user can run `openevo`; installs or upgrades the `openevo` Python package with `python3 -m pip install --user --upgrade openevo` when the command is absent or `openevo --help` fails. |
| `check_codex_cli` | Subscription mode only; verifies `codex --version`. |
| `check_codex_subscription` | Subscription mode only; verifies `~/.codex/auth.json`. |
| `docker_pull_runtime` | For custom images, pulls the image declared by the compiled experiment. For managed OpenEvo Science images, writes a managed runtime Dockerfile under `<state_root>/runtime-images/` and runs `docker pull <image> || docker build ... -t <image> ...`. |
| `hf_snapshot_download` | Managed local inference only; installs `huggingface_hub` for the remote user and downloads the HF model snapshot. |

`bootstrap.json` records the state root, workspace root, experiment snapshot
path, runtime image, and managed HF model name when one is present.

Managed runtime fallback images are built from public Python and Node bases and
install the pinned Codex CLI used by the existing Codex harness examples. The
fallback is only for OpenEvo-managed image names. Developer-supplied
`custom_image` profiles remain pull-only, because OpenEvo cannot infer their
Dockerfile or system dependencies.

Subscription-mode Codex auth is checked on the remote host during preflight and
bootstrap. At gateway runtime initialization, the host `~/.codex/auth.json` is
copied into the per-session bind mount under the runtime `CODEX_HOME`, defaulting
to `/polar/session/.codex`. Users should not need to log in inside the managed
runtime container.

## Execution Semantics

`execute_remote_bootstrap_plan()` uses the existing `RemoteExecutorTransport`.
It can therefore run through the CLI dry-run transport, the SSH transport, or
test fakes.

By default it runs the common remote preflight before any bootstrap step. If
preflight fails or raises a transport exception, no bootstrap steps are run and
the report tells the user to fix preflight failures.

Docker Compose is a non-blocking preflight probe in the current Desktop Science
path. Missing Compose is reported as a warning because services are started by
direct commands, not a Compose stack.

When bootstrap steps run:

- a successful command becomes `pass`;
- a required failed command becomes `fail` and stops later steps;
- an optional failed command becomes `warn`;
- command stdout, stderr, return code, remediation kind, and command text are
  retained in `RemoteBootstrapStepExecution`.

The report includes `prepared_paths` for:

- `workspace_root`;
- `state_root`;
- `experiment_snapshot`;
- `bootstrap_manifest`.

## Proxy Behavior

Networked bootstrap steps receive `proxy_env` from `RemoteProfileConfig.proxy`.
This includes standard `HTTP_PROXY`, `HTTPS_PROXY`, lowercase variants,
`NO_PROXY`, `PIP_INDEX_URL`, `HF_ENDPOINT`, `HF_HOME`, and user-provided
`extra_env`.

The `ensure_openevo_cli` step is intentionally user-scoped. It adds
`~/.local/bin` to the process PATH while checking for `openevo`, installs into
the remote user's site packages when the command is missing or its help check
fails, and leaves host-wide Python or shell configuration untouched. Later run
commands also prepend `~/.local/bin` so the console script created by
`pip --user` can be found without editing shell profiles.
Bootstrap reports sanitize stdout and stderr from remote steps before storing
them, redacting configured proxy/PIP credentials and URL userinfo while
preserving enough command failure text for diagnosis.

This is intentionally process-scoped. Bootstrap does not configure the Docker
daemon, systemd units, registry mirrors, host-wide pip config, or shell profile
files. In particular, `docker pull` and the managed runtime `docker build`
receive proxy environment variables in the client process and proxy build args,
but a remote Docker daemon may still require administrator-level proxy or
registry mirror configuration outside OpenEvo.

## Lifecycle Models

`src/openevo/remote/lifecycle.py` defines data-only contracts for Desktop:

- `RemoteDaemonLaunchSpec`;
- `RemoteLifecycleStatus`;
- `RemoteServiceState`;
- `RemoteServiceStatus`;
- `RemoteManagedServiceStatus`;
- `RemoteServicesStatus`;
- `RemoteServiceLog`;
- `RemoteServiceOperationResult`;
- `RemoteLifecycleEvent`;
- `RemoteStatusReport`.

These models are strict, frozen, and JSON round-trip safe. They do not supervise
processes. `RemoteStatusReport.ready` is true only when bootstrap and workspace
are ready, every service is `running`, `ready`, or `planned`, and no actionable
errors are present.

`RemoteServicesStatus` is the Desktop/Core contract for the managed remote
service facade. It contains one `RemoteManagedServiceStatus` per daemon service
and computes `ready` when every required daemon is `ready` or `running`.
`RemoteServiceState` values are `planned`, `starting`, `running`, `ready`,
`degraded`, `stopped`, `failed`, and `unknown`.

## Remote Service Lifecycle Facade

The existing aggregate start endpoint remains:

```text
POST /openevo-api/desktop/services
```

It still builds a `RemoteServicesPlan` from the current sidecar session and
executes its ordered steps. The facade added for Desktop control does not create
a parallel supervisor topology. It uses the same `RemoteServicesPlan` as the
source of managed daemons and ignores the `write_topology` step because that
step writes config, not a long-running process.

Daemon `RemoteServiceStep.manifest` entries include:

- `service_id`;
- `pid_path` under `<state_root>/services/pids/<service_id>.pid`;
- `log_path` under `<state_root>/services/logs/<service_id>.log`;
- `port` where the service owns a local HTTP port;
- `model` for managed vLLM.

The sidecar exposes these token-gated endpoints:

| Endpoint | Response | Purpose |
|---|---|---|
| `GET /openevo-api/desktop/services/status` | `RemoteServicesStatus` | Inspect pid files, process liveness, and health commands for all managed daemons. |
| `GET /openevo-api/desktop/services/health` | `RemoteServicesStatus` | Same structured health report as status for Desktop polling. |
| `GET /openevo-api/desktop/services/logs?service_id=gateway&lines=50` | `RemoteServiceLog` | Tail one managed daemon log. |
| `POST /openevo-api/desktop/services/stop` | `RemoteServiceOperationResult` | Stop one managed daemon by pid file. |
| `POST /openevo-api/desktop/services/restart` | `RemoteServiceOperationResult` | Stop, start with the existing step command, then run that step's health command. |

All five require `X-OpenEvo-Sidecar-Token`, a config-backed sidecar session, and
ready workspace/bootstrap state. Stop and restart also acquire the existing
services lifecycle lock so they do not collide with service start or each other.
Unknown service ids return a clear client error; `write_topology` is not a valid
service id for logs, stop, or restart.

Stop sends SIGTERM and waits for the process to disappear before deleting the
pid file. If the process is still alive after the grace period, stop returns a
failed `RemoteServiceOperationResult`, leaves the pid file intact, and restart
does not start a replacement process.

Inspection semantics are intentionally pragmatic:

- missing pid file or dead pid is `stopped`;
- live pid plus successful health command is `ready`;
- live pid with no health command is `running`;
- live pid plus failed health command is `degraded`;
- transport exceptions or malformed inspect results become structured
  `failed` or `unknown` service statuses instead of uncaught crashes.

Log and operation outputs are sanitized with the remote proxy redaction rules.
They also redact `Authorization:` and `Proxy-Authorization:` header values and
bearer tokens before returning content to Desktop.

This facade is still command-based. It does not add systemd units, persistent
restart policies, cross-session process ownership tracking, or a new daemon
supervisor.

## CLI

Dry-run bootstrap:

```bash
openevo sidecar bootstrap science.yaml --remote-profile remote.yaml --json
```

Real SSH bootstrap:

```bash
openevo sidecar bootstrap science.yaml --remote-profile remote.yaml --transport ssh --json
```

Skip preflight:

```bash
openevo sidecar bootstrap science.yaml --remote-profile remote.yaml --skip-preflight --json
```

Without `--json`, reports are printed as YAML.

The CLI returns exit code `0` when `RemoteBootstrapReport.ready` is true and
`1` otherwise.

## Unsupported In This Slice

This slice intentionally does not implement:

- Docker, NVIDIA driver, CUDA, or system package installation;
- sudo, systemd, or daemon configuration;
- Docker daemon proxy or registry mirror repair;
- full Python dependency repair beyond the user-site `openevo` and
  `huggingface_hub` installs attempted by bootstrap;
- Docker Compose stack startup;
- production vLLM tuning, restart policy, or dynamic adapter loading;
- physical LoRA merge or request-level adapter lifecycle changes;
- credential vault or keychain integration;
- Desktop UI rendering beyond the structured readiness/status reports.

## Verification

Focused validation:

```bash
source .venv/bin/activate && PYTHONPATH=src pytest tests/openevo/remote/test_services.py tests/openevo/remote/test_lifecycle.py tests/openevo/sidecar/test_api.py -q
source .venv/bin/activate && ruff check src/openevo/remote/services.py src/openevo/remote/lifecycle.py src/openevo/remote/__init__.py src/openevo/sidecar/api.py tests/openevo/remote/test_services.py tests/openevo/remote/test_lifecycle.py tests/openevo/sidecar/test_api.py
git diff --check
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/science tests/evolution/test_models.py --collect-only -q >/tmp/openevo-remote-bootstrap-collect.txt
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/remote src/openevo/cli.py tests/openevo/remote tests/openevo/test_cli.py
git diff --check openevo/stable...HEAD
```
