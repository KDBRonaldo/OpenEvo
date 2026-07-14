# OpenEvo External Beta Release Process

Canonical release requirements are defined in
`docs/maintainer/productization/spec.md`. This guide records the practical
release procedure. It will evolve with the implementation, but it must never
weaken the canonical release gates.

## Current Status

Automated publishing is disabled while productization work tracked by #131 is
in progress. Do not publish a GitHub Release, PyPI package, or `v*` tag from the
current placeholder workflows.

Maintainers can manually dispatch `OpenEvo Desktop unsigned candidate` to get a
14-day Actions artifact from a macOS runner. That workflow builds, mounts,
copies, smokes, checksums, and validates the candidate DMG and exact nested Core
wheel. Its packaged-app gate launches the native executable from both the build
bundle and the copied DMG, then requires the React renderer, Tauri IPC, and
managed sidecar to agree on the frozen Local API digest. It is for exhibition
and packaging rehearsal only; passing it does not satisfy the remote science
E2E, benchmark, security/privacy, or draft-release gates and does not authorize
publication. The native smoke accepts only a complete log line equal to the
frozen readiness marker, reads the log incrementally so later volume cannot
discard earlier evidence, and always TERM/KILLs and verifies the dedicated
native process group on success, failure, or timeout.

PyPI is not part of the unsigned External Beta. The ordinary-user artifact is
the macOS Desktop DMG; Desktop installs the descriptor-matched Core artifact on
the remote server.

## Required Outputs

- OpenEvo Desktop DMG for each declared macOS architecture, or one universal
  DMG;
- DMG SHA256 checksum;
- exact Core install artifact and SHA256 checksum;
- Core descriptor containing version, compatibility, source commit, artifact
  name, and checksum;
- release notes;
- dependency lock, practical vulnerability, and license results for shipped
  Python, npm, and Rust dependencies;
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
6. Dispatch `OpenEvo Core Backend checks` for the exact candidate with
   `require_real_docker=true`; the required Docker ownership job must pass
   without skips after pulling `python:3.12-slim-bookworm`.
7. Build the Desktop app and run source-level tests before packaging.
8. Build the DMG and rerun the packaged-app lifecycle and science workflow
   smoke against the exact Core descriptor/artifact.
9. Run secret-canary, diagnostics redaction, privacy, identity, docs/link, and
   dependency checks.

The exact Core wheel export parent and directory must be owned by the build user
and must not be group/world writable. On macOS the held parent must not grant
mutation through an extended ACL. A newly created directory is `0700` and may
have an inherited ACL normalized once; any later ACL addition fails closed.
The packaged `openevo/wheels` inventory must be exactly the Core wheel plus its
canonical `framework-lock.json`, both verified through Core's lock loader. After
an interrupted build, rerun the same builder with the same wheel inputs so its
durable transaction recovery can complete. The builder takes a non-blocking
exclusive lock on the held output-directory inode before inventory or recovery;
an active same-output build therefore fails explicitly instead of being treated
as crash residue. Recoverable cleanup uses one output-identity-bound sibling
tombstone/purge state and removes it before success. A successful export and the
candidate workflow must leave exactly the wheel/lock pair with no sibling cleanup
state. Do not manually remove a preserved unknown path, hardlink, symlink,
identity-mismatched replacement, or failed-cleanup tombstone without first
investigating the release workspace.

Any product or benchmark failure creates a new candidate after the fix.
Infrastructure-only retries must be recorded and may not be used to select the
best stochastic result.

## Draft Release Validation

Create a GitHub draft release only after the candidate preparation succeeds.
Upload the required outputs, download every asset into a clean directory, and
verify:

- asset names and architectures are expected;
- SHA256 files match downloaded bytes;
- the Core descriptor references the uploaded Core artifact;
- the DMG version and bundled/fetched descriptor match the candidate commit;
- release notes state unsigned/not-notarized status, supported modes, known
  limitations, benchmark counts, privacy/security behavior, and
  install/upgrade/uninstall steps;
- no unclassified development, secret, benchmark-private, or source-checkout
  files are present.

Two fresh-context `gpt-5.6-sol` high-effort reviews must approve product/spec
compliance and release risk before publication.

## Publication

After validation, create the final annotated tag at the candidate commit and
publish the already-validated draft release without rebuilding assets. Record
the release URL and final asset checksums in the release issue.

## Rollback

Before publication, close the failed draft and open a corrective issue. After
publication, mark a broken release clearly, preserve evidence needed to explain
the failure, and publish a corrected version rather than replacing bytes under
the same tag. User-facing rollback is manual installation of the most recent
compatible DMG/Core pair; document any irreversible state migration.
