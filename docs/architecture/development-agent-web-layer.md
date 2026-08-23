# Development agent web layer

This document describes an incremental, development-only path. It does not replace or
weaken the release Desktop/Core v2 contract.

## Data flow

```text
React renderer (unchanged product UI)
  -> Desktop session-authenticated /desktop/v2/*
  -> remote loopback development Web Layer
  -> bearer-authenticated daemon /v2/*
  -> scripts/dev/live_agent_daemon.py
  -> Codex and the remote machine workspace
```

Vite authenticates to the Web Layer with an ephemeral local token. The development daemon
bearer token remains only in the launcher/Web Layer process and is never compiled into or
returned to browser code.

The Web Layer also exposes the strict `/desktop/v2/*` projection in parallel. Project/Task
authority and mutations now use this provider. Task status, timeline, persistent logs, and
terminal agent responses are read from the daemon's bounded `/v2/tasks*` observation routes;
the renderer consumes them through the existing Desktop v2 Task contracts. The daemon assigns
monotonic per-Task sequences and persists the journals in SQLite, so pagination, refresh, and
daemon restart do not reconstruct or renumber earlier entries. Workspace inventory and binary
file transfer now use the development-only `/desktop/v2/development/projects/{id}/workspace*`
bridge to daemon `/v2/projects/{id}/workspace*`. Inventories are bounded and manifest-pinned;
uploads and downloads are SHA-256 verified. Artifact presentation and standalone Evolution run
are still separate product actions. Artifact metadata now uses the standard Desktop/Core v2
contracts, while bounded rich document presentation uses the development-only
`/desktop/v2/development/artifacts*` bridge to daemon `/v2/development/artifacts*`. Standalone
Evolution create, inventory, detail, and apply now use the development-only
`/desktop/v2/development/evolution-runs*` bridge to daemon
`/v2/development/evolution-runs*`. Creation is action-ID idempotent; run state and candidate
artifact identities are durable across daemon/Web Layer restarts.

## Start command

From `desktop/`:

```bash
npm run dev:agent:web:remote -- \
  --host js4.blockelite.cn \
  --user root \
  --ssh-port 27104
```

The existing `dev:agent:remote` command is unchanged and remains available as the direct
development path.

Only one local development launcher may own the default ports at a time. Before it contacts the
server or rotates the daemon bearer token, the launcher checks ports 5173 and 8765 (plus 8766 in Web
Layer mode). A second launcher fails locally and leaves the working remote daemon untouched. Stop the
old launcher with Ctrl+C before replacing it; Vite also uses strict port 5173 and never silently moves
the new UI to another port.

## Browser acceptance test

Unit and contract tests do not establish product usability. The development chain is accepted only
after the launcher above is running and a real browser completes:

```bash
cd desktop
npm run test:agent:web:e2e
```

The command installs Playwright's managed Chromium on first use and reuses its platform-local cache
afterward. It therefore behaves the same from WSL/Linux and Windows and does not depend on a system
Edge installation. `OPENEVO_E2E_BROWSER_CHANNEL` remains available for an explicit browser override.

This Playwright test does not mock `fetch`, the Web Layer, SSH, the daemon, the agent, or evolution.
It opens the real product page, creates a uniquely named remote project, uploads and reloads a
workspace file through daemon v2, starts a Session, waits for the remote agent and evolution worker,
refreshes task logs, applies a text-memory candidate, and saves a success screenshot. It also proves
that the rendered Artifact result was reloaded through the authenticated daemon v2 Artifact routes.
The test then starts another Session, verifies in the rendered Session inspector that the applied
Artifact is pinned as context, and waits for that real agent turn to close successfully.
A failed run retains its browser trace and screenshot under
`desktop/test-results/`. Because project deletion is not currently part of the development daemon
contract, each full test leaves its timestamped acceptance project in remote development state.

For this Web Layer command, uncommitted changes are allowed only under `desktop/`,
`docs/`, `tests/`, and the two local Web Layer launcher modules. The remote daemon remains
pinned to the committed branch head. Uncommitted changes to `live_agent_daemon.py`,
`src/openevo/`, project dependencies, or any other remotely used path still fail closed
until they are committed and pushed. The direct `dev:agent:remote` command continues to
require an entirely clean checkout.

## Implemented v2 projection

The first increment transparently forwards the complete bounded development API used by
the previously working renderer: project and workspace mutations, Session submit/cancel,
state polling, logs, artifacts, evolution job retry, and standalone evolution run/apply.
It also implements discovery/health, Desktop state, the connected development profile,
project/task/artifact projections, service status, and an SSE heartbeat on `/desktop/v2`.
Task admission, attempt, dataset-sealed timeline events and Task logs are now daemon-owned
durable v2 journals rather than responses reconstructed by the Web Layer. Artifact list, metadata,
content metadata, and rich bounded documents are likewise read from daemon-owned v2 routes; the
Web Layer no longer reconstructs Artifact authority from the v1 state response. Evolution Run
create/list/detail/apply and the frontend's polling authority now use closed daemon-owned v2
payloads; the self-hosted provider ignores Evolution Runs embedded in v1 state.

The web layer converts remote development state to closed Desktop/Core v2 response models.
It maps the development-only `report` artifact type to renderer-safe `diagnostic`; it does
not expose SSH commands, daemon credentials, remote host paths, or raw workspace paths in
the Desktop API.

## Explicit limitations

The adapter advertises only `development_agent_bridge_v2` and
`mutation_idempotency_v2`. Unsupported Desktop v2 operations fail with HTTP 503; they do
not fall back to SSH or claim release capabilities. Native-folder import, profile
lifecycle, transition repair, daemon/service mutation, diagnostics generation, and full
event replay remain for later increments.

The adapter and its small shared development v2 observation models live under `scripts/dev/`
and are not included in packaged Desktop assets or the release daemon.
