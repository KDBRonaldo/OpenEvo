# OpenEvo External Beta Release Process

Canonical release requirements are defined in
`docs/maintainer/productization/spec.md`. This guide records the practical
release procedure. It will evolve with the implementation, but it must never
weaken the canonical release gates.

## Current Status

Final publication is disabled while productization work tracked by #131/#163 is
in progress. Do not publish a draft, create a final `v*` tag, or upload to PyPI
from the disabled placeholder workflows.

Maintainers can manually dispatch `OpenEvo Desktop unsigned draft prerelease`
from one reviewed `stable` commit. The workflow builds only its macOS runner
architecture, launches the real Tauri app before and after DMG copy, verifies
the Tauri executable and sidecar Mach-O slices in both copies with `file` and
`lipo`, verifies the same final Core wheel on Linux, and creates an unsigned
draft prerelease.
It uploads all assets, downloads them into a clean directory, and validates the
exact closed manifest before leaving the draft for review. Passing this
packaging rehearsal does not satisfy the science E2E, benchmark,
secret-canary/privacy, signing, notarization, or final publication gates.

PyPI is not part of the unsigned External Beta. The ordinary-user artifact is
the macOS Desktop DMG; Desktop installs the descriptor-matched Core artifact on
the remote server.

## Required Outputs

- one OpenEvo Desktop DMG for the architecture actually built and declared by
  the runner; the current workflow does not claim a universal build;
- DMG SHA256 checksum;
- exact Core install artifact and SHA256 checksum;
- Core descriptor containing version, compatibility, source commit, artifact
  name, and checksum; the current closed compatibility is Python `>=3.11` on
  `linux-x86_64`;
- candidate manifest and canonical checksum inventory binding the DMG, exact
  Core wheel, framework lock, Core descriptor, source commit, architecture,
  native smoke evidence, and supply-chain reports;
- release notes;
- dependency lock, practical vulnerability, and license results for shipped
  Python, npm, and Rust dependencies, including the exact third-party-only
  Python requirements input audited by `pip-audit`;
- benchmark summaries for textual memory, trajectory-to-skill, and
  agent-system gates.

## Candidate Preparation

1. Select one candidate commit from `stable` after all productization PRs are
   merged.
2. Run protected algorithm/source-boundary tests.
3. Run the three independent Terminal Bench performance gates.
4. Build and clean-install the Core artifact.
5. Run Core integration tests for Codex subscription transcript and the
   self-deployed reference profile.
6. Build the Desktop app and run source-level tests before packaging.
7. Build the DMG and rerun the packaged-app lifecycle and science workflow
   smoke against the exact Core descriptor/artifact.
8. Run secret-canary, diagnostics redaction, privacy, identity, docs/link, and
   dependency checks.

The exact Core wheel export runs only on a GitHub-hosted ephemeral runner or an
equivalently controlled one-shot build account. Its requested output path must
not exist. The builder verifies the generated wheel and canonical
`framework-lock.json`, writes the exact pair into a private random sibling
staging directory, fsyncs and revalidates both files, and atomically publishes
the complete directory with no-replace semantics. It never adopts, overwrites,
or automatically deletes stale output. A failed job publishes no manifest or
artifact; a local maintainer must use a new output path and inspect any
non-authoritative staging residue before removing it.

The generated target-triple sidecar uses a separate same-directory atomic
replacement. The builder verifies and fsyncs a random staging file before
replacing the Tauri externalBin target. A failure before replacement keeps the
previous target intact; any retained staging file is non-authoritative and must
be inspected before removal.

The pull-request release smoke keeps platform responsibilities separate. Its
macOS packaging job builds the wheel/lock pair once, verifies a two-member
SHA-256 manifest, and uploads the exact inputs under an artifact name bound to
the source commit, workflow run, and run attempt. The Linux Core job depends on
that producer, verifies the downloaded manifest against the digest passed
through the job output, rechecks both member digests, and only then installs the
wheel and runs `openevo-core-service`. It must not rebuild the wheel or lock, and it rechecks
those final candidate bytes after the service smoke. Conversely, the macOS
packaging job must not run the Linux-only Core service lifecycle.

The manual candidate uses the same producer/consumer rule for the complete
release inventory. `release-candidate.json` and `core-install-artifact.json`
bind the exact Core wheel and framework lock; the former also binds the DMG,
`SHA256SUMS`, commit, runner architecture, app/DMG native evidence, and Python,
npm, and Cargo dependency/license/security summaries. Those summaries are
checked against the candidate checkout's four lock/license files. The Python
export uses `uv export --no-emit-project`; the collector requires the
`pip-audit` package/version set to equal every applicable exact requirement,
rejects OpenEvo itself, and records the requirements digest and audited package
count. `cargo-audit` is pinned to `0.22.2` so the current RustSec CVSS 4.0 data
is parseable; malformed or incomplete JSON still fails in the collector and no
advisory is ignored. The final draft job alone receives `contents: write`;
build and Linux verification retain read-only permissions.

The dependency and security summaries, native smoke evidence, Core descriptor,
and release candidate manifest use schema version 2 for these closed contracts.
The unchanged license inventory remains version 1; `framework-lock.json`
retains its independent string-valued version 1 contract.

Each native smoke records closed `mach_o` evidence for the Tauri executable and
packaged sidecar: bounded `file -b` output plus the sorted `lipo -archs` slice
list. Candidate creation requires raw-app and DMG-copy observations to match
and requires both binaries to contain exactly the slice represented by the
runner architecture. `release-candidate.json` repeats those normalized slice
lists under `macos.native_architectures`, while the file inventory binds both
complete smoke reports by size and SHA256.

The portable app-smoke unit fixture uses an emitted evidence record because its
executable is a test script, not Mach-O. It never substitutes for candidate
evidence: macOS candidate runs always inspect and launch the real Tauri binary
and packaged sidecar with `file`, `lipo`, native window, and FD checks.
Any product or benchmark failure creates a new candidate after the fix.
Infrastructure-only retries must be recorded and may not be used to select the
best stochastic result.

## Draft Release Validation

The manual workflow creates a uniquely tagged GitHub draft prerelease only after
its macOS and Linux candidate jobs succeed. Cross-job Actions artifact names
bind source commit, workflow run, and run attempt so a full rerun cannot collide
with immutable v4 artifacts from an earlier attempt. It uploads the required
outputs, downloads every asset into a clean directory, and verifies:

- asset names and architectures are expected;
- SHA256 files match downloaded bytes;
- the Core descriptor references the uploaded Core artifact;
- the DMG version and bundled/fetched descriptor match the candidate commit;
- release notes exactly match the release-tool-owned canonical packaging
  document. The GitHub body adds only a release-tool-generated, 128-bit random
  ownership marker used by failure cleanup. The document states
  unsigned/not-notarized status, available and unavailable execution
  modes, known limitations, `0 of 3` benchmark gates with all three rescue
  counts `pending`, privacy/security behavior, and install/upgrade/uninstall
  retention for `~/.openevo/desktop`, the Tauri native host app-data directory
  for `org.openevo.desktop`, and remote data;
- the GitHub draft title, tag, target commit, body, draft state, and prerelease
  state match the candidate at the discrete API read immediately after asset
  redownload; this is not an atomic assertion about later workflow completion;
- no unclassified development, secret, benchmark-private, or source-checkout
  files are present.

Before creation, the workflow validates the exact candidate tag name as a Git
ref and uses the authenticated, paginated release inventory so private drafts
are included when requiring both a same-name release and remote Git tag to be
absent. If creation, upload, redownload, metadata validation, or
verification-record upload fails or is cancelled, an `always()` cleanup deletes
only a draft whose complete metadata and random ownership marker still match
this workflow attempt. It does
not delete Git tags, and it fails unless both the owned draft and any same-name
remote Git tag are absent afterward. A successful run leaves the draft for
review but proves that no real Git tag exists; the draft's `tagName` is release
metadata only. It does not publish the release.

A GitHub draft remains administratively mutable after the workflow. The
workflow provides point-in-time verification, not an immutable GitHub object.
Any later edit, asset replacement, or tag movement invalidates the candidate;
delete it and run a new candidate rather than attempting to repair or promote
the edited draft.

Two fresh-context `gpt-5.6-sol` high-effort reviews must approve product/spec
compliance and release risk before a candidate reaches `stable`.

## Publication

Final External Beta publication remains disabled. The packaging-only draft from
this workflow is review evidence only and must not be edited, retagged, or
promoted. After the science, benchmark, privacy, signing-policy, and final
product gates are implemented and
pass, create a new final candidate from a reviewed `stable` commit. That future
path must generate final release notes and a new manifest/checksum inventory,
roundtrip every asset again, and publish only those unchanged revalidated bytes.

## Rollback

Before publication, close the failed draft and open a corrective issue. After
publication, mark a broken release clearly, preserve evidence needed to explain
the failure, and publish a corrected version rather than replacing bytes under
the same tag. User-facing rollback is manual installation of the most recent
compatible DMG/Core pair; document any irreversible state migration.
