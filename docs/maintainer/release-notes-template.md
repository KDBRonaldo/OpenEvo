# OpenEvo External Beta Release Notes Template

Replace every placeholder and remove sections that do not apply before
publishing a GitHub Release.

This template is for a future final External Beta candidate. The automated
packaging-only draft uses a smaller closed note contract, is checksum-bound to
its `0 of 3` and `pending` results, and must never be edited or promoted. After
all final gates pass, populate this template and generate a new candidate,
manifest, checksum inventory, and draft roundtrip from reviewed source.

## Release

- Version:
- Release date:
- Commit SHA:
- GitHub Actions run:

## Download And Install

- Desktop DMG and supported architecture:
- DMG SHA256:
- Core install artifact:
- Core descriptor and SHA256:
- Supported macOS versions:
- Unsigned/not-notarized notice:
- Checksum verification and Gatekeeper steps:

## Supported Workflows

- Codex subscription transcript mode:
- Self-deployed reference model ID, revision, vLLM profile, and hardware:
- Textual memory:
- Trajectory-to-skill:
- Agent-system evolution:
- Science workflow and promoted-artifact reuse:

## Known Limitations

- Signing, notarization, and automatic update status:
- PyPI status: disabled for External Beta.
- Unsupported harnesses, platforms, models, and experimental methods:
- Parameter evolution status:
- No leaderboard claim:

## Validation Results

- Textual-memory pass@1 rescue count and benchmark summary:
- Trajectory-to-skill pass@1 rescue count and benchmark summary:
- Agent-system pass@1 rescue count and benchmark summary:
- Core clean-install and integration result:
- Codex three-artifact science E2E result:
- Self-deployed three-artifact science E2E result:
- Packaged DMG smoke result:
- Security, privacy, diagnostics, and docs result:

## Security And Privacy

- Secret storage and redaction:
- Backend/sidecar local binding and authentication:
- Telemetry default:
- Local and remote data locations:
- Diagnostics sharing behavior:
- Deletion and retention behavior:
- Unsigned-app security caveat:

## Dependencies

- Python lock/vulnerability/license result:
- npm lock/vulnerability/license result:
- Rust lock/vulnerability/license result:

## Upgrade, Uninstall, And Support

- Upgrade steps and compatibility:
- Rollback limitations:
- Desktop uninstall steps:
- Remote state/model/runtime cache cleanup:
- Troubleshooting guide:
- Support issue link:
