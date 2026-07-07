# OpenEvo Desktop Science Foundation

Tracked by #37.

This document defines the foundation contract for OpenEvo Desktop Science
projects. A Science Project config is the user-facing input for ordinary
science users. It validates a task, environment, execution mode, and evolution
targets, then compiles to the existing OpenEvo `ExperimentConfig` contract.

Users do not configure runtime images, Polar gateway wiring, Docker lifecycle,
or model serving directly in the common path. The science layer chooses a
runtime profile and execution mode, and the lower-level OpenEvo/Polar experiment
compiler keeps responsibility for the concrete experiment payload.

## Boundary

The science layer is a config and compilation layer. It does not:

- run Codex;
- start Docker;
- open SSH connections;
- call model APIs;
- manage gateway, vLLM, or Docker Compose lifecycles.

It validates Science Project input and compiles it to the existing OpenEvo/Polar
experiment contract. OpenEvo still wraps the Codex harness; this layer only makes
that contract easier for Desktop science workflows to produce.

## Task Sources

Science Project task sources describe where the task workspace comes from:

- `remote_path`: use an already available path on the remote machine.
- `scratch`: run without an uploaded workspace.
- `local_folder`: upload or mirror a local folder before compilation.
- `git_repository`: clone or materialize a repository before compilation.

`local_folder` and `git_repository` require a Desktop or remote backend to
prepare the workspace first. After preparation, the compiler receives a prepared
remote workspace path for the task, for example through:

```bash
openevo science compile CONFIG --prepared-workspace task=/remote/path
```

The science compiler does not upload local files or clone repositories itself.

## Environment Profiles

Science environment profiles compile to runtime images:

| Profile | Runtime image |
|---|---|
| `managed_science` | `openevo/science-runtime:0.1.0` |
| `python_research` | `openevo/python-research-runtime:0.1.0` |
| `custom_image` | developer-supplied override |

`custom_image` is an escape hatch for developers and advanced experiments. It is
not the default user path.

## Execution Modes

`codex_subscription_transcript` uses Codex subscription authentication and sets
`agent.settings.capture_mode="transcript"`. This produces transcript trajectory
data for text evolution and explicitly has no token-level metrics.

`codex_managed_local_inference` uses proxy authentication and requires
`execution.hf_model`. The compiler sets the agent model to that Hugging Face
model and injects `OPENEVO_MANAGED_HF_MODEL` into the runtime environment. The
remote backend is responsible for starting and wiring vLLM, the gateway, and the
proxy path.

Science Projects do not support `evolution.parametric_memory` in this foundation
slice, including managed local inference projects. Parametric memory requires a
separate adapter source or trainer contract that is not part of the Science
Project schema yet.

## Runtime Prepare

Task `setup_commands` compile to `RuntimeSpec.prepare` exec actions running in
the experiment workspace. The science compiler first emits a prepare action that
creates `/polar/session/workspace`, then emits user setup commands. Workspace
upload is handled by the lower-level experiment compiler before these prepare
actions run for workspace-backed tasks, so dependency installation and other
setup steps can read workspace files.

## Evolution Targets

Science Projects support text memory, skill bundle, and agent system evolution
targets in this foundation slice. Parametric memory is intentionally rejected for
all Science Projects until adapter source and trainer configuration are defined.

## Preflight

`openevo.remote.preflight` defines fakeable remote preflight contracts. It does
not implement SSH transport. Callers provide a `RemoteProbe` that can run remote
commands and return structured results.

Preflight checks SSH first with `true`. If SSH fails, it returns immediately and
does not run later checks. After SSH succeeds, Docker is checked with
`docker info`, disk capacity is checked with `df -Pk "$HOME"`, and other remote
capabilities are reported through the same fakeable probe contract. Codex CLI
and subscription checks run only when subscription execution is required.

## Desktop Web Shell

The first web shell lives at `/openevo` in the existing Vite app. It uses the
local Python sidecar API when available and keeps a fixture fallback so the
layout remains usable when the sidecar is not running.

The shell intentionally keeps Terminal Bench and low-level runtime image fields
out of the default flow. It displays the remote profile, proxy settings, Science
Project summary, bootstrap paths, lifecycle readiness, and evolution timeline
using the same concepts as the Python contracts.

### Local Sidecar API

OpenEvo Desktop starts a local sidecar API with
`openevo sidecar serve --host 127.0.0.1 --port 3766`.

For a user project, Desktop can start the same server with local config paths:

```bash
openevo sidecar serve \
  --config science.yaml \
  --remote-profile remote.yaml \
  --host 127.0.0.1 \
  --port 3766
```

In this mode the sidecar reads the local Science Project YAML and remote profile
YAML, validates them, builds the existing sidecar science plan, and derives a
Desktop shell status response from those contracts. The status endpoint is a
local read-only operation; it does not run SSH, remote preflight, workspace
upload, git clone, bootstrap, model download, or remote service startup.

The first endpoint is `GET /openevo-api/desktop/shell`. It returns typed shell
status for the `/openevo` route and keeps the same subscription transcript
semantics as the Python sidecar contracts: token-level metrics remain false in
subscription mode, bootstrap readiness is represented separately from
informational readiness notes, and no direct model API call is made.

`POST /openevo-api/desktop/bootstrap` is the first mutating sidecar endpoint.
It is available only for config-backed sidecar sessions. It reuses
`build_sidecar_science_plan()`, `build_remote_bootstrap_plan()`, and
`execute_remote_bootstrap_plan()` to run the existing bootstrap executor, then
returns both the bootstrap report and refreshed shell status. The default serve
transport is dry-run; `openevo sidecar serve --transport ssh` selects the SSH
transport for the bootstrap endpoint. Bootstrap does not upload local folders or
clone git task sources; workspace preparation remains a separate lifecycle step
so the UI can report source materialization independently from runtime
readiness.

The sidecar generates a per-process mutation token and includes it in
`GET /openevo-api/desktop/shell` under `sidecar.mutation_token`. Mutating
requests must send that token in the non-simple
`X-OpenEvo-Sidecar-Token` header. Missing or invalid tokens are rejected before
any bootstrap work starts. This is a local CSRF guard for the Desktop sidecar:
cross-site pages can submit simple localhost requests, but cannot read the
same-origin shell response or set the required custom header. The sidecar also
serializes bootstrap runs per config-backed session; a second bootstrap request
returns 409 while one is already running.

Dry-run serve mode is intended for local UI development and smoke tests. It
exercises the same planning and report mapping path, but it does not mutate the
remote server. A dry-run report can therefore show the UI path as ready without
proving that remote files, Docker images, or Hugging Face models were actually
prepared. Real remote preparation requires `--transport ssh`.

This slice does not let the HTTP API start vLLM, Polar gateway, rollout,
evolution worker, or long-running OpenEvo backend processes. Those operations
remain behind the remote lifecycle contracts until a supervisor is added.

## CLI

The initial user-visible CLI for this slice is:

```bash
openevo science compile CONFIG [--json] [--prepared-workspace task=/remote/path]
```

`--prepared-workspace` can be repeated when multiple tasks need prepared remote
workspace paths.

## Limitations

This foundation slice does not include:

- a fully wired Desktop UI;
- vault or SSH tunnel management;
- a remote backend implementation;
- Docker Compose lifecycle management;
- vLLM lifecycle management;
- parametric memory or adapter training for Science Projects.

Those capabilities remain separate layers above or below the Science Project
contract.
