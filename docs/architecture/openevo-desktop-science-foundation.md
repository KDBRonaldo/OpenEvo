# Release-incompatible foundation note

This document preserves pre-External-Beta foundation context. It is not External Beta release behavior.
Direct run commands, dry-run transports,
developer override env vars, legacy token headers, package-relative Core
artifacts, and command-based service facades are superseded by
`docs/maintainer/productization/spec.md`.

# OpenEvo Desktop Science Foundation

Tracked by #37.

This document defines the foundation contract for OpenEvo Desktop Science
projects. A Science Project config is the user-facing input for ordinary
science users. It validates a task, environment, execution mode, and evolution
targets, then compiles to the existing OpenEvo `ExperimentConfig` contract.

Users do not configure runtime images, Core gateway wiring, Docker lifecycle, or
model serving directly in the common path. The science layer chooses a runtime
profile and execution mode, and the lower-level OpenEvo experiment compiler
keeps responsibility for the concrete experiment payload.

## Boundary

The science layer is a config and compilation layer. It does not:

- run Codex;
- start Docker;
- open SSH connections;
- call model APIs;
- manage Docker Compose lifecycles.

It validates Science Project input and compiles it to the existing OpenEvo
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
remote workspace path for the task through the Python compiler API.

The science compiler does not upload local files or clone repositories itself.

## Environment Profiles

Science environment profiles compile to runtime images:

| Profile | Runtime image |
|---|---|
| `managed_science` | release-locked `openevo/science-runtime@sha256:...` |
| `python_research` | release-locked `openevo/python-research-runtime@sha256:...` |
| `custom_image` | developer-supplied override |

`custom_image` is an escape hatch for developers and advanced experiments. It is
not the default user path. It retains the image-declared user and therefore
cannot be combined with `codex_subscription_transcript`; project validation
returns an actionable error directing the user to a managed environment or
self-deployed execution before compilation or runtime startup.

For managed profiles, users do not upload or choose a runtime image in Desktop.
The Science compiler serializes the release lock's immutable repository digest
into `runtime.image`; the human-readable tag is only a local bootstrap alias.
Every `managed_science` run, including `codex_subscription_transcript` and
`self-deployed`, must reach Core as Docker + the exact canonical profile image +
host-user execution. Runtime config, compiled `RuntimeSpec`, Core launcher, and
Gateway admission independently enforce the profile alias, while the release
contract binds that alias to a full trusted `sha256` rather than trusting the
tag. Release bootstrap pulls `repository@digest`, tags it only as a local alias,
and verifies Docker `RepoDigests`/image ID. DockerRuntime repeats the proof
immediately before create and uses the matched immutable reference before any
subscription credential mount is added. Registry tag drift, no digest, or an
identity mismatch fails closed.

Release mode does not build on the remote host. Explicit development mode may
write the OpenEvo-managed Dockerfile under the run state directory and build on
pull failure; a custom/unknown profile never receives that privilege. Both
Dockerfile base images are digest-pinned and the final image must still match
the trusted release digest. The generated image contains Python, Node, common
build tools, and the pinned Codex CLI required by the Codex harness. The fallback
build fetches Debian package metadata over HTTPS and retains the distribution's
archive signature verification; proxy or mirror failures never downgrade that
verification. Build proxy args come only from the selected project/profile
(`HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY`, with lowercase equivalents where
required). The launcher must not inherit stale Desktop or server shell proxy
values; the UI-selected proxy port is passed verbatim through explicit build
args.

For this exact Core-owned Dockerfile only, fallback build uses
`docker build --network=host` so an explicitly configured server-loopback proxy
such as `127.0.0.1:<port>` retains host semantics inside build steps. Custom
images remain pull-only and never receive build or host-network privileges.

The managed Science build was verified on 2026-07-14 with the generated
Dockerfile, `docker build --network=host`, and explicit host
`HTTP(S)_PROXY=http://127.0.0.1:7890`. It produced
`openevo/science-runtime:0.1.0` (reported digest prefix `sha256:16837a`, size
376342787 bytes) with Codex CLI 0.121.0, Python 3.12.13, and Git 2.39.5. A
`12345:12345` container user with `HOME=/tmp/openevo-home` could run Codex, and
the image contained no `/root/.codex/auth.json`; credentials are supplied only
by the later private runtime mount.

## Execution Modes

`codex_subscription_transcript` uses Codex subscription authentication and sets
`agent.settings.capture_mode="transcript"`. This produces transcript trajectory
data for text evolution and explicitly has no token-level metrics.
Remote preflight checks that the remote user has `codex` and
`~/.codex/auth.json`. The default subscription model is `gpt-5.5`; an explicit
user model remains unchanged. This default was verified on 2026-07-14 with
`codex-cli 0.144.1` and a logged-in ChatGPT account: the same minimal ephemeral,
read-only JSON smoke succeeded with `gpt-5.5`, while `gpt-5.1-codex-mini`
returned HTTP 400 unsupported.

During each gateway runtime session, Core creates a private credential root
outside the session tree and fixes container-visible
`CODEX_HOME=/openevo/credentials/codex`. Workspace sync and setup complete while
that mount is empty; only then is the host auth file staged as `auth.json`.
`HOME`, `PATH`, and `CODEX_HOME` are Core-owned across agent, runtime, and action
env. Subscription execution calls `/home/openevo/.local/bin/codex` directly, so
workspace content cannot shadow the binary.
The source must be a private, remote-user-owned, link-count-one regular file of
bounded size; symlinks, hard links, special files, owner mismatches, and path
replacement are rejected. The gateway copies from a verified no-follow file
descriptor into an exclusive `0600` target under a `0700` root and rechecks
source/target size, SHA-256 digest, identity, link count, and change times. Both
source and credential-root absolute pathname chains are pinned component by
component and rechecked before and after publication. Docker daemon PID
namespaces do not reliably expose Core's `/proc/<pid>/fd/<fd>` paths, so Docker
receives two Core-owned, daemon-visible sources. DockerRuntime creates and pins an
empty home view and empty auth placeholder under the journaled root, mounts that
view read-only at `CODEX_HOME`, then mounts the sibling exact auth file read-only
over the placeholder. Because the auth source is outside the view, renaming it on
the host cannot move the container mountpoint to another name or reveal a
replacement. Core verifies both create-time mount records and every held
root/view/placeholder/auth binding, uses `restart=no`, and brackets in-container
view/auth adoption with stable container process identity. The empty staging
inode is durably journaled before secret bytes are copied, then its final full
identity is journaled after publication.

Core derives a bounded exact-value redactor from the verified auth JSON and its
string leaves. Core stdout/stderr and transcript logs live in a node-private,
unmounted log authority rather than the host-user agent's session bind. Each
leaf is bounded and published from an exclusive `0600`, no-follow regular inode;
the final transcript read uses the same held-root authority and rejects links,
FIFOs, sockets, hard links, and replacements without blocking. Redaction writes
only to this private log authority. Workspace inputs, workspace outputs, and
artifacts are never redaction write targets. The complete private-log scan is
preflighted before writes; a per-file or aggregate limit fails finalization and
leaves every original byte unchanged. Codex is the necessary trusted
credential consumer, so this boundary does not claim to prevent Codex from
actively transforming and transmitting a secret. It does guarantee that
OpenEvo's own sync/scanner/capture paths do not automatically copy the verified
auth document or known leaf values verbatim.

Managed containers use the Core process UID/GID with
`HOME=/openevo/session/home`. The managed `PATH` still begins with
`/home/openevo/.local/bin`, which contains the pinned Codex binary. Codex state
remains on the dedicated credential mount, while evolution skills remain on the
writable session bind mount. No
host-user lifecycle path applies recursive `a+rwX`. Teardown uses the
dispatch-pinned session root identity and a bounded fd-relative no-follow walk,
so nested `000` directories converge without following symlinks; owner or root
identity replacement fails closed. Every recursive directory pathname is
rechecked against the opened inode after the final scan. A configured evaluator
builds its trajectory and runs before runtime stop so it retains valid live
runtime references; sessions without evaluators build after absence proof. Core
never publishes a subscription result until all credential-capable containers
have been removed by pinned container ID and proven absent. Post-absence recovery
uses a separate bounded finalization budget to preserve transcript bytes already
captured before execution timeout/cancel, then defensively redacts the in-memory
result before export. A private v7 cleanup journal with monotonic revisions drives independent
startup/shutdown retries. It binds the exact staged auth inode and persists a
canonical result digest and monotonic
evolution-export/callback success proofs; an unknown callback or failed export
retains completion data, transcript/session, log, credential roots, and the
journal. Stable event identity and callback idempotency headers make retries
non-polluting. Journal transitions and recovery scans use a bounded
cross-process `flock` bound to the same held no-follow journal root; lock and root
identity are rechecked before durable success. Cancel authority is committed
before runtime cancellation, remains monotonic over completed evaluation, and a
failed commit leaves the runtime untouched. Terminal pending status/error is published to live memory only
after the journal transition is durable. Cleanup begins only after both required
phases are durably successful. A separate bounded no-follow recursive scan first
finds and scrubs the journal-bound auth inode even after a nested rename; a limit,
race, or missing inode retains the root and journal before ordinary deletion.
Historical publication-handoff cleanup without an auth identity scrubs every
owned regular file in the dedicated root, while v5 terminal finalization still
fails closed rather than trusting a replacement as redaction authority.
The journal's private parent also stores an immutable marker binding the
normalized journal path, no-follow ancestor identity chain, and root inode.
Startup rejects root replacement and symlinked ancestors, and preflights row,
filename, metadata, per-record, and aggregate-byte budgets before reading record
content.

`self-deployed` uses proxy authentication and requires `execution.hf_model`.
The legacy config value `codex_managed_local_inference` remains accepted as an
input alias at Desktop and Science model boundaries, but configs normalize and
emit `self-deployed`. The compiler sets the agent model to that Hugging Face
model, explicitly sets `agent.settings.capture_mode="transcript"`, and injects
`OPENEVO_MANAGED_HF_MODEL` into the runtime environment. The remote Desktop
service lifecycle starts vLLM, the gateway, and the proxy path before a run can
launch.

The `text_memory`, `skill_bundle`, and `agent_system` targets use pure-text
transcript trajectories in both execution modes. Their trajectories report
`token_level_metrics_available=false`; proxy authentication in self-deployed
mode does not opt these Science Project runs into token-level capture.

Science Projects do not support an enabled
`evolution.targets.parametric_memory` selection in this foundation slice,
including self-deployed projects. Parametric memory requires a separate adapter
source or trainer contract that is not part of the ordinary-user Science
Project workflow yet.

## Runtime Prepare

Task `setup_commands` compile to `RuntimeSpec.prepare` exec actions running in
the experiment workspace. The science compiler first emits a prepare action that
creates `/openevo/session/workspace`, then emits user setup commands. Workspace
upload is handled by the lower-level experiment compiler before these prepare
actions run for workspace-backed tasks, so dependency installation and other
setup steps can read workspace files.

## Evolution Targets

Science Projects support text memory, skill bundle, and agent system evolution
targets in this foundation slice. Parametric memory is intentionally rejected for
all Science Projects until adapter source and trainer configuration are defined.

The persisted and sidecar request shape is the same generic Core contract used
by experiments:

```yaml
evolution:
  targets:
    text_memory:
      enabled: true
      method: text_memory_reflector
      config: {}
    skill_bundle:
      enabled: true
      method: skill_bundle_reflector
      config: {}
    agent_system:
      enabled: true
      method: auto
      config:
        target_path: AGENTS.md
    parametric_memory:
      enabled: false
      method: parametric_memory_register
      config: {}
```

Target IDs are generic map keys and each value contains only `enabled`,
`method`, and `config`. Enabled targets require a method. Disabled targets may
retain draft settings. The removed flat booleans and experiment-level
`artifacts` object are rejected rather than normalized through compatibility
aliases.

## Preflight

`openevo.deployment.preflight` defines fakeable remote preflight contracts. It does
not implement SSH transport. Callers provide a `RemoteProbe` that can run remote
commands and return structured results.

Preflight checks SSH first with `true`. If SSH fails, it returns immediately and
does not run later checks. After SSH succeeds, Docker is checked with
`docker info`, disk capacity is checked with `df -Pk "$HOME"`, and other remote
capabilities are reported through the same fakeable probe contract. Codex CLI
and subscription checks run only when subscription execution is required.

## Desktop UI

The Desktop UI lives in the top-level `desktop/` product package. The Tauri
native host owns the installed application window and starts the local Python
sidecar launcher; the Vite development server remains a developer and CI smoke
path. The React UI uses the local sidecar API when available and keeps a
fixture fallback so the layout remains inspectable when the sidecar is not
running.

The shell intentionally keeps Terminal Bench and low-level runtime image fields
out of the default flow. It displays the remote profile, proxy settings, Science
Project summary, bootstrap paths, lifecycle readiness, and evolution timeline
using the same concepts as the Python contracts.

### Local Desktop Serve

The installable Python distribution is named `openevo`. It includes only the
OpenEvo Core Backend modules that provide rollout, gateway, trajectory, and
evolution backend runtime code. Desktop assets live under the top-level
`desktop/packaging/web/` path for Desktop release and smoke validation. In the
Core Backend migration phase, the only Python console script is the backend
launcher:

```bash
openevo-backend --help
openevo-backend serve --help
```

`openevo-backend serve` starts the typed Core Backend API. Desktop-native launch
is handled by the Tauri host under `desktop/src-tauri/`, and the local Desktop
sidecar can be served with `python3 -m desktop.server.launcher` in development
or by the bundled PyInstaller sidecar binary during app launch. Platform
signing, notarization, and update policy are separate Desktop release hardening
tasks.
The root path `/` redirects to `/openevo`. The Desktop asset set is built in
OpenEvo-only mode and kept under the top-level `desktop/packaging/web/` path,
so users do not see the shared OpenEvo Observability navigation and the Core
wheel does not package Desktop assets.
The local server still returns the SPA for compatibility routes `/tasks`,
`/tasks/*`, `/sessions`, `/sessions/*`, and `/compare`; unknown
`/openevo-api/*` paths remain API 404s.

Packaged Desktop assets are refreshed with:

```bash
cd desktop && npm run build:openevo
rsync -a --delete desktop/dist/ desktop/packaging/web/
```

The release smoke check runs on Node 22, audits the Desktop frontend dependency
graph for high or critical advisories, rebuilds the OpenEvo-only Desktop assets,
verifies that `desktop/packaging/web` matches `desktop/dist`, builds the Python
Core wheel, inspects the wheel metadata, console scripts, bundled remote-install
wheel, and Core/Desktop package boundary, then installs the wheel into a clean
environment and runs the installed backend launcher plus Desktop API smoke
checks. The installed-wheel smoke covers Core capabilities, method metadata,
project config save, workspace, bootstrap, services, service status, run launch,
backend-facade timeline/artifact reads, and artifact preview rendering:

```bash
cd desktop
npm ci
npm audit --audit-level=high
npm test -- --run
npm run build:openevo
cd ..
diff -qr desktop/dist desktop/packaging/web
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
.openevo-wheel-smoke/bin/openevo-backend --help
.openevo-wheel-smoke/bin/openevo-backend serve --help
.openevo-wheel-smoke/bin/openevo-backend run --help
PYTHONPATH=. .openevo-wheel-smoke/bin/python scripts/ci/smoke_openevo_desktop_wheel.py
```

OpenEvo package and Desktop-sidecar Python regressions are checked with:

```bash
ruff check src/openevo tests/openevo
PYTHONPATH=src:. python -m pytest tests/ci/test_openevo_python_workflow.py tests/openevo -q
```

The former Python Desktop and sidecar console-script entrypoints are not
exposed after the Core Backend package migration. Desktop-created projects can
start from a no-config sidecar. In that mode the sidecar receives a writable
local config root from `--desktop-config-root`; the local launcher defaults to
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
`HF_HOME`, plus the complete canonical `evolution.targets` selections. Desktop
keeps that map through load/edit/save. Target controls, friendly method choices,
support state, and config fields come only from the connected Core capability
projection. A toggle preserves every existing non-null, remotely supported
method and its config. A method change atomically installs that method's remote
default config; a Core-owned resolver starts with an empty override and existing
resolver config remains opaque. When enabling a missing, method-null, removed,
or unsupported selection, Desktop binds a supported remote effective default
only when one exists. It never guesses another method when Core publishes no
effective default.

Visible method config is edited through Core's bounded closed-schema subset and
stored as a partial override without eagerly adding schema defaults. Desktop
recursively merges Core defaults and that override to validate the complete
effective config, so a missing required user-owned field blocks an evolution
change. Core/compiler-owned required fields are removed from the Desktop
projection through descriptor field/source ownership metadata and are injected
again before Core plan compilation. Controls display the merged effective value,
including inherited booleans and nested defaults, while edits still persist only
the explicit override. A reset action replaces the current visible method's
entire override with its remote `defaultConfig`, allowing a saved override with
fields removed by a newer remote schema to recover without preserving unknown
keys. Resetting a Core-owned resolver replaces its opaque override with an empty
object. Hidden accepted methods, stale methods, and null selections cannot be
reset because Desktop has no visible editable contract for them. Present invalid
values, non-finite numbers, and integers that cannot round-trip through
JavaScript block save and run. Resolver config otherwise remains opaque, and
hidden accepted methods, stale methods, and unknown targets keep opaque config
unchanged.
Enabled unknown targets remain visible and block run launch until disabled;
disabled unknown selections stay in the canonical map without being promoted
into ordinary-user controls. Desktop cannot silently discard future targets.
The response does not include raw passwords or private-key material.
The React offline fallback contains no target or method defaults; before the
sidecar responds it shows only neutral transcript state. Initial target
selections come from the sidecar's validated Core-owned Science Project config,
then become editable only against connected remote capabilities.
Valid Core-owned resolver values such as `agent_system.method=auto` and valid
explicit methods hidden from the ordinary-user picker remain accepted through
remote `selection_resolvers` and `accepted_methods`. Unsupported visible options
remain identifiable with the Core support reason but cannot be newly selected.
Draft changes must be saved and activated before Start Run can use them. After
save, Desktop rebuilds the draft from the canonical sidecar status so
server-side whitespace and config normalization cannot leave a false
unsaved-change block. The complete setup form is disabled while save or saved
config activation is in flight, preventing a late response from overwriting a
newer local edit. Evolution-changing saves additionally require a current remote
capability projection for the draft execution mode; non-evolution setup changes
remain saveable when the evolution map is unchanged. A disabled target's retained
config is not treated as an active validation failure. Saved-config activation is
disabled while the current draft is dirty; the user must save or explicitly use
Discard Changes before another config can replace it.
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
settings, execution mode, mode-specific model field, and complete
`evolution.targets` selections. Subscription transcript drafts carry
`codex_model`; self-deployed
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
and clears stale latest-run state, but cannot overwrite an unsaved draft without
an explicit discard. Saving a new draft refreshes the catalog so
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
returns both the bootstrap report and refreshed shell status. Desktop-native
launch will select the transport for the bootstrap endpoint. Bootstrap does not
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
Bootstrap includes a user-scoped exact OpenEvo Core check. Desktop searches
only bundled package-relative wheel directories for an `openevo-<version>-*.whl`
whose wheel metadata matches the local packaged version. If present, it uploads
only that wheel and bootstrap installs it with user-site `pip --force-reinstall`
before verifying remote package metadata and the `openevo-backend` launcher with
`~/.local/bin` prepended to PATH. If no bundled wheel is available, bootstrap
passes only when the remote package and backend launcher already report the exact
expected version; otherwise it fails clearly and does not install an unpinned
latest package from PyPI.
For managed Science runtime profiles, bootstrap also prepares the runtime image
without requiring the user to provide one. It runs
`docker pull <repository>@<trusted-digest>`, creates the internal alias, and
fails unless inspect proves the expected RepoDigest/image ID. Explicit developer
mode may instead use the digest-pinned fallback Dockerfile and receives the same
proxy environment and standard proxy build args, but it must produce the same
trusted digest. If the remote Docker daemon needs registry mirrors or
daemon-level proxy configuration, bootstrap reports the failure instead of
editing host-wide Docker settings.
For self-deployed configs, the compiled experiment contains
`OPENEVO_MANAGED_HF_MODEL`, so the remote bootstrap plan also attempts a
Hugging Face snapshot download using the configured `HF_ENDPOINT`, `HF_HOME`,
and proxy/PIP environment. vLLM startup and health supervision are handled by
the separate service lifecycle.

Docker Compose is probed but not required for the current Desktop Science path.
Missing Compose is rendered as a warning because OpenEvo services are started
through the service lifecycle endpoint rather than a Compose stack.

The following `/openevo-api` service flow is a historical scaffold and is not a
release Core Control path. Formal Desktop bootstrap must not call it after Core
attach; run and service lifecycle must go through the active tunnel and formal
`/v1/*` operations.

`POST /openevo-api/desktop/services` historically starts the remote runtime services after
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

The historical services plan no longer contains a per-run Core daemon. The
host-global Core listener is selected and authenticated only by
`openevo.deployment.core_control`; deleted fixed-port launcher arguments are not
retained as a compatibility wrapper.

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
PATH="$HOME/.local/bin:$PATH" openevo-backend run <experiment_snapshot> --output-dir <state_root>/runs/<run-id> --artifact-root <state_root>/evolution/artifacts --json
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

After a terminal run status, Desktop calls the local backend facade with the
same sidecar mutation token:

```text
GET /openevo-api/backend/runs/{run_id}/timeline
GET /openevo-api/backend/runs/{run_id}/artifacts
GET /openevo-api/backend/artifacts/{artifact_id}/content
GET /openevo-api/backend/artifacts/{artifact_id}/diff
```

The facade forwards these requests to the remote OpenEvo Core Backend through
the sidecar-managed SSH tunnel created after remote services become ready. The
sidecar preserves typed backend payloads and errors; it does not read run
`summary.json`, parse artifact directories, execute evolution methods, or
define a second method registry for Desktop. The remote backend receives the
bootstrap `state_root` and reads Core-owned
`<state_root>/runs/<run-id>/summary.json` plus `<state_root>/evolution/`
artifacts for sidecar-launched runs.

Desktop renders Core-provided timeline events, artifact summaries, promoted
artifact previews, and text diffs after terminal success or failure when Core
has data available. Timeline/artifact load errors are shown without hiding the
terminal run status. Saving or activating a different project config, rerunning
workspace sync, rerunning bootstrap, or restarting services clears the latest
run, timeline, artifacts, and preview to avoid showing stale evolution state.

The sidecar generates a per-process mutation token and includes it in
`GET /openevo-api/desktop/shell` under `sidecar.mutation_token`. Mutating
requests and backend facade requests must send that token in the non-simple
`X-OpenEvo-Sidecar-Token` header. Missing or invalid tokens are rejected before
any workspace, bootstrap, run, or backend facade work starts. This is a local
CSRF guard for the Desktop sidecar: cross-site pages can submit simple localhost
requests, but
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
SSH transport. Desktop-native launch is responsible for choosing dry-run or SSH
transport when it should mutate a remote server.

This slice adds a command-and-health-check service supervisor plus a local
sidecar run supervisor with one latest run per config-backed session. It does
not survive sidecar process restarts, stream incremental remote log files,
cancel remote process groups, expose resume, run Docker Compose, restart
crashed remote daemons, browse artifact diffs, tune GPU placement or
quantization for vLLM, or manage dynamic adapters. The Tauri host starts and
stops the bundled local sidecar process; restart policy is still a product
hardening task. Human-readable artifact content for `text_memory`,
`skill_bundle`, and `agent_system` is available through the backend artifact
preview facade.

## Backend Launcher

The Python package no longer exposes a user-facing `openevo` console script.
Its closed internal script set is `openevo-backend` for the backend launcher and
`openevo-core-service` for Desktop's maintenance supervisor. They are backend
and product automation entry points, not separately published ordinary-user CLI
surfaces.

## Limitations

The current release includes local Desktop serving, config-backed sidecar
sessions, SSH-backed remote workspace preparation, remote bootstrap,
command-based service startup, run supervision, backend-facade timeline and
artifact preview display, and human-readable artifact content viewing. It still
does not include:

- local credential vault or persistent SSH tunnel monitoring/reconnect;
- auto-update, code signing, notarization, or OS credential vault integration;
- Docker Compose lifecycle management;
- production vLLM lifecycle tuning, restart policy, GPU placement, or dynamic
  adapter loading;
- rich artifact directory browsing in Desktop;
- parametric memory or adapter training for Science Projects.

Those capabilities remain separate layers above or below the Science Project
contract.
