# OpenEvo

OpenEvo is a product for running and evolving agent systems on real scientific
workflows. It has two release-facing surfaces:

- **OpenEvo Desktop**: the macOS application used by scientists to configure a
  remote server, start runs, and inspect memory, skill, and agent-system
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
-> configure a remote server and optional network proxy
-> run doctor/bootstrap from Desktop
-> start the remote OpenEvo Core Backend
-> choose Codex subscription transcript mode or Self-Deployed Reference mode
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

**Self-Deployed Reference mode** uses a remote model-serving path, initially
vLLM. It supports the same non-parametric evolution path and provides the
deployment structure for future parameter-oriented work.

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

benchmarks/
  terminal_bench/  standalone release-maintainer benchmark automation

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

The product dependency direction is `desktop -> src/openevo`; Core Backend must
not import Desktop code. Standalone benchmark packages may import Core
contracts, while Core and Desktop must not import or package benchmark code.

## Core Backend

The Python package is named `openevo`. The public console entrypoint is the
server-side backend launcher:

```bash
openevo-backend --help
openevo-backend serve --help
```

`openevo-backend serve` starts the typed backend API used by Desktop through an
SSH tunnel. `openevo-backend run` is a backend maintenance and automation
entrypoint for experiment snapshots; it is not the ordinary-user product UI.

Maintainer-only backend automation smoke:

<!-- openevo:maintainer-only-command -->
```bash {.openevo-maintainer-only}
openevo-backend run --help
openevo-backend run examples/science-minimal/experiment.yaml --dry-run --json
```

## Development Setup

<!-- openevo:maintainer-only-command -->
```bash {.openevo-maintainer-only}
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install -e benchmarks/terminal_bench
pip install pytest pytest-asyncio ruff build twine
```

Focused Python checks:

<!-- openevo:maintainer-only-command -->
```bash {.openevo-maintainer-only}
ruff check src tests scripts benchmarks/terminal_bench
PYTHONPATH=src:. python -m pytest tests/ci tests/openevo tests/evolution -q
python -m pytest benchmarks/terminal_bench/tests -q
```

Desktop checks:

<!-- openevo:maintainer-only-command -->
```bash {.openevo-maintainer-only}
cd desktop
npm ci
npm audit --audit-level=high
npm test -- --run
npm run typecheck
npm run build:openevo
cd src-tauri
# Ubuntu/Linux CI runners need Tauri native packages such as
# libwebkit2gtk-4.1-dev, libayatana-appindicator3-dev, libgtk-3-dev,
# libxdo-dev, librsvg2-dev, and patchelf.
cargo metadata --locked --format-version 1
cargo test --locked
```

Desktop's local provider store keeps the idempotency reservation for every
nonterminal local operation even after the normal retention deadline. Startup
and replay require that reservation's canonical response and unique operation
ID to match an operation-side authority digest over the full action identity,
request digest, resource scope, and operation kind. Operation reads and replays
do not reconcile or cancel live work, and every nonterminal operation must fit
its fixed startup-cancellation slot before it is persisted. A nullable operation
authority is only an in-transaction staging state: every mutation precommit and
startup recovery rejects any operation without a digest. Before startup changes
resource state or cancels work, every live operation must have exactly one
canonical idempotency row with the same route, scope, key, request digest, kind,
operation ID, and authority digest, and its fixed terminal reservation must fit.
Corrupt live records are never converted into cancellation records. The
canonical v2 schema fingerprint is the only accepted v2 authority: the
unreleased intermediate branch-private v2 layout is rejected at startup without
migration. Terminal reservations become eligible for ordinary retention cleanup.

Run retry recovery also preserves one exact mutation intent across an ambiguous
transport or response-validation outcome. The release provider may replay that
same run ID, idempotency key, ETag, and observed renderer epoch after later
refreshes advance the view; a new or cross-wired intent must pass the current
snapshot checks. A typed API rejection ends this replay authority immediately
and does not start reconciliation polling. Before transport, the provider saves
a bounded, non-secret copy of the original run and intent in Desktop's persistent
WebView storage so a renderer or application restart cannot mint a second action.
A 2xx response is accepted only when it preserves the complete canonical prefix,
same project, and exactly one new current attempt. The provider then overlays the
validated run into both its own cache and the renderer until a fresh Core
aggregate independently proves that same appended attempt.

Current release smoke checks are pre-External-Beta maintainer checks. They
validate Core Backend wheel identity and Desktop asset packaging, but they do
not publish GitHub Release assets, PyPI artifacts, or a release-ready `.dmg`.
The External Beta release gates and required Core, DMG, checksum, and
release-note artifacts are defined in
`docs/maintainer/productization/spec.md`.

## Pre-External-Beta Release Smoke

The historical smoke path is maintainer-only and predates the External Beta
release contract. It is useful for local regression checks, but it is not the
release process and does not create a releasable DMG, GitHub Release, or PyPI
artifact.

External Beta release work must use the descriptor-matched Core artifact, the
exact packaged DMG and checksums, and the release gates in
`docs/maintainer/productization/spec.md`. PyPI is not part of this release.

## Examples

- `examples/science-minimal/`: smallest Core Backend experiment config.
- `examples/science-with-local-folder/`: ordinary science project shape that
  uses a user workspace folder.
- `examples/self-deployed-model/`: Self-Deployed Reference model-serving
  configuration notes.
- `examples/backend-automation/`: backend automation examples for maintainers.
- `examples/research-benchmarks/`: release-excluded research/maintainer-only
  benchmark and training examples for OpenEvo developers and researchers, not an
  ordinary-user Desktop quickstart.

Benchmark examples are not Desktop quickstarts. They translate external
benchmark tasks and results into the same Core records, datasets, metrics, jobs,
artifacts, and context inputs that Desktop-backed runs use.

## Documentation

- User guidance: [docs/user/README.md](docs/user/README.md)
- Core Backend API: `docs/core/backend-api.md`
- Release-facing architecture index: `docs/architecture/README.md`
- Security policy: [SECURITY.md](SECURITY.md)
- Runtime and evolution contracts:
  `docs/architecture/core-runtime-system-overview.md`,
  `docs/architecture/evolution-api-and-method-integration.md`,
  `docs/architecture/evolution-backend.md`,
  `docs/architecture/evolution-runtime-context.md`,
  `docs/architecture/runtime-injection.md`

## Contributing

OpenEvo is still pre-release. Non-trivial changes should start from a GitHub
issue, preserve existing evolution algorithm behavior unless explicitly scoped
otherwise, update the relevant docs, and pass focused tests before review.
See `AGENTS.md` and `CONTRIBUTING.md` for repository workflow.
