# Changelog

## Unreleased - First Unsigned Preview

### Added

- Ship the macOS OpenEvo Desktop Client with a version-matched remote Linux
  OpenEvo Daemon.
- Add SSH agent connection, explicit host-key confirmation, private tunneling,
  and reconnectable remote sessions.
- Upload and verify the release Daemon assets and controlled science runtime
  from Desktop; users do not install an OpenEvo Python package or upload a
  runtime image.
- Support Codex subscription execution with transcript capture.
- Support cross-session textual memory, skill bundle, and agent-system
  evolution, with the committed result taking effect in the next session.
- Add typed activation remediation for remote Codex, subscription login,
  Docker, managed runtime, and Daemon readiness.

### Known Limitations

- The DMG is unsigned and not notarized; macOS requires a manual
  **Privacy & Security** exception.
- The remote SSH user must already have a supported Codex CLI installed and
  signed in to a subscription.
- SSH agent is the only supported authentication method.
- Self-Deployed execution, parameter or adapter evolution, other harnesses,
  in-session evolution, PyPI installation, and a public CLI are not supported.
- The Preview does not claim automatic preparation of every clean Linux host
  and does not expose a complete in-app remote uninstall or data-erasure flow.
