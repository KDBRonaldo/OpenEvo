# OpenEvo v0.1.10 Verification And Public Preview Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze, validate, install, exercise, and publicly publish one immutable OpenEvo Desktop v0.1.10 Preview whose exact macOS candidate fixes long-operation timeout/idempotency and passes the real remote two-Task successor/context path.

**Architecture:** Version and release identities bind the new Desktop OpenAPI/event digests and lifecycle features while Core v2 authority remains unchanged unless its generated contract changes. Local gates precede two required fresh-context reviews. The reviewed branch merges to `stable`; the stable-only candidate workflow creates an unsigned DMG with an ad-hoc-signed app and immutable draft assets. Exact downloaded candidate bytes are installed under `/Applications`, generate OpenSSH-signed real-science evidence, and are published only through the protected Preview workflow.

**Tech Stack:** Python/pytest, Node/Vitest/Playwright, Rust/cargo, Tauri, GitHub Actions/`gh`, macOS LaunchServices, system OpenSSH, signed JSON evidence.

---

## Release policy fixed for this plan

- This is a public immutable **Preview**, because the canonical product spec still disables final External Beta publication.
- The DMG is unsigned and non-notarized. The contained app is ad-hoc signed exactly as required by the current release contract.
- v0.1.9 assets, release metadata, and Git tag remain untouched.
- Candidate failure creates a new candidate; no draft asset is replaced.
- After candidate creation, the only allowed source delta before publication is the exact evidence JSON and `.sig` under that candidate tag.
- The configured system OpenSSH alias is the connection authority. The real run does not substitute IP/username inputs.

## Task 1: Bind v0.1.10 version, contract, and feature identity

**Files:**

- Modify: `pyproject.toml`
- Modify: `src/openevo/__init__.py`
- Modify: `desktop/package.json`
- Regenerate: `desktop/package-lock.json`
- Modify: `desktop/src-tauri/Cargo.toml`
- Regenerate: `desktop/src-tauri/Cargo.lock`
- Modify: `desktop/src-tauri/tauri.conf.json`
- Modify: `desktop/release-contract.json`
- Modify: `desktop/src/product/releaseContract.ts`
- Modify: `desktop/sidecar/release_capabilities.py`
- Modify: `desktop/sidecar/release_provider_v2.py`
- Modify: `desktop/sidecar/release_app.py`
- Modify: `desktop/sidecar/release_runtime.py`
- Modify: `desktop/sidecar/core_client_v2.py`
- Modify: `tests/ci/test_check_openevo_release.py`
- Modify: `tests/openevo/sidecar/test_release_local_api_v2.py`

- [ ] First update release tests to require `0.1.10`, a `v0110` closed policy, current generated Desktop digests, unchanged exact Core v2 digests if generation confirms no Core contract change, and required features `lifecycle_operations_v2`, `lifecycle_process_logs_v2`, and `mutation_idempotency_v2`.
- [ ] Run the tests and observe version/policy failures:

```bash
uv run pytest -q \
  tests/ci/test_check_openevo_release.py \
  tests/openevo/sidecar/test_release_local_api_v2.py \
  -k 'version or release or feature or contract'
```

- [ ] Set all package-owned version fields to `0.1.10`. Regenerate only the root Desktop package lock entry and the OpenEvo Rust package entry through the package managers:

```bash
cd desktop
npm install --package-lock-only --ignore-scripts
cd src-tauri
cargo check --locked
```

If `cargo check --locked` reports that the lock needs an update, run `cargo check`, inspect that only the OpenEvo root package lock identity changed, then rerun `cargo check --locked`.

- [ ] Rename release-specific policy symbols from `V019_*` to `V0110_*` and remove executable v0.1.9-only acceptance paths. Historical prose/comments describing already persisted v0.1.9 data remain unchanged.
- [ ] Replace the release-contract `v019` object with `v0110`, bind `release_version: "0.1.10"`, insert canonical Desktop digests, and append the three lifecycle features in sorted order. Do not accept both v0.1.9 and v0.1.10 Desktop contract digests.
- [ ] Recalculate `feature_set_sha256` through existing canonical feature serialization; update tests/fixtures rather than weakening digest checks.
- [ ] Run identity tests and audit version drift:

```bash
uv run pytest -q tests/ci/test_check_openevo_release.py tests/openevo/sidecar/test_release_local_api_v2.py
uv run python scripts/ci/audit_openevo_identity.py
rg -n '0\.1\.9|v019|V019' \
  pyproject.toml src/openevo/__init__.py desktop/package.json desktop/package-lock.json \
  desktop/src-tauri/Cargo.toml desktop/src-tauri/Cargo.lock desktop/src-tauri/tauri.conf.json \
  desktop/release-contract.json desktop/src/product/releaseContract.ts \
  desktop/sidecar/release_capabilities.py desktop/sidecar/release_provider_v2.py \
  desktop/sidecar/release_app.py desktop/sidecar/release_runtime.py desktop/sidecar/core_client_v2.py
```

Expected: no active v0.1.9 release identity in the listed files; intentionally historical compatibility comments must be reviewed individually.

- [ ] Commit and push:

```bash
git add pyproject.toml src/openevo/__init__.py desktop/package.json desktop/package-lock.json \
  desktop/src-tauri/Cargo.toml desktop/src-tauri/Cargo.lock desktop/src-tauri/tauri.conf.json \
  desktop/release-contract.json desktop/src/product/releaseContract.ts desktop/sidecar \
  tests/ci/test_check_openevo_release.py tests/openevo/sidecar/test_release_local_api_v2.py
git commit -m "chore(release): bind OpenEvo v0.1.10 identity"
git push
```

## Task 2: Extend candidate and real-science evidence contracts

**Files:**

- Modify: `scripts/ci/openevo_release_candidate.py`
- Modify: `scripts/e2e/desktop_real_science_e2e.py`
- Modify: `scripts/ci/desktop_real_science_e2e_attestation.py`
- Modify: `scripts/ci/validate_desktop_real_science_e2e.py`
- Modify: `tests/ci/test_openevo_release_candidate.py`
- Modify: `tests/ci/test_desktop_real_science_e2e.py`
- Modify: `tests/ci/test_validate_desktop_real_science_e2e.py`
- Modify: `.github/workflows/openevo-desktop-candidate.yml`

- [ ] Add failing validator tests for evidence that lacks lifecycle reservation latency, total duration, ordered phases, real child-log source/digest, SSE reconnect, relaunch recovery, stable action ID, one Core project, one applied bridge mutation, and secret-canary absence.
- [ ] Add failing tests that reject duration at or below 15 seconds, reservation at or above the renderer deadline, fewer than two phases, synthetic desktop-only logs, duplicate project IDs, duplicate applied mutation rows, and a different operation after relaunch.
- [ ] Run red tests:

```bash
uv run pytest -q \
  tests/ci/test_openevo_release_candidate.py \
  tests/ci/test_desktop_real_science_e2e.py \
  tests/ci/test_validate_desktop_real_science_e2e.py
```

- [ ] Increment the release-candidate schema from 9 to 10 and real-science evidence schema from 2 to 3. Bind the new Desktop contract digests/features and an exact lifecycle-evidence section; do not make old evidence valid for v0.1.10.
- [ ] Extend the runner to create a cold project lifecycle through the packaged v2 Local API, time the 202 reservation separately from terminal duration, collect at least two authoritative phases, fetch actual `ssh_*`/`daemon_*` log entries, force one SSE reconnect, terminate/restart the exact packaged Desktop sidecar/native composition at the supported recovery boundary, and continue observation without issuing another create mutation.
- [ ] Query the provider/Core bridge stores through their existing bounded evidence helpers after shutdown and record exactly one project/mapping and one applied `create_project_v2` mutation for the action ID. Do not include host paths, commands, env, token values, or raw credentials in evidence.
- [ ] Preserve the existing two completed Tasks, generation-0 → generation-1 → generation-2 adjacent Project Head chain, three target outputs per successor, and Task-2 Runtime Context reuse checks.
- [ ] Add a generated secret canary to the test process/remote env allowed only for detection. Fail before evidence write if it occurs in any lifecycle page, screenshot text extraction, support log, or evidence string.
- [ ] Update the candidate workflow to validate the new schemas and include the lifecycle regression in the candidate-bound acceptance input inventory.
- [ ] Run the evidence tests and structural check:

```bash
uv run pytest -q \
  tests/ci/test_openevo_release_candidate.py \
  tests/ci/test_desktop_real_science_e2e.py \
  tests/ci/test_validate_desktop_real_science_e2e.py
uv run python scripts/e2e/desktop_real_science_e2e.py --structural-check
```

- [ ] Commit and push:

```bash
git add scripts/ci/openevo_release_candidate.py scripts/e2e/desktop_real_science_e2e.py \
  scripts/ci/desktop_real_science_e2e_attestation.py \
  scripts/ci/validate_desktop_real_science_e2e.py tests/ci \
  .github/workflows/openevo-desktop-candidate.yml
git commit -m "test(release): require lifecycle regression evidence"
git push
```

## Task 3: Update packaged smoke, release checks, and support docs

**Files:**

- Modify: `scripts/ci/smoke_openevo_desktop_sidecar.py`
- Modify: `scripts/ci/smoke_openevo_desktop_launchservices.py`
- Modify: `scripts/ci/check_openevo_release.py`
- Modify: `tests/ci/test_smoke_openevo_desktop_sidecar.py`
- Modify: `tests/ci/test_smoke_openevo_desktop_launchservices.py`
- Modify: `docs/maintainer/testing.md`
- Modify: `docs/maintainer/release-process.md`
- Modify: `docs/maintainer/desktop-real-science-e2e.md`
- Modify: `docs/maintainer/macos-desktop-development-handoff.md`
- Modify: `scripts/ci/openevo_release_candidate.py`

- [ ] Add failing smoke tests requiring v0.1.10 feature/digest negotiation, provider schema-v3 migration from retained v0.1.9 state, operation rediscovery after restart, and absence of timeout/double-create in the packaged composition.
- [ ] Run red tests:

```bash
uv run pytest -q \
  tests/ci/test_smoke_openevo_desktop_sidecar.py \
  tests/ci/test_smoke_openevo_desktop_launchservices.py \
  tests/ci/test_check_openevo_release.py
```

- [ ] Generalize active release-check functions from v0.1.9 naming to v0.1.10 and keep frozen historical evidence validators explicit. Require the exact new schema/digests/features without allowing legacy fallback.
- [ ] Extend sidecar/LaunchServices smoke to reserve an operation, observe phase/log authority, quit/relaunch, and confirm exact replay. Use deterministic injected worker output for smoke only; real-process log proof remains in real-science evidence.
- [ ] Update maintainer instructions and user-facing release notes with: fixed project-create timeout, durable exact retry, lifecycle progress/logs, actual sanitized SSH/Daemon output, unsupported command/env/secret exposure, system-OpenSSH aliases, unsigned-app install procedure, supported OS/server matrix, and known Preview boundaries.
- [ ] Explicitly state that existing duplicate v0.1.9 projects are preserved and may be manually ignored; migration does not delete them.
- [ ] Run the focused tests and release audit:

```bash
uv run pytest -q \
  tests/ci/test_smoke_openevo_desktop_sidecar.py \
  tests/ci/test_smoke_openevo_desktop_launchservices.py \
  tests/ci/test_check_openevo_release.py
uv run python scripts/ci/check_openevo_release.py
```

- [ ] Commit and push:

```bash
git add scripts/ci/smoke_openevo_desktop_sidecar.py \
  scripts/ci/smoke_openevo_desktop_launchservices.py scripts/ci/check_openevo_release.py \
  tests/ci/test_smoke_openevo_desktop_sidecar.py \
  tests/ci/test_smoke_openevo_desktop_launchservices.py \
  tests/ci/test_check_openevo_release.py docs/maintainer
git commit -m "docs(release): prepare v0.1.10 Preview acceptance"
git push
```

## Task 4: Run clean local verification

**Files:**

- Modify only files required by failures proven during this task; every fix starts with a failing regression test and a separate focused commit.

- [ ] Confirm the worktree is clean and synchronized:

```bash
git fetch origin
git status --short --branch
git log --oneline --decorate -5
git rev-list --left-right --count origin/stable...HEAD
```

- [ ] Run Python contract/sidecar/release gates:

```bash
uv sync --frozen --group dev
uv run pytest -q \
  tests/openevo/sidecar \
  tests/openevo/desktop/test_app.py \
  tests/ci/test_build_sidecar.py \
  tests/ci/test_check_openevo_release.py \
  tests/ci/test_openevo_release_candidate.py \
  tests/ci/test_desktop_real_science_e2e.py \
  tests/ci/test_validate_desktop_real_science_e2e.py \
  tests/ci/test_smoke_openevo_desktop_sidecar.py \
  tests/ci/test_smoke_openevo_desktop_launchservices.py
```

- [ ] Run renderer/browser gates:

```bash
cd desktop
npm ci
npx playwright install chromium
npm test -- --run
npm run typecheck
npm run build:openevo
npm run test:product-browser
```

- [ ] Run native gates:

```bash
cd desktop/src-tauri
cargo fmt --check
cargo test --locked --release -- --test-threads=1
cargo clippy --locked --release --all-targets -- -D warnings
```

- [ ] Run identity/security/static gates from repository root:

```bash
uv run python scripts/ci/audit_openevo_identity.py
uv run python scripts/ci/check_openevo_release.py
git diff --check origin/stable...HEAD
```

- [ ] Build the local development DMG and perform packaged startup only; do not treat it as remote-release evidence:

```bash
cd desktop
npm run build:sidecar
npm run tauri:build -- --ci
find src-tauri/target/release/bundle/dmg -maxdepth 1 -type f -name 'OpenEvo-Desktop-0.1.10-*.dmg' -print
```

- [ ] Use the `computer-use` skill to install the local app through Finder/LaunchServices, open it, verify startup with retained state, Add remote workspace, operation panel/log rendering, quit/relaunch, and clean shutdown. Move the prior `/Applications/OpenEvo Desktop.app` to a uniquely named owner-controlled backup or Trash before copying; report the action.
- [ ] If any gate fails, use `superpowers:systematic-debugging`, add the smallest failing regression, fix, rerun the focused test and then this task's complete gate, commit, and push.
- [ ] Record the exact passing commands/commit in PR #221, then push the clean head.

## Task 5: Obtain the two required fresh-context reviews and merge to stable

**Files:**

- Modify implementation/tests/docs only for findings accepted after technical verification.

- [ ] Use `superpowers:requesting-code-review` for one fresh-context product/spec review and one independent release-risk review. Both reviewers must use `gpt-5.6-terra` at high effort as required by repository release policy and the user's instruction.
- [ ] Give each reviewer the exact diff `origin/stable...HEAD`, design doc, all three implementation plans, Issue #220, PR #221, and fresh verification outputs. Neither review may inherit the other's conclusions.
- [ ] Triage every finding with `superpowers:receiving-code-review`; reproduce technical claims before changing code. Add a failing test for each accepted behavior defect.
- [ ] Rerun Task 4 after review fixes, commit each coherent correction, and push.
- [ ] Mark PR #221 ready, ensure all required GitHub checks pass, and inspect unresolved review threads.
- [ ] If GitHub Actions fails, use `github:gh-fix-ci` to inspect authoritative logs before changing code.
- [ ] Merge PR #221 into `stable` only after both reviews and all checks pass. Fetch and verify the exact remote result:

```bash
git fetch origin
git rev-parse origin/stable
git merge-base --is-ancestor origin/feat/v010-lifecycle-progress origin/stable
git status --short --branch
```

- [ ] Do not make any non-evidence change to `stable` after the candidate source commit is frozen.

## Task 6: Build the stable-only immutable draft candidate

**Files:** None unless the candidate fails; a failure returns to implementation and creates a new stable commit/candidate.

- [ ] Dispatch the stable workflow:

```bash
gh workflow run openevo-desktop-candidate.yml \
  --ref stable \
  -f candidate_label=v0110
```

- [ ] Resolve the exact run without guessing by querying the newest workflow-dispatch run on stable whose head SHA equals `origin/stable`, then watch it:

```bash
stable_sha="$(git rev-parse origin/stable)"
candidate_run_id="$(gh run list --workflow openevo-desktop-candidate.yml --branch stable \
  --event workflow_dispatch --json databaseId,headSha,createdAt \
  --jq 'map(select(.headSha == "'"$stable_sha"'")) | sort_by(.createdAt) | last | .databaseId')"
test -n "$candidate_run_id"
gh run watch "$candidate_run_id" --exit-status
```

- [ ] Download all candidate artifacts into a new owner-private temporary directory created with `mktemp -d`; validate with the repository scripts. Read the candidate tag, release ID, run attempt, and manifest digest only from the workflow's validated snapshot/artifacts.
- [ ] Verify the GitHub release is still draft/prerelease, every asset ID/name/size/API digest matches, the real Git tag is absent, and the source SHA equals `stable_sha`.
- [ ] On failure, do not replace assets. Follow workflow cleanup or delete only the exactly validated owned draft through its numeric ID, fix on the feature branch with TDD, merge a new stable commit, and dispatch a new candidate.

## Task 7: Install and exercise the exact candidate on this Mac

**Files:**

- Add after successful execution: `release-evidence/$candidate_tag/desktop-real-science-e2e.json`
- Add after successful execution: `release-evidence/$candidate_tag/desktop-real-science-e2e.json.sig`

- [ ] Validate the DMG SHA-256 against `release-candidate.json` and `SHA256SUMS`, mount read-only with `hdiutil`, inspect the app/sidecar/askpass digests and ad-hoc signature, and copy the exact app to `/Applications` through Finder/LaunchServices semantics.
- [ ] Move the existing `/Applications/OpenEvo Desktop.app` to Trash or an owner-controlled timestamped backup first. Do not delete application data; retained-state migration is part of acceptance.
- [ ] Apply only the documented recursive quarantine-removal command for the unsigned Preview, then revalidate the complete app signature with `codesign --verify --deep --strict`.
- [ ] Use `computer-use` to launch the installed app and verify: no top-of-page startup error, retained state loads, Add remote workspace lists literal `~/.ssh/config` aliases, and the configured alias connects through system OpenSSH.
- [ ] Start a cold native-workspace/project lifecycle from Desktop. Confirm the 202 operation appears promptly, runs longer than 15 seconds, shows at least two real phases and SSH/Daemon log text, survives one SSE reconnect and app quit/relaunch, and reaches one project without a timeout banner or repeated create action.
- [ ] Run the exact candidate-bound automation using the installed app and downloaded candidate inputs. Supply the literal configured SSH alias, exact candidate manifest/smoke/web evidence, exact managed runtime, `gpt-5.3-codex-spark`, and high reasoning. Retain its owner-private temporary output until validation succeeds.
- [ ] Validate and OpenSSH-sign the evidence through `desktop_real_science_e2e_attestation.py`; then validate the evidence/signature against the candidate source public key and expected digests through `validate_desktop_real_science_e2e.py`.
- [ ] Inspect the evidence assertions: reservation under 15 seconds, terminal duration over 15 seconds, phases/log source, reconnect/relaunch, stable action/operation ID, exactly one Core project/mapping/applied mutation, two successful Tasks, adjacent head chain, three successor outputs, Task-2 context reuse, no secret canary, and complete ownership cleanup.
- [ ] Copy only the validated JSON/signature to the exact candidate-tag path. Commit these two additions directly on top of the candidate source and push `stable`:

```bash
git diff --name-status "$stable_sha"..HEAD
git add "release-evidence/$candidate_tag/desktop-real-science-e2e.json" \
  "release-evidence/$candidate_tag/desktop-real-science-e2e.json.sig"
git commit -m "test(release): attest v0.1.10 real Desktop workflow"
git push origin HEAD:stable
```

- [ ] Verify the complete delta from candidate source contains exactly those two added files and no modification/deletion.

## Task 8: Publish through the protected Preview controller

**Files:** None.

- [ ] Read all workflow inputs from validated candidate/evidence files: candidate tag, numeric release ID, candidate source SHA, candidate manifest SHA-256, evidence SHA-256, signature SHA-256, candidate workflow run ID, and run attempt.
- [ ] Dispatch the reviewed publisher from `stable`:

```bash
gh workflow run openevo-desktop-publish-preview.yml \
  --ref stable \
  -f candidate_tag="$candidate_tag" \
  -f expected_release_id="$release_id" \
  -f expected_source_sha="$stable_sha" \
  -f expected_release_candidate_manifest_sha256="$candidate_manifest_sha256" \
  -f expected_real_science_e2e_sha256="$real_e2e_sha256" \
  -f expected_real_science_e2e_signature_sha256="$real_e2e_signature_sha256" \
  -f candidate_workflow_run_id="$candidate_run_id" \
  -f candidate_workflow_run_attempt="$candidate_run_attempt" \
  -f confirmation=publish-preview
```

- [ ] Watch the exact publisher run to success. Protected-environment approval is the only expected user participation if GitHub requests it.
- [ ] Verify by GitHub REST/`gh`: release is public, immutable, prerelease, tag points exactly to `stable_sha`, title/body unchanged, asset IDs/names/sizes/digests unchanged, and v0.1.9 remains unchanged.
- [ ] Download the public DMG into a fresh owner-private directory and prove byte equality with the candidate DMG and manifest. Launch the already installed identical app once more and verify `/version` reports v0.1.10 and the exact source/digests.
- [ ] Record the public release URL, source SHA, candidate/publisher run URLs, DMG SHA-256, manifest SHA-256, evidence/signature SHA-256, and post-publication verification result in the final handoff.

## Release plan completion criteria

- [ ] Public immutable v0.1.10 Preview exists and v0.1.9 is untouched.
- [ ] Public DMG bytes equal the exact candidate bytes installed/tested on this Mac.
- [ ] Installed Desktop starts, connects through literal system-OpenSSH config, and shows lifecycle progress/actual sanitized logs.
- [ ] One cold project operation exceeds 15 seconds without a renderer timeout and creates exactly one Core project/mutation.
- [ ] Real two-Task successor/context evidence and signature validate against the candidate.
- [ ] All local, CI, review, candidate, publisher, and post-publication evidence is linked and digest-bound.
