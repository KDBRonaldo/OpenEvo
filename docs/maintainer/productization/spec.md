# OpenEvo self-hosted product specification

Status: canonical for the experimental product branch.

## Goal

OpenEvo gives a researcher one browser UI for remote projects, agent sessions,
workspace files, artifacts, and evolution.  The researcher supplies an
ordinary OpenSSH target; OpenEvo installs or updates the committed local source
through SSH, starts the remote processes, opens one private tunnel, and
launches the local browser.

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
2. The user runs `npm run dev:agent:webui:remote -- ...`.
3. The launcher deploys the exact committed local branch head through SSH; the
   remote host does not need GitHub access and the branch does not need to be
   pushed before use.
4. The browser creates or selects a persistent project.
5. Sessions run against that project's remote workspace.
6. Completed session evidence can produce and apply evolution artifacts.
7. Projects and their history remain visible after creating another project
   and after process restart.

## Required behavior

- WebUI, Web Layer, and daemon versions come from one exact commit.
- A matching installed commit starts without source transfer.  Source install,
  update, and start are independently invocable, while the default command may
  compose them as one bounded operation.
- Source bundles are integrity-checked before use.  SSH connection, transfer,
  remote bootstrap, and readiness waits have finite timeouts.
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

A candidate is usable only when the real remote command starts successfully
and a browser can create two projects, switch between them, run a session,
reload history, upload/download a file, produce an evolution artifact, apply
it, and observe it in the next session.

Unit tests and simulator-only results do not replace this real end-to-end gate.
