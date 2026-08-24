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
Exclusive Session admission, worker-thread lifetime, live cancellation signal
lookup, and unconditional operation-lock cleanup are owned by
`src/openevo/daemon/session_runtime.py`. Persistent project directories,
bounded document projections, digest-complete inventories, uploads, downloads,
and safe mutations are owned by `src/openevo/daemon/workspace_store.py`.
Dataset/evolution artifact persistence, promoted-context selection, canonical
records, and stable cursor pagination are owned by
`src/openevo/daemon/artifact_store.py`. Normalized Codex harness invocation and
the admitted Session transaction across context loading, workspace mutation,
terminal persistence, cancellation, failure, and optional evidence sealing are
owned by `src/openevo/daemon/agent_runner.py`. The compatibility daemon now
supplies its development runtime-context materializer and Evolution sealer to
that runner. Transcript dataset sealing, development capability resolution,
fixed-input Evolution jobs/retries, output validation, artifact publication,
and explicit multi-Session candidate runs are owned by
`src/openevo/daemon/evolution_orchestrator.py`. The compatibility daemon retains
unchanged HTTP models, SQLite rows, and process composition.

The renderer enters the Session conversation as soon as the user starts a
task. It shows the submitted instruction during validation/admission, then
uses the development provider's 750 ms active-Session refresh loop to replace
startup state with daemon transcript messages. Active Sessions expose the
existing authenticated cancel endpoint from both the conversation and the
project overview; cancelling state remains visible until daemon authority
reports a terminal result.

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
