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

### Local Desktop Serve

The installable Python distribution is named `openevo`. It includes the
OpenEvo Desktop assets and the lower-level `polar` / `polar_evolution` packages
that still provide rollout, gateway, trajectory, and evolution backend runtime
modules. The `openevo`, `polar`, and `polar-evolution` console scripts remain
declared from the same distribution so existing backend workflows keep working
while the user-facing package identity is OpenEvo.

The release-shaped local launcher is:

```bash
openevo desktop open
```

This starts the same local FastAPI/uvicorn process, opens `/openevo` in the
user browser, and falls back to an available local port if the preferred port is
occupied. `--no-browser` keeps the server-only behavior for headless or scripted
environments.

The exact-port server entrypoint remains:

```bash
openevo desktop serve --host 127.0.0.1 --port 3766
```

Both commands serve the packaged Desktop SPA at `/openevo` and the sidecar API
at `/openevo-api/*`. `desktop serve` preserves exact-port behavior for tests,
integrations, and power users.
The root path `/` redirects to `/openevo`. The packaged asset set is built in
OpenEvo-only mode, so users do not see the shared Polar dashboard navigation.
The local server still returns the SPA for compatibility routes `/tasks`,
`/tasks/*`, `/sessions`, `/sessions/*`, and `/compare`; unknown
`/openevo-api/*` paths remain API 404s.

Packaged Desktop assets are refreshed with:

```bash
cd web && npm run build:openevo
rsync -a --delete web/dist/ src/openevo/desktop/web/
```

The release smoke check rebuilds the OpenEvo-only Desktop assets, verifies that
the committed package assets match `web/dist`, builds the Python wheel, and
inspects the wheel metadata, console scripts, and packaged assets:

```bash
cd web
npm ci
npm run build:openevo
cd ..
diff -qr web/dist src/openevo/desktop/web
python -m build --wheel
python scripts/ci/check_openevo_release.py --wheel dist/*.whl
```

For development or custom packages, `--static-root` can point at a Vite build
output directory:

```bash
openevo desktop serve --static-root web/dist
```

If static assets are missing, incomplete, or referenced by `index.html` but not
present on disk, the command fails before starting the API server and tells the
caller to build and package Desktop assets or pass `--static-root`.

`openevo sidecar serve --host 127.0.0.1 --port 3766` remains available as an
API-only entrypoint for integration tests and power users.

For a user project, Desktop can start the same server with local config paths:

```bash
openevo desktop serve \
  --config science.yaml \
  --remote-profile remote.yaml \
  --host 127.0.0.1 \
  --port 3766
```

Desktop-created projects can start from a no-config sidecar. In that mode the
sidecar receives a writable local config root from `--desktop-config-root`, or
uses `OPENEVO_DESKTOP_CONFIG_DIR`, falling back to
`~/.openevo/desktop`.

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

`POST /openevo-api/desktop/project-config` is the local setup endpoint for
ordinary Desktop users. It is mutation-token protected and available when the
sidecar was created with a writable config root and transport factory. The
request payload is a typed Desktop draft with project name, task id, objective,
task source, SSH host/user/port, proxy/mirror settings, Codex model, and text
evolution toggles. The sidecar validates that draft by constructing the existing
`ScienceProjectConfig` and `RemoteProfileConfig` models, then writes:

```text
<desktop-config-root>/projects/<project-slug>/science.yaml
<desktop-config-root>/profiles/<remote-profile-id>.yaml
```

The response returns those two paths and a refreshed shell status. Saving a
valid draft replaces the current in-process sidecar session with a config-backed
session, so subsequent `/workspace`, `/bootstrap`, and `/run` calls use the
saved configs without requiring the user to restart the sidecar. Invalid drafts
return 422 and do not write files.

The draft contract remains secret-reference-only. It accepts SSH agent,
private-key path, password reference, and passphrase reference fields, but it
forbids raw secret extras such as `password`. A future vault/keychain layer can
resolve those references outside this local config contract.

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

`POST /openevo-api/desktop/workspace` executes that separate workspace
preparation lifecycle. It is available only for config-backed sidecar sessions
and reuses `build_sidecar_science_plan()` plus `execute_sidecar_plan()` so
`local_folder` uploads, `git_repository` clones, `remote_path` no-ops, and
`scratch` workspaces stay on the existing remote executor contract. The endpoint
returns both the full executor report and a top-level workspace summary, plus a
refreshed shell status. Workspace readiness updates only the SSH and Workspace
service rows; it does not imply bootstrap readiness, model availability, gateway
startup, rollout startup, or evolution worker startup.

`POST /openevo-api/desktop/run` launches the configured Science task after
workspace and bootstrap readiness. It is available only for config-backed
sidecar sessions, requires the same mutation token, and rejects requests unless
the Workspace service is `ready`, `status.bootstrap.ready` is true, and the
latest bootstrap report contains both `prepared_paths.experiment_snapshot` and
`prepared_paths.state_root`. The command is derived only from those bootstrap
paths:

```bash
openevo run <experiment_snapshot> --output-dir <state_root>/runs/<run-id> --json
```

The sidecar supervises that command as a background job in the local sidecar
process. `POST /openevo-api/desktop/run` records a `run_<timestamp>` id, stores
a running status, starts a daemon thread that executes the command through the
configured remote transport with `cwd=<state_root>`, and returns immediately.
The returned run status includes the run id, state, readiness, command, return
code, stdout/stderr snapshots, output directory, experiment snapshot, start
timestamp, and finish timestamp.

Desktop polls `GET /openevo-api/desktop/run` with the same sidecar mutation
token to recover the latest run state and refreshed shell status. While the run
is active, the OpenEvo backend service and transcript evolution row are marked
`running`. A passing terminal status marks the OpenEvo backend service ready and
the transcript evolution row complete. A failing terminal status still returns
HTTP 200 from the polling endpoint with `run.ready=false`, and the refreshed
shell status marks those rows blocked with the command error.

The sidecar generates a per-process mutation token and includes it in
`GET /openevo-api/desktop/shell` under `sidecar.mutation_token`. Mutating
requests must send that token in the non-simple
`X-OpenEvo-Sidecar-Token` header. Missing or invalid tokens are rejected before
any workspace, bootstrap, or run work starts. This is a local CSRF guard for the
Desktop sidecar: cross-site pages can submit simple localhost requests, but
cannot read the same-origin shell response or set the required custom header.
The sidecar serializes workspace syncs, bootstrap runs, and run launches
independently per config-backed session; a second request for the same lifecycle
action returns 409 while one is already running. Status updates from lifecycle
actions are written under a shared status lock so concurrent workspace and
bootstrap runs do not clobber each other's service rows.

Dry-run serve mode is intended for local UI development and smoke tests. It
exercises the same planning, status, and polling path, but it does not mutate
the remote server. A dry-run report can therefore show the UI path as ready
without proving that task workspaces, Docker images, or Hugging Face models were
actually prepared. Real remote preparation and run execution require
`--transport ssh`.

This slice adds only a local sidecar run supervisor with one latest run per
config-backed session. It does not survive sidecar process restarts, stream
incremental remote log files, cancel remote process groups, expose resume,
restart the sidecar process, start vLLM, start Polar gateway, run Docker
Compose, manage dynamic adapters, or supervise rollout/evolution worker
services independently. Those operations remain behind the remote lifecycle
contracts until dedicated service supervisors are added.

## CLI

The initial user-visible CLI for this slice is:

```bash
openevo science compile CONFIG [--json] [--prepared-workspace task=/remote/path]
```

`--prepared-workspace` can be repeated when multiple tasks need prepared remote
workspace paths.

## Limitations

This foundation slice does not include:

- vault or SSH tunnel management;
- a remote backend implementation;
- Electron packaging or sidecar process supervision;
- Docker Compose lifecycle management;
- vLLM lifecycle management;
- parametric memory or adapter training for Science Projects.

Those capabilities remain separate layers above or below the Science Project
contract.
