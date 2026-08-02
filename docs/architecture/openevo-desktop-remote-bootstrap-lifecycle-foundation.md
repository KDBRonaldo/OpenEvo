# Release-incompatible foundation note

This document preserves pre-External-Beta foundation context. It is not External Beta release behavior.
Direct run commands, dry-run transports,
developer override env vars, legacy token headers, package-relative Core
artifacts, and command-based service facades are superseded by
`docs/maintainer/productization/spec.md`.

# OpenEvo Desktop Remote Bootstrap Lifecycle Foundation

Tracked by #45. Remote lifecycle controls are tracked by #120.

This document defines the first remote bootstrap and lifecycle-status contract
for OpenEvo Desktop. It sits above the sidecar planner, remote executor, and SSH
transport layers. The goal is to let a local Desktop app prepare a remote GPU
server for a Science run, return a structured readiness report, and expose data
models that the UI can render while later service supervisors are built.

Bootstrap itself prepares run state and dependencies. The historical Desktop
service lifecycle plan now excludes Core: host-global Core startup and attach
are owned only by `openevo.deployment.core_control`. The remaining legacy plan
can still start gateway, rollout, Evolution backend/worker, and optional vLLM
services where older development flows require them. No current
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

This section describes the older per-run science bootstrap scaffold. It does
not own the formal Core Control daemon. Release integration must use the
host-global service contract in `core-control-host-service.md`; after attach it
must not use this scaffold to launch Core, science runs, Gateway, workers, or
model serving over SSH.

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
| `ensure_openevo_cli` | Legacy run-scoped maintainer compatibility only. Its user-site package check is not the release Core host-service installer and must not be used to replace an active daemon. The Core Control product path uploads the exact wheel and sibling `framework-lock.json`, creates a fresh isolated generation under `~/.openevo/core/releases/`, verifies that generation's complete lock-declared Core distribution inventory, and only then enters controlled daemon attach/replacement. |
| `check_codex_cli` | Subscription mode only; verifies `codex --version`. |
| `check_codex_subscription` | Subscription mode only; verifies `~/.codex/auth.json`. |
| `docker_pull_runtime` | For custom images, pulls the image declared by the compiled experiment. A managed release pulls the Core contract's `repository@sha256`, creates the internal alias, and fails unless inspect matches that digest. Only explicit development mode may write the digest-pinned managed Dockerfile under `<state_root>/runtime-images/` as fallback, and its result must match the same trusted digest. |
| `hf_snapshot_download` | Managed local inference only; installs `huggingface_hub` for the remote user and downloads the HF model snapshot. |

`bootstrap.json` records the state root, workspace root, experiment snapshot
path, runtime image alias, managed release mode and trusted image digest, and
the managed HF model name when one is present.

Managed runtime fallback images are built from public Python and Node bases and
install the pinned Codex CLI used by the existing Codex harness examples. The
fallback requires both the Core-owned profile and its exact bound image, not
only a matching image tag. Developer-supplied
`custom_image` profiles remain pull-only, because OpenEvo cannot infer their
Dockerfile or system dependencies. The generated Dockerfile uses HTTPS Debian
sources and requires valid archive signatures. A full remote filesystem,
unreachable proxy, or invalid mirror metadata therefore fails bootstrap with an
actionable report instead of enabling an unauthenticated package path.

Subscription-mode Codex auth is checked on the remote host during preflight and
bootstrap. At gateway runtime initialization, the host `~/.codex/auth.json` is
verified as a private user-owned, link-count-one regular file and copied through
a stable no-follow descriptor into a dedicated private credential bind mount
outside the session tree. Managed Science uses `HOME=/openevo/session/home` and
Core-fixed `CODEX_HOME=/openevo/credentials/codex`; the pinned Codex install
remains on `PATH` at `/home/openevo/.local/bin`. Users should not need to log in
inside the managed runtime container. Custom-image subscription projects fail
exact profile/image admission before credential staging.

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
`extra_env`. The live `openevo-backend run` transport call receives the same
environment so its in-process evolution worker uses the configured mirrors,
proxies, and extra variables rather than only applying them during bootstrap.
Runtime image pull/build commands explicitly unset every uppercase/lowercase
proxy variable that is absent from that profile before invoking Docker. This
prevents stale SSH login-shell `ALL_PROXY`/HTTP(S) proxy values from becoming a
release default while preserving explicit `NO_PROXY` and the exact UI-selected
proxy URL and port. Managed builds pass the same explicit variables as Docker
build args; no proxy value is embedded in the command text. The exact
Core-owned managed Dockerfile builds with `--network=host`, allowing an
explicit server-loopback proxy (`127.0.0.1:<port>`) to remain reachable from
APT and npm build steps. Custom images remain pull-only and do not receive
build or host-network privileges even when their tag collides with a managed
image. Unknown non-null runtime profiles fail bootstrap planning.

The `ensure_openevo_cli` step is intentionally user-scoped. It never falls back
to installing the latest package from PyPI. Desktop first looks for a
packaged exact-version OpenEvo wheel under bundled package-relative wheel
directories and uploads only that selected wheel. Bootstrap installs the
uploaded wheel into the remote user's site packages, prepends `~/.local/bin`
while checking the console script, and leaves host-wide Python or shell
configuration untouched. Later run commands also prepend `~/.local/bin` so the
console script created by `pip --user` can be found without editing shell
profiles. The sidecar computes SHA-256 over the selected local wheel and uploads
an external `framework-lock.json` beside it. Core backend, Evolution backend,
Evolution worker, and run commands all receive that lock path; startup verifies
the installed inventory and method entry points before publishing an executable
registry. If upload is unavailable and the remote package/CLI version is not
already exact, bootstrap fails with an actionable report instead of repairing
from an unpinned network source.
Bootstrap reports sanitize stdout and stderr from remote steps before storing
them, redacting configured proxy/PIP credentials and URL userinfo while
preserving enough command failure text for diagnosis.

The packaged managed-runtime archive is ordinary read-only application media,
so a copied `.app` may expose it as mode `0644` and may be owned by either the
installing user or root. Cold remote installation snapshots that exact release
asset into a private mode-`0400` temporary file before transfer. The snapshot
still requires a link-count-one regular non-executable file with no group or
other write bits, the frozen size and digest, and unchanged descriptor/path
identity before and after the streaming copy; symlinks, hard links, mutable
media, and mid-copy metadata changes fail closed.

This is intentionally process-scoped. Bootstrap does not configure the Docker
daemon, systemd units, registry mirrors, host-wide pip config, or shell profile
files. In particular, `docker pull` and the managed runtime `docker build`
receive proxy environment variables in the client process and proxy build args,
but a remote Docker daemon may still require administrator-level proxy or
registry mirror configuration outside OpenEvo.

## Lifecycle Models

`src/openevo/deployment/lifecycle.py` defines data-only contracts for Desktop:

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
- `identity_path` under `<state_root>/services/pids/<service_id>.identity`;
- `identity_source_path`, which names the uploaded `framework-lock.json`;
- `identity_scheme=framework_lock_and_argv_sha256_v1`;
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
Pid file values must be positive integers before inspect or stop sends any
signal. Non-positive pid values are treated as invalid pid files and reported
without calling `os.kill`.
The daemon already-running check and pid-file health checks apply the same
positive-pid validation before probing process liveness.
Before reusing a live daemon, service startup hashes the exact framework lock
bytes together with the service id, canonical argv, and a digest of the
proxy/mirror/extra environment, then compares that value with the daemon identity
file. Environment secrets are not embedded in the command or identity file. An
exact match is idempotent. A missing or changed identity stops the old managed PID
before starting a replacement; if the old PID cannot be stopped, startup fails
instead of running two service generations.
An unreadable framework lock also fails closed and never reuses the live daemon.
This binds gateway, rollout, vLLM, Evolution backend, and Evolution
worker processes to the release wheel/lock and their startup command without
adding a separate supervisor.

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
If log tailing itself fails due to a transport exception, the logs endpoint
still returns a `RemoteServiceLog` with sanitized diagnostic content instead of
surfacing an unstructured server error.

This legacy per-run service facade remains command-based and its PID files do
not protect its Gateway, rollout, worker, or model-serving processes against PID
reuse. It is not the release Core launcher. The formal host-global Core Control
service separately uses pidfd plus boot-ID/start-time identity, an exclusive
lifecycle lock, pending-start recovery, and authenticated readiness as defined
in `core-control-host-service.md`. That does not upgrade or attest the other
legacy service processes.

## Validation

Dry-run bootstrap:

```bash
PYTHONPATH=src:. python -m pytest tests/openevo/remote/test_bootstrap.py -q
```

Real SSH bootstrap:

```bash
PYTHONPATH=src:. python -m pytest tests/openevo/remote/test_ssh_transport.py -q
```

Skip preflight:

```bash
PYTHONPATH=src:. python -m pytest tests/openevo/remote/test_bootstrap.py::test_execute_bootstrap_plan_skips_preflight_when_requested -q
```

Without `--json`, reports are printed as YAML.

The CLI returns exit code `0` when `RemoteBootstrapReport.ready` is true and
`1` otherwise.

## Unsupported In This Slice

This slice intentionally does not implement:

- Docker, NVIDIA driver, CUDA, or system package installation;
- sudo, systemd, or daemon configuration;
- Docker daemon proxy or registry mirror repair;
- migration of legacy run-scoped user-site package repair and the
  `huggingface_hub` helper into the verified Core host-service generation;
- Docker Compose stack startup;
- production vLLM tuning, restart policy, or dynamic adapter loading;
- physical LoRA merge or request-level adapter lifecycle changes;
- credential vault or keychain integration;
- Desktop UI rendering beyond the structured readiness/status reports.

## Verification

Focused validation:

```bash
source .venv/bin/activate && PYTHONPATH=src pytest tests/openevo/remote/test_services.py tests/openevo/remote/test_lifecycle.py tests/openevo/sidecar/test_api.py -q
source .venv/bin/activate && ruff check src/openevo/deployment/services.py src/openevo/deployment/lifecycle.py src/openevo/deployment/__init__.py src/openevo/sidecar/api.py tests/openevo/remote/test_services.py tests/openevo/remote/test_lifecycle.py tests/openevo/sidecar/test_api.py
git diff --check
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/science tests/evolution/test_models.py --collect-only -q >/tmp/openevo-remote-bootstrap-collect.txt
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/deployment src/openevo/backend/launcher.py tests/openevo/remote tests/openevo/test_cli.py
git diff --check openevo/stable...HEAD
```
