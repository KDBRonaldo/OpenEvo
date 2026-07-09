# OpenEvo

OpenEvo is a product for running and evolving agent systems on real scientific
workflows. It has two release-facing surfaces:

- **OpenEvo Desktop**: the macOS application used by scientists to configure a
  remote GPU server, start runs, and inspect memory, skill, and agent-system
  evolution.
- **OpenEvo Core Backend**: the Python runtime installed on the remote server.
  It owns harness execution, runtime sessions, trajectory capture, datasets,
  jobs, workers, artifacts, context resolution, deployment, and backend APIs.

Desktop wraps Core Backend. Core Backend is the source of truth for execution
and evolution behavior.

## User Model

The ordinary-user flow is:

```text
Install OpenEvo Desktop .dmg
-> create a science project
-> configure a remote GPU server and optional network proxy
-> run doctor/bootstrap from Desktop
-> start the remote OpenEvo Core Backend
-> choose Codex subscription transcript mode or self-deployed mode
-> launch a science run
-> monitor services, logs, timeline, and evolved artifacts
```

OpenEvo Desktop should not require a scientist to install Python packages,
manually upload runtime images, or operate individual backend services. When a
fresh remote server can be prepared safely from user-level permissions, OpenEvo
does that setup. If Docker permissions, account login, system packages, or
network policy need manual action, Desktop reports a typed error and the next
action.

## Execution Modes

**Codex subscription transcript mode** uses a subscription-authenticated Codex
harness on the remote server. It requires transcript capture, does not call
model APIs directly, and supports non-parametric evolution such as text memory,
skill bundles, and agent-system instructions.

**Self-deployed mode** uses a remote model-serving path, initially vLLM. It
supports the same non-parametric evolution path and provides the deployment
structure for future parameter-oriented work.

In both modes, OpenEvo is a wrapper around an existing harness. OpenEvo captures
or ingests trajectories/transcripts, evolves typed artifacts, and injects the
selected context into later sessions.

## Repository Structure

```text
src/openevo/
  backend/       remote Core Backend API and launchers
  deployment/    SSH, preflight, bootstrap, workspace sync, services
  experiments/   experiment compiler, runner, and promotion gates
  evolution/     datasets, jobs, workers, artifacts, methods, context resolver
  gateway/       proxy, runtime injection, completion capture
  harness/       Codex and other harness contracts/presets
  projects/      science project schemas and compiler
  rollout/       rollout scheduler and aggregation
  runtime/       Docker and Apptainer runtime abstractions
  trajectory/    trajectory builders and evaluators

desktop/
  src/           React user interface
  src-tauri/     macOS native host
  sidecar/       local Desktop facade
  packaging/     bundled sidecar and packaged web assets

docs/
  user/          ordinary-user Desktop guidance
  core/          Core Backend contracts
  architecture/  current architecture notes
  maintainer/    release, process, and migration material

examples/
  science-minimal/
  science-with-local-folder/
  self-deployed-model/
  backend-automation/
  research-benchmarks/
```

The dependency direction is `desktop -> src/openevo`; Core Backend must not
import Desktop code.

## Core Backend

The Python package is named `openevo`. The public console entrypoint is the
server-side backend launcher:

```bash
openevo-backend --help
openevo-backend serve --help
openevo-backend run --help
```

`openevo-backend serve` starts the typed backend API used by Desktop through an
SSH tunnel. `openevo-backend run` is a backend maintenance and automation
entrypoint for experiment snapshots; it is not the ordinary-user product UI.

Minimal experiment smoke:

```bash
openevo-backend run examples/science-minimal/experiment.yaml --dry-run --json
```

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Focused Python checks:

```bash
ruff check src/openevo tests/openevo
PYTHONPATH=src:. python -m pytest tests/ci tests/openevo -q
```

Desktop checks:

```bash
cd desktop
npm ci
npm audit --audit-level=high
npm test -- --run
npm run build:openevo
cd src-tauri
cargo test --locked
```

Release smoke checks build the Core Backend wheel, package the remote-install
wheel, validate Desktop assets, and build the macOS `.dmg` in GitHub Actions.
See `docs/architecture/openevo-desktop-release.md` and
`docs/maintainer/release-process.md`.

## Release Smoke

The release smoke path runs on Node 22 for Desktop assets and validates the
installed Core Backend wheel:

```bash
cd desktop
npm ci
npm audit --audit-level=high
npm test -- --run
npm run build:openevo
cd ..
diff -qr desktop/dist desktop/packaging/web
rm -rf .openevo-remote-wheel src/openevo/wheels dist
python -m build --wheel --outdir .openevo-remote-wheel
mkdir -p src/openevo/wheels
cp .openevo-remote-wheel/openevo-*.whl src/openevo/wheels/
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

The wheel smoke covers the config-backed Desktop lifecycle through the packaged
sidecar facade. PyPI trusted publishing uses
`pypa/gh-action-pypi-publish@release/v1` after a GitHub release is published.

## Examples

- `examples/science-minimal/`: smallest Core Backend experiment config.
- `examples/science-with-local-folder/`: ordinary science project shape that
  uses a user workspace folder.
- `examples/self-deployed-model/`: self-deployed model-serving configuration
  notes.
- `examples/backend-automation/`: backend automation examples for maintainers.
- `examples/research-benchmarks/`: benchmark and training examples for OpenEvo
  developers and researchers.

Benchmark examples are not Desktop quickstarts. They translate external
benchmark tasks and results into the same Core records, datasets, metrics, jobs,
artifacts, and context inputs that Desktop-backed runs use.

## Documentation

- User guidance: `docs/user/`
- Core Backend API: `docs/core/backend-api.md`
- Runtime and evolution contracts: `docs/architecture/`
- Maintainer release/process docs: `docs/maintainer/`

## Contributing

OpenEvo is still pre-release. Non-trivial changes should start from a GitHub
issue, preserve existing evolution algorithm behavior unless explicitly scoped
otherwise, update the relevant docs, and pass focused tests before review.
See `AGENTS.md` and `CONTRIBUTING.md` for repository workflow.
