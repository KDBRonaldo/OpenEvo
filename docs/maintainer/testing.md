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

The packaging-level native smoke is
`scripts/ci/smoke_openevo_desktop_bundle.py`. It reads
`CFBundleExecutable` from `Info.plist` and launches that exact
`Contents/MacOS` process; directly executing the bundled sidecar is not app
evidence. On macOS it requires a renderer window, the packaged sidecar listener
on inherited FD 3, executable FD 4 whose bytes match the bundled externalBin,
and disappearance of the captured app/sidecar process groups after main-app
termination. Candidate runs retain separate `app-bundle-smoke.json` and
`dmg-copy-smoke.json` outputs.

`scripts/ci/openevo_release_candidate.py` creates and validates the closed
candidate inventory. Validation includes the source commit, actual runner
architecture, final Core wheel/framework lock/registry identity, DMG,
`core-install-artifact.json`, canonical `SHA256SUMS`, release notes, both native
smokes, and dependency/license/security evidence. The Linux job and the
redownloaded draft run the same validator before using any Core bytes. A valid
packaging manifest is not evidence for the still-separate science, benchmark,
privacy, signing, or notarization gates.

Remote capability discovery has an additional artifact-level gate. In a clean
environment containing the exact Core wheel, run
`scripts/ci/smoke_openevo_remote_capabilities.py --wheel <exact-core-wheel>
--framework-lock <build-generated-framework-lock> --sidecar <packaged-sidecar>`.
The lock must be the exact sibling artifact emitted by the sidecar build, not a
runtime-generated substitute. The smoke must start Core with that external lock
through the installed Core supervisor API, exercise the packaged sidecar through its native
inherited-listener/credential-frame launch contract, and use a full 40-character
`--source-commit` matching the release checkout. This gate runs only on its
dedicated GitHub-hosted Linux worker because the supervisor derives its required
canonical host-global root from the OS account database, not from `HOME` or a
caller-selected path.
The smoke stops a process only through `stop_core_service_if_generation`, which
rechecks the attachment generation and release identity under the supervisor
locks before signalling or removing publication state. A failed ensure, an
attachment to an existing process, or a replaced generation has no cleanup
authority. It
validates the two packaged process boundaries;
the separate remote-profile/SSH/active-project gate validates their production
forwarding composition. A source `TestClient` or local fake capability catalog
does not satisfy either gate.

The pull-request release workflow splits this coverage across operating systems
without splitting artifact identity. `macos-packaging-smoke` is the only job
that builds the exact Core wheel and `framework-lock.json`; it writes and
verifies `SHA256SUMS`, exports the manifest digest, and uploads those three files
under a source-commit-qualified artifact name. `linux-core-smoke` has an
explicit dependency on that job, downloads the same artifact, verifies the
manifest digest and both payload digests before installation, then owns the
actual Core service ensure/attachment/capability/stop smoke. It never rebuilds
the release inputs or constructs a later outer wheel. Because a macOS Mach-O
sidecar cannot execute on the Linux Core runner, Linux rebuilds a packaged Linux
sidecar from the same checkout solely as the executable native-process fixture;
it is not candidate artifact evidence and does not replace the macOS packaged or
app-bundle smokes. The Linux remote smoke combines that fixture with the exact
transferred wheel and lock. The macOS job owns candidate sidecar packaging and
exact-pair publication on its GitHub-hosted ephemeral runner; it does not call
the Linux-only Core service lifecycle.

The sidecar process smoke loads `desktop/release-contract.json`, validates the
closed `VersionV1` and `DesktopStateV1` models, requires the frozen OpenAPI digest
and every renderer-required feature flag, and checks that state negotiation binds
the same digest. The renderer imports that JSON directly; anti-drift tests bind
the Rust native-host digest to it.

The exact Core wheel/lock export test contract is intentionally small. The
requested output must not exist, and existing directories or symlinks are
rejected without mutation. Tests verify that the builder opens stable private
regular-file inputs, validates the canonical lock against the wheel filename and
SHA-256, copies both files into a private random sibling directory, fsyncs and
revalidates the exact two-member inventory, and publishes the directory with an
atomic no-replace rename. An injected publish failure leaves the requested path
absent and preserves only a clearly non-authoritative random staging directory.
A successful publish contains exactly the verified wheel and lock; a second
publish fails without changing them.

Sidecar target publication has a separate fault-injection contract. Tests prove
that a verified staging file atomically replaces an existing externalBin with
mode `0755`, and that a replacement failure preserves the old target byte for
byte while leaving the new file only under a non-authoritative staging name.

The workflow guard requires a GitHub-hosted ephemeral runner. The build UID and
its processes are part of the release-build trust boundary; this publisher does
not claim protection from malicious same-UID code or provide restart recovery
for a persistent shared workspace. The Actions artifact manifest, candidate
manifest, Linux clean install, embedded-archive checks, and final draft-asset
roundtrip remain the durable cross-job identity evidence. Tests also retain the
reproducible wheel build, authoritative `FrameworkDistributionLock` loader,
raw PyInstaller TOC multiplicity, embedded byte equality, output-overlap, and
failure-before-artifact gates.

## Release Identity

```bash
python scripts/ci/audit_openevo_identity.py
python scripts/ci/check_openevo_release.py --wheel dist/*.whl
python scripts/ci/write_sha256.py dist/*.whl
git diff --check
```
