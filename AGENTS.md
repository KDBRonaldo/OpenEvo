# OpenEvo experimental product branch

This branch rebuilds the OpenEvo product around the working self-hosted WebUI
path.  Do not restore the removed packaged Desktop, Tauri host, release
sidecar, Core Control daemon, or `src/openevo/deployment` stack.

## Product boundary

The only supported product path is:

```text
browser
  -> local SSH tunnel owned by scripts/dev/run_remote_agent_development.py
  -> remote scripts/dev/development_agent_web_layer.py
  -> remote scripts/dev/live_agent_daemon.py
  -> Codex harness + OpenEvo evolution runtime
```

This path is also the migration trunk.  `src/openevo/daemon` is the destination
for implementation extracted from the two `scripts/dev` services; it is not a
parallel product path.  The acceptance command, browser API, SSH topology, and
remote persisted data must remain usable after every migration slice.

The acceptance command is:

```bash
cd desktop
npm run dev:agent:webui:remote -- \
  --host <host> \
  --user <user> \
  --ssh-port <port>
```

The launcher must continue to use the user's normal OpenSSH configuration and
must not expose daemon credentials to browser JavaScript.  The Web Layer and
daemon bind to remote loopback; the browser reaches one local loopback port.
Committed source is delivered from the local checkout as a verified Git bundle
over SSH.  The remote host must not fetch product source from GitHub.  A matching
installed commit skips delivery; explicit install, update, and start actions
must remain available alongside the default one-command auto flow.  Every SSH,
transfer, remote bootstrap, and health operation must have a finite timeout.

## Source boundaries

- `desktop/self-hosted-product-preview.tsx` and
  `desktop/src/product/DesktopProductApp.tsx` are the product renderer.
- `desktop/src/api/v2`, `desktop/src/product/*V2*`, and the retained
  Desktop v2 contract modules are the current browser/Web Layer wire boundary.
- `src/openevo/daemon/project_catalog.py` owns the existing SQLite project
  table and active-project metadata behind the unchanged development API.
- `src/openevo/daemon/event_journal.py` owns the bounded recoverable state
  event journal, commit-time append semantics, cursors, and long-poll wakeups.
- `src/openevo/daemon/task_journal.py` owns durable per-task logs, typed task
  timeline entries, pagination cursors, and legacy journal backfill.
- `src/openevo/daemon/session_store.py` owns the existing Session SQLite table,
  additive legacy migrations, admitted/running/completed/failed/cancelled state
  transitions, context artifact pinning, and interrupted-session recovery.  It
  shares the compatibility daemon transaction and task journal so lifecycle
  state and journal entries commit atomically.
- `src/openevo/daemon/session_runtime.py` owns exclusive Session admission,
  asynchronous worker lifetime, live cancellation-signal lookup, idempotent
  action identity, and fail-closed cleanup.  It shares the compatibility
  coordinator's operation lock so Session, Evolution, and workspace mutations
  retain the existing serialization contract.
- `scripts/dev/live_agent_daemon.py` remains the compatibility composition
  root and owns workspace, artifact, evolution orchestration, harness/workspace
  execution adaptation, and process composition until each remaining owner is
  extracted behind the same API.
- `src/openevo/daemon` owns already-extracted daemon capabilities.  The
  `scripts/dev` entry points remain compatibility composition roots until the
  complete working path has passed real remote acceptance.
- `src/openevo/backend/evolution_runtime.py` and
  `src/openevo/backend/harness_adapter.py` are shared runtime helpers, not a
  second daemon product.
- `src/openevo/evolution`, gateway, trajectory, rollout, runtime, harness,
  and project modules remain OpenEvo Core.

Do not introduce another product server or desktop shell without a new,
reviewed architecture decision.  New functionality should first extend the
existing lightweight daemon and authenticated Web Layer.

## Incremental daemon migration

Migrate one independently testable capability at a time:

1. Characterize the current `scripts/dev` behavior with regression tests.
2. Move or adapt that implementation into `src/openevo/daemon` without
   changing its external API, persisted identity, or acceptance command.
3. Leave a thin compatibility adapter in `scripts/dev` and run the focused
   local tests.
4. Run the existing remote WebUI command against a real server.
5. Remove old code only after the replacement has passed both test and remote
   acceptance; do not combine several authority migrations into one cutover.

The preferred order is process lifecycle, Web Layer lifecycle, project catalog
and persistence, recoverable events, task journals, session lifecycle,
workspace and artifacts, agent runner, evolution orchestration, and finally
process composition.  This is a strangler migration of the proven chain, not a
launcher switch to a second chain.

The MIT-licensed `nanobot/` source may be copied or adapted for process
lifecycle, token storage, atomic state, bounded session persistence, and
HTTP/WebSocket edge patterns.  Preserve attribution in
`THIRD_PARTY_NOTICES.md`, mark substantial source-derived modules, and keep
OpenEvo wire models and evolution semantics covered by OpenEvo tests.

## Product invariants

- Creating a project never hides or deletes older projects.
- Project, session, artifact, workspace, and evolution identities are durable
  across browser, Web Layer, daemon, and machine restarts.
- Browser code never receives SSH commands, backend bearer tokens, host
  secrets, or unrestricted host paths.
- Mutations are action-ID idempotent.  Reads are bounded and validated.
- The remote checkout must equal the committed local branch head before it
  runs; pushing that branch to a Git remote is not a runtime prerequisite.
- Existing `OPENEVO_*` runtime identities and Core evolution semantics remain
  unchanged unless the task explicitly changes them.

## Verification

For product changes run:

```bash
cd desktop
npm test -- --run
npm run typecheck
npm run build:webui-gateway

cd ..
python -m pytest \
  tests/dev/test_live_agent_daemon.py \
  tests/dev/test_development_agent_web_layer.py \
  tests/dev/test_run_remote_agent_development.py -q
```

Preserve unrelated user changes.  Use `rg` for search and `apply_patch` for
text edits.  Historical Desktop documents under
`docs/maintainer/development-history` are archival only and are not current
product requirements.
