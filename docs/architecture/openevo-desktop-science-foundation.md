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
- manage Docker Compose lifecycles.

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

For managed profiles, users do not upload or choose a runtime image in Desktop.
Remote bootstrap first attempts to pull the managed image. If that image is not
available to the remote Docker daemon, bootstrap writes an OpenEvo-managed
Dockerfile under the run state directory and builds the same image tag on the
remote server. The generated image contains Python, Node, common build tools,
and the pinned Codex CLI required by the Codex harness. Custom images remain
pull-only because OpenEvo cannot infer their system dependencies.

## Execution Modes

`codex_subscription_transcript` uses Codex subscription authentication and sets
`agent.settings.capture_mode="transcript"`. This produces transcript trajectory
data for text evolution and explicitly has no token-level metrics.
Remote preflight checks that the remote user has `codex` and
`~/.codex/auth.json`. During each gateway runtime session, that host auth file
is staged into the container-visible `CODEX_HOME` under `/polar/session`, so the
user does not need to log in again inside the managed runtime image.

`self-deployed` uses proxy authentication and requires `execution.hf_model`.
The legacy config value `codex_managed_local_inference` remains accepted as an
input alias at Desktop and Science model boundaries, but configs normalize and
emit `self-deployed`. The compiler sets the agent model to that Hugging Face
model and injects `OPENEVO_MANAGED_HF_MODEL` into the runtime environment. The
remote Desktop service lifecycle starts vLLM, the gateway, and the proxy path
before a run can launch.

Science Projects do not support `evolution.parametric_memory` in this foundation
slice, including self-deployed projects. Parametric memory requires a separate
adapter source or trainer contract that is not part of the Science Project
schema yet.

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
environments. The user-facing launcher defaults sidecar mutating endpoints to
SSH transport so the Desktop lifecycle buttons operate the configured remote
server. Pass `--transport dry-run` for local demos that should not open SSH
connections.

The exact-port server entrypoint remains:

```bash
openevo desktop serve --host 127.0.0.1 --port 3766
```

Both commands serve the packaged Desktop SPA at `/openevo` and the sidecar API
at `/openevo-api/*`. `desktop serve` preserves exact-port behavior for tests,
integrations, and power users. `desktop serve` and API-only `sidecar serve`
keep `dry-run` as their default transport; pass `--transport ssh` when those
entrypoints should mutate a remote server.
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

The release smoke check runs on Node 22, audits the Desktop frontend dependency
graph for high or critical advisories, rebuilds the OpenEvo-only Desktop assets,
verifies that the committed package assets match `web/dist`, builds the Python
wheel, inspects the wheel metadata, console scripts, packaged asset references,
and forbidden shared-dashboard payloads, then installs the wheel into a clean
environment and runs the installed OpenEvo CLI and Desktop app smoke checks:

```bash
cd web
npm ci
npm audit --audit-level=high
npm test -- --run
npm run build:openevo
cd ..
diff -qr web/dist src/openevo/desktop/web
rm -rf .openevo-remote-wheel src/openevo/wheels
python -m build --wheel --outdir .openevo-remote-wheel
mkdir -p src/openevo/wheels
cp .openevo-remote-wheel/openevo-*.whl src/openevo/wheels/
rm -rf dist
python -m build --wheel
python scripts/ci/check_openevo_release.py --wheel dist/*.whl
python -m venv .openevo-wheel-smoke
.openevo-wheel-smoke/bin/python -m pip install --upgrade pip
.openevo-wheel-smoke/bin/python -m pip install dist/*.whl
.openevo-wheel-smoke/bin/openevo --help
.openevo-wheel-smoke/bin/openevo desktop --help
.openevo-wheel-smoke/bin/openevo desktop open --help
.openevo-wheel-smoke/bin/python scripts/ci/smoke_openevo_desktop_wheel.py
```

OpenEvo package and Desktop-sidecar Python regressions are checked with:

```bash
ruff check src/openevo tests/openevo
PYTHONPATH=src:. python -m pytest tests/ci/test_openevo_python_workflow.py tests/openevo -q
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
The remote profile block includes the non-secret fields needed to reconstruct
the Desktop setup draft after startup or saved-config activation: profile id,
host, port, user, auth method plus key/reference ids, effective workspace root,
HTTP/HTTPS proxy, `NO_PROXY`, `PIP_INDEX_URL`, Hugging Face endpoint, and
`HF_HOME`. It does not include raw passwords or private-key material.
The response also includes `sidecar.transport` capability metadata for the
selected local mutating transport. Desktop uses it to block lifecycle actions
before remote execution when the active auth settings require unsupported
secret-reference resolution, such as `password_ref` or `passphrase_ref` with the
current SSH transport. The sidecar API enforces the same capability check on
workspace sync, bootstrap, and run launch, returning `409` before invoking the
remote transport.

`POST /openevo-api/desktop/project-config` is the local setup endpoint for
ordinary Desktop users. It is mutation-token protected and available when the
sidecar was created with a writable config root and transport factory. The
request payload is a typed Desktop draft with project name, task id, objective,
task source, SSH host/user/port/auth references, workspace root, proxy/mirror
settings, execution mode, mode-specific model field, and text evolution
toggles. Subscription transcript drafts carry `codex_model`; self-deployed
drafts carry Hugging Face `hf_model` and omit `codex_model`. The sidecar
validates that draft by constructing the existing `ScienceProjectConfig` and
`RemoteProfileConfig` models, then writes:

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

`GET /openevo-api/desktop/project-configs` lists saved Desktop project configs
from the same config root. It scans
`<desktop-config-root>/projects/*/science.yaml`, validates each Science Project,
loads the matching remote profile from
`<desktop-config-root>/profiles/<remote-profile-id>.yaml`, and returns
deterministic summaries sorted by project slug. Summaries include only
non-secret fields: project slug, project name, task id, objective, source type
and label, remote profile id, remote host/user, config file paths, validity, and
a sanitized validation error when invalid. They do not expose raw password
values, key material, private key paths, password references, or passphrase
references.

`POST /openevo-api/desktop/project-configs/{project_slug}/activate` loads a
previously saved valid config into the current sidecar process after a Desktop
restart. It requires the same mutation token as other mutating endpoints,
rejects invalid slugs before path resolution, returns 404 for unknown saved
projects, and returns 422 for saved configs whose Science Project or matching
remote profile no longer validates. Successful activation returns the config
paths plus a refreshed shell status and replaces the in-process session, so
subsequent `/workspace`, `/bootstrap`, and `/run` calls use the activated saved
config without asking the user to locate YAML files manually.

The packaged Desktop UI consumes both endpoints in the Project Setup panel. On
sidecar connection it loads the saved config catalog, renders valid and invalid
summaries, disables activation for invalid configs, and shows the sanitized
validation error returned by the sidecar. Activating a valid saved config
refreshes the shell status, repopulates the setup draft from the active project,
and clears stale latest-run state. Saving a new draft refreshes the catalog so
the newly written config is available without restarting Desktop.
The same panel exposes the non-secret remote setup fields, including remote
profile id, SSH auth method, private-key path/reference ids, workspace root,
HTTP/HTTPS proxy, `NO_PROXY`, pip index URL, Hugging Face endpoint, and
`HF_HOME`, plus the execution-mode selector and relevant model input. Science
users can therefore configure either Codex subscription transcript mode or
self-deployed remote inference from Desktop without editing YAML for common
proxy, mirror, or model settings. Drafts using the legacy
`codex_managed_local_inference` value are accepted for compatibility and saved
back as `self-deployed`.

`POST /openevo-api/desktop/bootstrap` is the first mutating sidecar endpoint.
It is available only for config-backed sidecar sessions. It reuses
`build_sidecar_science_plan()`, `build_remote_bootstrap_plan()`, and
`execute_remote_bootstrap_plan()` to run the existing bootstrap executor, then
returns both the bootstrap report and refreshed shell status. The default serve
transport is dry-run; `openevo sidecar serve --transport ssh` selects the SSH
transport for the bootstrap endpoint. The user-facing `openevo desktop open`
launcher selects SSH by default, while exact-port serve and API-only sidecar
serve keep dry-run defaults for tests and integrations. Bootstrap does not
upload local folders or clone git task sources; workspace preparation remains a
separate lifecycle step so the UI can report source materialization
independently from runtime readiness.

`POST /openevo-api/desktop/workspace` executes that separate workspace
preparation lifecycle. It is available only for config-backed sidecar sessions
and reuses `build_sidecar_science_plan()` plus `execute_sidecar_plan()` so
`local_folder` uploads, `git_repository` clones, `remote_path` no-ops, and
`scratch` workspaces stay on the existing remote executor contract. The endpoint
returns both the full executor report and a top-level workspace summary, plus a
refreshed shell status. Workspace readiness updates only the SSH and Workspace
service rows; it does not imply bootstrap readiness, model availability, gateway
startup, rollout startup, or evolution worker startup.
Desktop keeps the most recent workspace report in the Bootstrap Readiness area
and surfaces failed or warning workspace actions with their message, command,
and stderr when present, so upload or clone failures are visible without opening
the raw API response.

Desktop also keeps the most recent bootstrap report in the same area. It renders
`next_actions`, failed or warning preflight checks with `remediation_kind`, and
failed or warning bootstrap steps. Long commands, paths, proxy URLs, and stderr
snippets are wrapped in the panel so remote dependency and setup failures remain
readable in the app.
Bootstrap includes a user-scoped exact OpenEvo Core check. Desktop/CLI searches
only bundled package-relative wheel directories for an `openevo-<version>-*.whl`
whose wheel metadata matches the local packaged version. If present, it uploads
only that wheel and bootstrap installs it with user-site `pip --force-reinstall`
before verifying remote package metadata, `openevo --version`, and
`openevo --help` with `~/.local/bin` prepended to PATH. If no bundled wheel is
available, bootstrap passes only when the remote package and CLI already report
the exact expected version; otherwise it fails clearly and does not install an
unpinned latest package from PyPI.
For managed Science runtime profiles, bootstrap also prepares the runtime image
without requiring the user to provide one. It runs
`docker pull <managed-image> || docker build ... -t <managed-image> ...`; Docker
build receives the same proxy environment and standard proxy build args. If the
remote Docker daemon itself needs registry mirrors or daemon-level proxy
configuration, bootstrap reports the failure instead of editing host-wide Docker
settings.
For self-deployed configs, the compiled experiment contains
`OPENEVO_MANAGED_HF_MODEL`, so the remote bootstrap plan also attempts a
Hugging Face snapshot download using the configured `HF_ENDPOINT`, `HF_HOME`,
and proxy/PIP environment. vLLM startup and health supervision are handled by
the separate service lifecycle.

Docker Compose is probed but not required for the current Desktop Science path.
Missing Compose is rendered as a warning because OpenEvo services are started
through the service lifecycle endpoint rather than a Compose stack.

`POST /openevo-api/desktop/services` starts the remote runtime services after
workspace and bootstrap readiness. It is available only for config-backed
sidecar sessions and requires the same mutation token. The sidecar rebuilds the
deterministic bootstrap plan, writes a remote service topology under
`<state_root>/services/topology.yaml`, then starts and checks the OpenEvo
service daemons needed by Desktop Science runs:

- evolution backend on `127.0.0.1:8200`;
- rollout on `127.0.0.1:8080`;
- gateway on `127.0.0.1:8100`;
- evolution worker bound to the same topology file;
- vLLM on `127.0.0.1:8000` for `self-deployed`.

The service commands use the remote user PATH plus `~/.local/bin` and export the
same proxy/PIP/Hugging Face environment rendered from the remote profile for the
whole remote command script. The self-deployed inference path installs `vllm`
with `python3 -m pip install --user vllm` if the import check fails, then
launches the OpenAI-compatible vLLM server for the configured Hugging Face
model. Subscription mode still starts the OpenEvo runtime services for rollout,
gateway, transcript capture, and evolution coordination, but it does not start a
local model server.

Each service start is followed by a bounded readiness poll. The standard
OpenEvo services poll their local health endpoints for 30 seconds. Managed vLLM
polls `/v1/models` for up to 900 seconds and verifies that the configured
Hugging Face model id is actually served before Desktop marks services ready.

The services endpoint returns a top-level readiness summary plus the full
service report. Desktop keeps the latest report in the Bootstrap Readiness area
and renders failed or warning start/health-check steps with their command,
message, and stderr when present. Remote stdout, stderr, and exception text are
redacted before they are returned so proxy URLs, pip index URLs, and URL
userinfo credentials are not displayed in Desktop. Re-running workspace
preparation or bootstrap invalidates the prior service readiness and clears
stale latest-run state, because the prepared workspace or state root may have
changed.

`POST /openevo-api/desktop/run` launches the configured Science task after
workspace, bootstrap, and service readiness. It is available only for
config-backed sidecar sessions, requires the same mutation token, and rejects
requests unless the Workspace service is `ready`, `status.bootstrap.ready` is
true, the latest bootstrap report contains both
`prepared_paths.experiment_snapshot` and `prepared_paths.state_root`, and the
latest services report is ready. The command is derived only from those
bootstrap paths:

```bash
PATH="$HOME/.local/bin:$PATH" openevo run <experiment_snapshot> --output-dir <state_root>/runs/<run-id> --json
```

The PATH prefix lets the run use the console script created by bootstrap's
remote user-site install without changing the remote user's shell profile.

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

After a terminal run status, Desktop calls
`GET /openevo-api/desktop/run/artifacts` with the same sidecar mutation token.
The endpoint is read-only but still token-protected because it reaches into the
remote run directory. It is available only for config-backed sidecar sessions
with a launched latest run, and it rejects active runs because the run
`summary.json` contract is complete only after `openevo run` exits.

The endpoint reads `<run.output_dir>/summary.json` on the remote machine through
the configured transport, parses it, and returns a compact timeline payload:
run id, output directory, summary status, experiment id/name, round count, and
per-task rounds with rollout status, dataset status, produced artifact ids, and
worker job summaries. Worker summaries include artifact type, method, worker
status, produced artifact ids, approved artifact ids, and promotion status. The
payload intentionally does not inline artifact contents; Desktop renders ids and
statuses so users can see the evolution process without transferring generated
memory, skill, agent-system, or adapter directories into the local browser.

If the remote summary file is missing, the endpoint returns 404. If remote
command execution or JSON parsing fails, it returns a sanitized 502 response.
Desktop renders the artifact timeline after terminal success or failure when a
summary is available, and shows the sanitized timeline-load error without
hiding the terminal run status. Saving or activating a different project config,
rerunning workspace sync, rerunning bootstrap, or restarting services clears the
latest run and its artifact timeline to avoid showing stale evolution state.

The sidecar generates a per-process mutation token and includes it in
`GET /openevo-api/desktop/shell` under `sidecar.mutation_token`. Mutating
requests must send that token in the non-simple
`X-OpenEvo-Sidecar-Token` header. Missing or invalid tokens are rejected before
any workspace, bootstrap, or run work starts. This is a local CSRF guard for the
Desktop sidecar: cross-site pages can submit simple localhost requests, but
cannot read the same-origin shell response or set the required custom header.
The sidecar allows only one mutating lifecycle action per config-backed session
at a time. A second request for the same lifecycle action returns a specific
409, such as `Desktop services are already running.` A different lifecycle
action started while workspace, bootstrap, services, or run is active also
returns 409. This prevents older workspace/bootstrap/service reports from
overwriting newer invalidation and prevents run launch from using stale service
readiness. Status updates from lifecycle actions are written under a shared
status lock.

Dry-run serve mode is intended for local UI development and smoke tests. It
exercises the same planning, status, and polling path, but it does not mutate
the remote server. A dry-run report can therefore show the UI path as ready
without proving that task workspaces, Docker images, or Hugging Face models were
actually prepared. Real remote preparation and run execution require
SSH transport. `openevo desktop open` selects SSH by default; `desktop serve`
and `sidecar serve` require `--transport ssh` when they should mutate a remote
server.

This slice adds a command-and-health-check service supervisor plus a local
sidecar run supervisor with one latest run per config-backed session. It does
not survive sidecar process restarts, stream incremental remote log files,
cancel remote process groups, expose resume, restart the sidecar process, run
Docker Compose, restart crashed remote daemons, transfer artifact contents or
diffs into Desktop, tune GPU placement or quantization for vLLM, or manage
dynamic adapters. Those operations remain behind future remote lifecycle
contracts.

## CLI

The initial user-visible CLI for this slice is:

```bash
openevo science compile CONFIG [--json] [--prepared-workspace task=/remote/path]
```

`--prepared-workspace` can be repeated when multiple tasks need prepared remote
workspace paths.

## Limitations

The current release includes local Desktop serving, config-backed sidecar
sessions, SSH-backed remote workspace preparation, remote bootstrap,
command-based service startup, run supervision, and terminal-run artifact
summary display. It still does not include:

- local credential vault or SSH tunnel management;
- Electron/native-app packaging or a supervised local sidecar daemon outside
  the `openevo desktop open` / `openevo desktop serve` process;
- Docker Compose lifecycle management;
- production vLLM lifecycle tuning, restart policy, GPU placement, or dynamic
  adapter loading;
- artifact content transfer or diff browsing in Desktop;
- parametric memory or adapter training for Science Projects.

Those capabilities remain separate layers above or below the Science Project
contract.
