# Changelog

## 0.1.9 Preview - 2026-07-27

### Added

- List literal hosts from the Mac user's normal `~/.ssh/config` and make the
  selected alias the default remote-workspace identity.
- Delegate routing, user, port, identity files, agent/Keychain, jump or proxy
  configuration, native prompts, and host trust to system `/usr/bin/ssh`.
- Use the strict Desktop Local API v2 and active-project Core v2 tunnel for the
  supported project, Task, successor, capability, and renderer workflow.

### Changed

- Repair the macOS Tahoe packaged-sidecar startup failure with a verified plain
  ad-hoc signing policy for this unsigned Preview, without adding a
  library-validation bypass entitlement.
- Keep remote project inventory scoped to the selected project and preserve
  safe multiline science objectives while rejecting unsafe control characters.
- Require signed exact-candidate evidence for two real Codex Subscription
  Tasks, adjacent successor Project Heads, Task-2 Runtime Context reuse, three
  independently selected textual evolution targets, and packaged-renderer
  observability.

### Known Limitations

- This remains an unsigned, unnotarized Apple Silicon exhibition Preview.
- The supported server is the documented Linux x86-64 Docker user-container
  profile with at least `20,000,000` KiB available on the remote home
  filesystem and Codex already installed and signed in for the selected
  account.
- Self-Deployed execution, parameter or adapter evolution, other harnesses,
  Intel Macs, automatic Codex installation/login, and a complete clean-host
  matrix remain outside this Preview.

The immutable release is available from the
[OpenEvo Desktop 0.1.9 Preview release](https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/openevo-desktop-v0.1.9-v019-system-ssh-final.30212086910.1).

## 0.1.5 Preview - 2026-07-21

### Added

- Make **Add remote workspace** the prominent first-run action for connecting
  Desktop to a supported Linux host.
- Add Codex model and reasoning-effort controls for Subscription projects.
- Let users independently enable textual memory, skill bundle, and agent-system
  targets and select each target's Daemon-reported evolution method.
- Include two built-in, read-only scientific project demonstrations with task
  timelines and evolution artifacts that require no server configuration.

### Changed

- Present the Desktop interface and built-in demonstrations entirely in
  English and use the current OpenEvo application icon throughout macOS and the
  app interface.
- Allow Desktop to upgrade a managed OpenEvo Daemon from Preview `0.1.4` to the
  release-matched `0.1.5` bundle.

### Known Limitations

- This is an unsigned, non-notarized Apple Silicon exhibition Preview. Follow
  the checksum and quarantine instructions in the Desktop quickstart.
- The supported path uses a remote Linux x86-64 host with Docker user-container
  access and a Codex CLI that is already signed in to a subscription.
- Self-deployed inference, parameter evolution, other harnesses, Intel Mac
  builds, and a general clean-host matrix remain outside this Preview.

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
