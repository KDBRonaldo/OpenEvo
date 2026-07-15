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
transferred wheel and lock. The macOS job owns candidate sidecar packaging and runs a direct probe
on APFS through the held object FD, `FSPathMakeRef`/`FSRef`, and
`FSUnlinkObject` implementation; it does not call the Linux-only Core service
lifecycle. Linux focused tests exercise the unsupported-platform fail-closed
behavior and their explicit conditional-removal testkit, but cannot establish
that the Carbon/APFS primitive works. That final boundary remains pending until
the `macos-14` job passes.

The sidecar process smoke loads `desktop/release-contract.json`, validates the
closed `VersionV1` and `DesktopStateV1` models, requires the frozen OpenAPI digest
and every renderer-required feature flag, and checks that state negotiation binds
the same digest. The renderer imports that JSON directly; anti-drift tests bind
the Rust native-host digest to it.

The exact Core wheel/lock export keeps the output directory open for the whole
sidecar build and verifies its pathname/inode binding before and after writes.
Before creating the output, it holds and validates the immediate parent's owner,
group/world write bits, and macOS ACL. A newly created child may clear a valid
inherited ACL once; an ACL added to an existing output, transaction, marker, or
member must be rejected without mutation.
Darwin reports an absent extended ACL as `acl_get_fd_np` returning null with
`ENOENT`; that exact result is normalized to an empty ACL inventory. Every other
lookup error, and every unknown, unreadable, or mutating ACL entry, fails closed.
After opening and validating the child, but before its first inventory or any
recovery, the builder takes a non-blocking exclusive `flock` on that same held
output-directory descriptor. Contention fails closed with an explicit
active-builder error. The lock remains held until the complete output context,
including commit or rollback and all transaction/source descriptor cleanup, has
exited. A paused real subprocess test proves a second builder cannot classify or
recover the first builder's live transaction.
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
move the canonical marker to a monotonic `cleaning` phase before removing any
member. Its identity prefix records every cleanup-owned inode and its
`cleanup_index` durably authorizes only an ordered prefix to have zero remaining
links. Each progress update is file-fsynced, directory-fsynced, atomically
installed through `transaction.ready`, and directory-fsynced again before the
identity-bound removal. Restart may adopt that temporary marker only when both
files are closed, canonical, identity-bound markers and they describe the exact
next phase or cleanup index.
Marker replacement tests also interrupt after the prior marker has moved to its
inode-named retired entry and race the source pathname after identity validation.
Both the checked inode and a same-name replacement must remain preserved; marker
publication never uses an overwrite-capable rename.

An unauthorized member must retain at least one canonical binding. An authorized
member may be absent or may still have names after an interrupted removal, but
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

After root members are quarantined, cleanup prepares to move the held transaction
inode out of the output. Before that rename, the builder file-fsyncs a canonical
receipt that binds the held parent/output, exact wheel/lock inputs, and cleanup
inode; directory-fsyncs its candidate; publishes it no-replace under a name that
also binds the receipt inode; and directory-fsyncs again. Only then does the held
transaction inode move by atomic no-replace rename to one deterministic sibling
tombstone bound to the output device/inode. Cleanup moves each authorized entry
to an identity-named quarantine, clears member payloads through the held descriptor,
and removes the transaction marker last. The empty held tombstone is then moved
no-replace to one deterministic purge name and checked against the receipt.
For final member, marker, directory, and receipt removal, macOS prepares an opaque
`FSRef` from the held FD before entering the syscall boundary and calls
`FSUnlinkObject`; the final call does not receive the mutable cleanup name.
Unsupported platforms and rejected filesystems preserve the object and fail
closed. Linux unit and subprocess tests install a test-only conditional-removal
model so portable transaction and crash cases remain executable; macOS CI uses
the production primitive.

Recovery accepts at most one receipt and one of those two exact sibling states; it
must not adopt the inode currently found at a known name. Real `os._exit` tests at
the empty-tombstone and purge windows rename the authorized directory, install a
different empty inode at the expected name, and require restart to preserve both
objects and fail explicitly. A double-snapshot no-follow parent identity scan is
capped at 4096 entries and detects the renamed authorized inode; exceeding that
budget also fails closed. Additional repeated crashes cover receipt candidate,
publication while the transaction remains marker-authorized in the output,
directory removal, and receipt quarantine. Twenty successful exports prove that
normal and recovered operation leaves no receipt/sibling state or wheel/lock byte
growth. Candidate builds have the same zero-residue requirement.
Three syscall-boundary races run after the native removal token is prepared and
immediately before execution: one replaces a tombstone regular member, one
replaces the empty purge directory, and one replaces the cleanup receipt. Every
replacement must remain present and the export must fail explicitly. On macOS
these cases exercise the real `FSRef` operation; on a platform without it, the
test-only conditional model refuses the mismatched binding before any delete.

## Release Identity

```bash
python scripts/ci/audit_openevo_identity.py
python scripts/ci/check_openevo_release.py --wheel dist/*.whl
python scripts/ci/write_sha256.py dist/*.whl
git diff --check
```
