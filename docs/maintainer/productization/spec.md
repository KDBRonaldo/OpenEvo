# OpenEvo self-hosted product specification

Status: canonical for the experimental product branch.

## Goal

OpenEvo gives a researcher one browser UI for remote projects, agent sessions,
workspace files, artifacts, and evolution.  The researcher supplies an
ordinary OpenSSH target; OpenEvo installs or updates one verified versioned
release through SSH, starts the remote processes, opens one private tunnel,
and launches the local browser. Maintainers may use the exact-commit Git-bundle
delivery path during source development.

## Supported topology

```text
browser on the user's machine
  -> authenticated local loopback WebUI
  -> one system-OpenSSH tunnel
  -> remote loopback Web Layer
  -> authenticated remote loopback daemon
  -> Codex CLI and OpenEvo Core
```

There is no packaged Tauri application, release sidecar, or separate Core
Control daemon in this branch.  Those removed implementations are not fallback
paths.

## User workflow

1. The user has a working SSH configuration and remote Codex login.
2. The user runs `openevo webui`, selects a discovered `~/.ssh/config` host,
   or supplies explicit SSH connection arguments.
3. The launcher verifies and deploys a versioned Release Bundle through SSH.
   During source development it may instead deploy the exact committed local
   branch head. The remote host does not need GitHub access and a development
   branch does not need to be pushed before use.
4. The browser creates or selects a persistent project.
5. Sessions run against that project's remote workspace.
6. Completed session evidence can produce and apply evolution artifacts.
7. Projects and their history remain visible after creating another project
   and after process restart.

## Required behavior

- WebUI, Web Layer, daemon, server contract code, and dependency lock come from
  one exact committed release payload and share one content-derived release ID.
- A matching installed release (or development commit) starts without source
  transfer. Install, update, and start are independently invocable, while the
  default command may compose them as one bounded operation.
- Release and development source bundles are integrity-checked before use.
  Release contents are verified locally, after SSH transfer, and again when an
  installed release is reused. SSH connection, transfer, remote bootstrap, and
  readiness waits have finite timeouts.
- Release payloads are immutable and versioned separately from their Python
  environment and authoritative SQLite/workspace state.
- The daemon persists authority in
  `~/.openevo/dev-agent/state.sqlite3` and project workspaces under the same
  managed state root.
- Only the active project's task-bound collections are loaded, while the
  project catalog contains every persisted project.
- Browser authentication is ephemeral and separate from the daemon bearer
  token.  Neither SSH credentials nor daemon tokens enter renderer state.
- Project/task/workspace/evolution mutations are idempotent and closed-model
  validated.
- Remote services bind to loopback and are reached only through the launcher
  tunnel.
- Failure must preserve existing daemon state and report an actionable phase.

## Acceptance

A candidate is usable only when its Release Bundle is built from an exact
commit, installed on a real remote host without a server-side GitHub checkout,
and the browser can create two projects, switch between them, run a session,
reload history, upload/download a file, produce an evolution artifact, apply
it, and observe it in the next session.

Unit tests and simulator-only results do not replace this real end-to-end gate.
