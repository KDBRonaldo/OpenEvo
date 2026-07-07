# OpenEvo Desktop Remote Bootstrap Lifecycle Foundation

Tracked by #45.

This document defines the first remote bootstrap and lifecycle-status contract
for OpenEvo Desktop. It sits above the sidecar planner, remote executor, and SSH
transport layers. The goal is to let a local Desktop app prepare a remote GPU
server for a Science run, return a structured readiness report, and expose data
models that the UI can render while later service supervisors are built.

This layer still does not start a real OpenEvo backend, Polar gateway, rollout
server, evolution worker, vLLM server, or Docker Compose stack.

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

This keeps subscription mode and managed local inference aligned with the
existing Science compiler:

- `codex_subscription_transcript` remains Codex subscription auth with explicit
  transcript capture.
- `codex_managed_local_inference` remains Codex harness plus proxy auth against
  a remote model-serving path.

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
| `docker_pull_runtime` | Pulls the runtime image declared by the compiled experiment. |
| `hf_snapshot_download` | Managed local inference only; installs `huggingface_hub` for the remote user and downloads the HF model snapshot. |

`bootstrap.json` records the state root, workspace root, experiment snapshot
path, runtime image, and managed HF model name when one is present.

## Execution Semantics

`execute_remote_bootstrap_plan()` uses the existing `RemoteExecutorTransport`.
It can therefore run through the CLI dry-run transport, the SSH transport, or
test fakes.

By default it runs the common remote preflight before any bootstrap step. If
preflight fails or raises a transport exception, no bootstrap steps are run and
the report tells the user to fix preflight failures.

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
files. In particular, `docker pull` receives proxy environment variables in the
client process, but a remote Docker daemon may still require administrator-level
proxy or registry mirror configuration outside OpenEvo.

## Lifecycle Models

`src/openevo/remote/lifecycle.py` defines data-only contracts for Desktop:

- `RemoteDaemonLaunchSpec`;
- `RemoteLifecycleStatus`;
- `RemoteServiceStatus`;
- `RemoteLifecycleEvent`;
- `RemoteStatusReport`.

These models are strict, frozen, and JSON round-trip safe. They do not supervise
processes. `RemoteStatusReport.ready` is true only when bootstrap and workspace
are ready, every service is `running` or `planned`, and no actionable errors are
present.

Later slices can connect these models to a real remote supervisor that starts
OpenEvo services, checks ports and health endpoints, and streams lifecycle
events to Desktop.

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
- vLLM startup, health management, or dynamic adapter loading;
- physical LoRA merge or request-level adapter lifecycle changes;
- credential vault or keychain integration;
- Desktop UI rendering;
- remote OpenEvo backend, gateway, rollout server, or evolution worker startup.

## Verification

Focused validation:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/science tests/evolution/test_models.py --collect-only -q >/tmp/openevo-remote-bootstrap-collect.txt
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/remote src/openevo/cli.py tests/openevo/remote tests/openevo/test_cli.py
git diff --check openevo/stable...HEAD
```
