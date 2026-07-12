# Testing

Use focused tests for ordinary changes and broaden the suite when touching
shared contracts.

## Focused Tests

Run the smallest meaningful test set first, then broaden when a change touches a
shared contract. Harness changes should cover harness fixtures; gateway/runtime
changes should cover gateway and trajectory tests; evolution backend changes
should cover datasets, jobs, artifacts, workers, context resolver, and runtime
injection. Do not claim a release gate is protected by a focused test unless the
test checks the exact contract named in the spec.

## Core Backend

```bash
ruff check src tests scripts
PYTHONPATH=src:. python -m pytest \
  tests/ci \
  tests/config \
  tests/platform \
  tests/test_evolution_agent_harnesses.py \
  tests/backend \
  tests/evolution \
  tests/gateway \
  tests/trajectory \
  tests/rollout \
  tests/openevo/remote \
  tests/openevo/science \
  tests/openevo/sidecar \
  tests/openevo/desktop \
  tests/openevo/test_experiment_compiler.py \
  tests/openevo/test_experiment_models.py \
  tests/openevo/test_experiment_runner.py \
  tests/openevo/test_core_capabilities.py \
  -q
```

Run relevant module suites when touching shared behavior:

```bash
PYTHONPATH=src:. python -m pytest tests/backend tests/evolution tests/gateway tests/trajectory tests/rollout -q
```

## Release Gate Tests

Release gate tests are stricter than local smoke tests. Their output must identify
the candidate commit, exact inputs, configuration, result, and produced release
artifact where applicable. Use the smallest durable report that proves the
behavior; do not create a schema/validator/report stack for every check.

Required release-gate families are:

- protected algorithm behavior, benchmark boundaries, and source identity;
- Core install artifact, backend API, remote bootstrap, and runtime injection;
- Desktop unit, web build, Tauri Rust, DMG build/smoke, mounted E2E, visual and
  accessibility, upgrade/rollback, diagnostics, and privacy;
- docs/security, supply chain, release workflow inventory, release asset
  validation, and final candidate consistency.

## Benchmark Performance Gate

The benchmark performance gate protects only the already validated non-parametric
families for External Beta: textual memory, trajectory-to-skill, and
agent-system evolution. The gate uses pass@1 rescue counts on the frozen
baseline-failed Terminal Bench subsets and must rerun with the final Core
artifact for the release candidate. Benchmark automation remains outside Core and Desktop; it
may call Core APIs but must not ship as an ordinary-user product surface.

## Desktop

```bash
cd desktop
npm ci
npm audit --audit-level=high
npm test -- --run
npm run build:openevo
cd src-tauri
# Ubuntu/Linux CI runners need Tauri native packages such as
# libwebkit2gtk-4.1-dev, libayatana-appindicator3-dev, libgtk-3-dev,
# libxdo-dev, librsvg2-dev, and patchelf.
cargo metadata --locked --format-version 1
cargo test --locked
```

## Desktop Smoke

Desktop smoke must exercise the packaged app, not only the Vite web surface. The
minimum release smoke covers unsigned DMG creation, checksum verification,
mounted app launch, local state creation, remote profile setup, remote bootstrap,
Codex subscription transcript mode, self-deployed reference mode, run monitor,
artifact inspection, diagnostics export, deletion/cleanup, and upgrade/rollback
state migration. Fake transports may be used in CI only when the evidence is
clearly marked as a non-release substitute; the release candidate needs real
canary evidence for the supported release modes.

## Release Identity

```bash
python scripts/ci/audit_openevo_identity.py
python scripts/ci/check_openevo_release.py --wheel dist/*.whl
python scripts/ci/write_sha256.py dist/*.whl
git diff --check
```
