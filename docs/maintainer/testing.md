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
  tests/runtime \
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

`tests/runtime` is part of the deterministic Core PR gate. Its Docker CLI unit
tests use controlled fakes; the real-daemon ownership probes remain a separate
candidate gate so a contributor machine or ordinary PR runner without Docker
does not fail merely because the daemon/image is unavailable.

For a release candidate, manually dispatch **OpenEvo Core Backend checks** with
`require_real_docker=true`. The `real-docker-runtime-probes` job first requires
`docker info` and pulls `python:3.12-slim-bookworm`, then runs with
`OPENEVO_REQUIRE_REAL_DOCKER=1`. Missing Docker, a missing image, or either
name-collision/cancel ownership probe is a hard failure rather than a pytest
skip. Record the job URL against the exact candidate commit.

The focused productization regression boundary is indexed in
`docs/architecture/protected-behavior.md`. It protects observable method and
cross-stage behavior without a source-hash manifest; it is not a substitute for
the final benchmark performance gates.

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
source .venv/bin/activate
cd desktop
npm ci
npm audit --audit-level=high
npm test -- --run
npm run typecheck
npm run build:openevo
python packaging/build_sidecar.py --core-wheel-output-dir ../.openevo-release-inputs
# The output directory is a closed pair: one openevo-*.whl plus framework-lock.json.
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

Remote capability discovery has an additional artifact-level gate. In a clean
environment containing the exact Core wheel, run
`scripts/ci/smoke_openevo_remote_capabilities.py --wheel <exact-core-wheel>
--framework-lock <build-generated-framework-lock> --sidecar <packaged-sidecar>`.
The lock must be the exact sibling artifact emitted by the sidecar build, not a
runtime-generated substitute. The smoke must start Core with that external lock
through `openevo-core-service`, exercise the packaged sidecar through its native
inherited-listener/credential-frame launch contract, and use a full 40-character
`--source-commit` matching the release checkout. This CI-only smoke may replace
and stop the current user's canonical Core service, so run it only on a
disposable release worker. Its cleanup attempts to stop that service even when
`ensure`, attachment consumption, or capability verification fails. It
validates the two packaged process boundaries;
the separate remote-profile/SSH/active-project gate validates their production
forwarding composition. A source `TestClient` or local fake capability catalog
does not satisfy either gate.

The exact Core wheel/lock export keeps the output directory open for the whole
sidecar build and verifies its pathname/inode binding before and after writes.
Before creating the output, it holds and validates the immediate parent's owner,
group/world write bits, and macOS ACL. A newly created child may clear a valid
inherited ACL once; an ACL added to an existing output, transaction, marker, or
member must be rejected without mutation.
It stages under a private transaction directory, publishes without replacement,
keeps every source/export descriptor open, and exits the output context last so
a `TemporaryDirectory` cleanup failure rolls the pair back. Before success, the
test gate requires an exact wheel/lock root inventory and rechecks each pathname,
inode/device, regular-file type, link count, owner/mode, byte size, and SHA-256
against the source and canonical lock.
The generated and embedded lock must load through Core's authoritative
`FrameworkDistributionLock` loader, bind the exact wheel filename/version/digest,
and be the only non-wheel member below PyInstaller's `openevo/wheels` directory.
The wheel build fixes `SOURCE_DATE_EPOCH` to the trusted candidate commit time;
reproducibility tests must build twice from the same source and compare exact
wheel and generated lock bytes so a post-commit retry can recognize the pair.

Failure injection covers partial copy, second-member failure, output-path
replacement, exported-member unlink/rename/same-name replacement, and extra
members. A real child-process crash after the wheel name is published but before
the lock name is published must leave a bounded marker-authorized state that the
next run reconciles and replaces with one complete pair. Recovery and rollback
move the canonical marker to a monotonic `cleaning` phase before unlinking any
member. Its identity prefix records every cleanup-owned inode and its
`cleanup_index` durably authorizes only an ordered prefix to have zero remaining
links. Each progress update is file-fsynced, directory-fsynced, atomically
installed through `transaction.ready`, and directory-fsynced again before the
authorized unlink. Restart may adopt that temporary marker only when both files
are closed, canonical, identity-bound markers and they describe the exact next
phase or cleanup index.
Marker replacement tests also interrupt after the prior marker has moved to its
inode-named retired entry and race the source pathname after identity validation.
Both the checked inode and a same-name replacement must remain preserved; marker
publication never uses an overwrite-capable rename.

An unauthorized member must retain at least one canonical binding. An authorized
member may be absent or may still have names after an interrupted unlink, but
every remaining name must resolve no-follow to the recorded inode and must still
pass owner, mode, aggregate link-count, byte-size, and SHA-256 checks. Rollback
may remove only an inode it recorded; its held descriptor is the only additional
proof allowed to authorize an already-unlinked member or a bounded same-inode
rename. If a pathname contains an identity-mismatched replacement or an owned
inode has an unbound residual link, cleanup reports an unverifiable rollback,
preserves the unknown entry and transaction marker, and subsequent retries remain
fail closed until a maintainer resolves that output directory. Real subprocess
tests interrupt initial publication, interrupt the following recovery after
wheel or lock cleanup, and require a third process to finish; the same wheel/lock
cleanup points are exercised through live rollback.
The same preservation rule applies to a `preparing` transaction containing a
staged entry whose inode has not yet reached the ready marker; only empty and
marker-only bootstrap remnants are automatically reclaimed before readiness.

## Release Identity

```bash
python scripts/ci/audit_openevo_identity.py
python scripts/ci/check_openevo_release.py --wheel dist/*.whl
python scripts/ci/write_sha256.py dist/*.whl
git diff --check
```
