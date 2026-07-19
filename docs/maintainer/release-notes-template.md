# OpenEvo External Beta Release Notes Template

Replace every placeholder and remove sections that do not apply before
publishing a GitHub Release.

This template is for a future final External Beta candidate. The automated
packaging-only Preview uses a smaller closed note contract and is
checksum-bound to its `0 of 3` and `pending` results. Its prepublication GitHub
draft may be published only by changing visibility after unchanged
revalidation through the protected numeric-ID Preview publisher; its title,
body, target, and assets must not be edited. A post-publication verification
failure is handled as an incident and does not trigger automatic deletion.
After all final
gates pass, populate this template and generate a new candidate, manifest,
checksum inventory, and draft roundtrip from reviewed source.

Do not turn packaged capability or Playwright evidence into a real-run claim.
Until exact-candidate Codex science E2E evidence passes the release verifier,
state that Codex Subscription is packaged and declared but still awaits
supported-host verification. A verified claim must cite evidence binding the
packaged Desktop, remote OpenEvo Daemon, subscription-authenticated Codex,
transcript capture, both science sessions, promoted artifacts, and successor
session reuse.

## Release

- Version:
- Release date:
- Commit SHA:
- GitHub Actions run:
- Release manifest SHA256:
- Detached candidate evidence index and G12 attestation:

## Download And Install

- Desktop DMG and supported architecture:
- DMG SHA256:
- Daemon Bundle and SHA256:
- Release manifest and SHA256:
- Supported macOS versions:
- Unsigned/not-notarized notice:
- Developer ID signature: absent; app-bundle signature: ad-hoc.
- Synthetic quarantine removal and post-removal signature/launch result:
- Checksum verification and Gatekeeper steps:

## Supported Workflows

- Codex subscription transcript mode (packaged/declared/real-host verified):
- Self-deployed reference model ID, revision, vLLM profile, and hardware:
- Restricted-network/proxy profile:
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
- Daemon Bundle clean-install and integration result:
- Codex three-artifact, two-session science E2E result and evidence identity:
- Self-deployed three-artifact science E2E result:
- Packaged DMG smoke result:
- Clean-host matrix result:
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
- Local `~/.openevo/desktop` data retention and cleanup:
- Tauri native host app-data for `org.openevo.desktop`, including run-retry
  recovery state, retention and cleanup:
- Remote state/model/runtime cache cleanup:
- Troubleshooting guide:
- Support issue link:
