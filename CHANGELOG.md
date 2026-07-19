# Changelog

## 0.1.2 Preview - 2026-07-19

### Added

- Ship the macOS OpenEvo Desktop Client with a version-matched remote Linux
  OpenEvo Daemon.
- Add SSH agent connection, explicit host-key confirmation, private tunneling,
  and reconnectable remote sessions.
- Upload and verify the release Daemon assets and controlled science runtime
  from Desktop; users do not install an OpenEvo Python package or upload a
  runtime image.
- Manage Daemon installation, startup, attachment, reconnection, and supported
  repair actions from Desktop without requiring ordinary users to SSH to the
  server.
- Support Codex subscription execution with transcript capture.
- Support cross-session textual memory, skill bundle, and agent-system
  evolution, with the committed result taking effect in the next session.
- Add typed activation remediation for remote Codex, subscription login,
  Docker, managed runtime, and Daemon readiness.

### Known Limitations

- The DMG is unsigned and not notarized; macOS requires a manual
  **Privacy & Security** exception.
- The Preview host must be prepared by the server administrator with Docker
  user-container access and a supported Codex CLI signed in for the selected
  account; Desktop detects these prerequisites and ordinary users do not run
  remote setup commands.
- SSH agent is the only supported authentication method.
- Self-Deployed execution, parameter or adapter evolution, other harnesses,
  in-session evolution, PyPI installation, and a public CLI are not supported.
- The Preview does not claim automatic preparation of every clean Linux host
  and does not expose a complete in-app remote uninstall or data-erasure flow.
- This packaging Preview has no candidate-bound real Codex science E2E,
  protected benchmark result, clean-host matrix, or External Beta qualification.

The immutable release is available from the
[OpenEvo Desktop 0.1.2 Preview release](https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/openevo-desktop-v0.1.2-exhibition.29702250883.1).

## Unreleased

The next release must not be cut from this published Preview without a new
candidate identity. It requires the real Codex science E2E and the remaining
canonical release gates described in `docs/maintainer/productization/spec.md`.
