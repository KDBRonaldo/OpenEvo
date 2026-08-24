# Self-hosted Web Layer

The Web Layer is the browser-facing half of the current OpenEvo product.  It
runs on the remote server beside the lightweight daemon and is reached through
one local SSH forward.

```text
React renderer
  -> X-OpenEvo-Desktop-Session authenticated /desktop/v2/*
  -> scripts/dev/development_agent_web_layer.py
  -> bearer-authenticated daemon /v2/*
  -> scripts/dev/live_agent_daemon.py
```

The browser bootstrap token is single-use.  It is exchanged for a browser
session token; the daemon bearer token remains only in launcher/Web Layer
process memory.

The project catalog contains all persisted projects.  Task, workspace,
artifact, capability, and evolution reads remain scoped to the active project.
Selecting another project performs an idempotent activation before its
project-bound collections are loaded.

The Web Layer's event relay still consumes the same `/v2/development/events`
contract.  Its durable sequence, bounded replay window, cursor expiry, and
commit-time long-poll notification are owned by
`src/openevo/daemon/event_journal.py`; browser payloads and reconnect behavior
did not change during that extraction.

Task log and timeline endpoints also retain their v2 payload and cursor
contracts.  Their SQLite append, chunking, pagination, and restart backfill are
owned by `src/openevo/daemon/task_journal.py`. Session schema, additive legacy
migrations, context artifact pinning, lifecycle transitions, cancellation, and
interrupted-run recovery are owned by `src/openevo/daemon/session_store.py`.
The compatibility daemon still composes those owners with the harness runner,
workspace, artifacts, and evolution orchestration.

Start from `desktop/`:

```bash
npm run dev:agent:webui:remote -- \
  --host <host> \
  --user <user> \
  --ssh-port <port>
```

The remote checkout is managed under `~/.openevo/dev-agent/source`; durable
state is stored separately at `~/.openevo/dev-agent/state.sqlite3`, so source
updates and process restarts do not erase projects. The launcher probes only
the installed commit. It skips source delivery when that commit matches, or
uploads an integrity-checked local Git bundle through SSH when it differs. The
remote server never fetches OpenEvo source from GitHub.
