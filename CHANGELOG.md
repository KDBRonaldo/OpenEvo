# Changelog

## 0.1.4 Preview - 2026-07-20

- Keeps evolution target activation under explicit user control and resolves
  each enabled target's method from the connected Daemon capabilities.
- Shows the completed session timeline in authoritative remote sequence order
  and treats the Daemon-reported Project Head as the sole active revision
  authority while local projections refresh.
- Adds candidate-bound packaged-renderer observation against the live Desktop
  Local API, including bounded task, log, and evolution-artifact verification.
- Lets users inspect every completed session's authoritative timeline and
  transcript from the project history.
- Requires signed, candidate-bound two-session Codex Subscription evidence,
  including cross-session artifact reuse, before Preview publication.
- Preserves the existing evolution algorithm implementations; this release
  changes product configuration, observability, validation, and packaging only.

## 0.1.3 Preview - 2026-07-20

### Added

- Add two built-in, read-only scientific demonstrations that show task
  timelines, transcript summaries, cross-session evolution, and the resulting
  `memory.md`, `SKILL.md`, and `AGENTS.md` artifacts without requiring a server.
- Let users independently enable textual memory, skill bundle, and agent-system
  targets and select each target's method from remote Daemon capabilities.
- Add configurable Codex model and reasoning effort controls for Subscription
  projects and propagate them through Desktop, Daemon, Core, and the Codex CLI.

### Changed

- Harden remote Codex readiness checks, Daemon restart recovery, and release
  evidence around the supported Docker user-container exhibition profile.
- Migrate Desktop project storage to schema v7 while preserving existing
  projects and idempotent replay records.
- Validate the exact Apple Silicon DMG by mounting it, launching it, copying the
  application out of the image, detaching the image, and relaunching the copy.

### Known Limitations

- The DMG remains unsigned and not notarized; follow the checksum and quarantine
  instructions in the Desktop quickstart.
- The remote account must already have Docker user-container access and a
  supported Codex CLI signed in to a subscription.
- Self-Deployed execution, parameter evolution, other harnesses, PyPI
  installation, and a public CLI remain unavailable in this Preview.
- This is an exhibition Preview, not a G1-G12 External Beta candidate.

The immutable release is available from the
[OpenEvo Desktop 0.1.3 Preview release](https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/openevo-desktop-v0.1.3-exhibition.29756612736.1).

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

The next release must not be cut from a published Preview without a new
candidate identity. It requires candidate-bound real Codex science E2E and the
remaining canonical release gates described in
`docs/maintainer/productization/spec.md`.
