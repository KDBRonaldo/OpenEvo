# Daemon rebuild v0

The new daemon starts from the proven self-hosted development path and does not
restore the removed release sidecar or managed deployment stack.

## Migration topology

```text
browser
  -> unchanged local SSH tunnel
  -> development_agent_web_layer.py compatibility composition
  -> live_agent_daemon.py compatibility composition
  -> capabilities progressively extracted into openevo.daemon
```

There is no parallel cutover daemon.  The working remote WebUI command is the
acceptance path throughout the rebuild.  `openevo.daemon` begins as a library
and lifecycle shell behind that path and gradually becomes its implementation.

The first implementation owns only:

- one loopback FastAPI process;
- public `/health` and authenticated `/v1/daemon/status` routes;
- private token, PID identity, state, and log files under
  `~/.openevo/daemon/`;
- `start`, `status`, `logs`, `restart`, and `stop` lifecycle commands;
- stale-PID protection before process termination.

Session, task, transcript, workspace, artifact, and evolution behavior remains
in the proven development daemon until each API is migrated with regression
tests.  Project catalog persistence has moved behind the compatibility
composition into `src/openevo/daemon/project_catalog.py` without changing its
SQLite tables.  The existing remote WebUI command remains unchanged during
this phase.

## Slice acceptance rule

For every capability, first freeze the current observable behavior, then move
the owner into `src/openevo/daemon`, retain a compatibility adapter in
`scripts/dev`, and validate the original remote command.  Persisted SQLite
identity and HTTP models are migration inputs rather than disposable debug
state.  A compatibility adapter is removed only after local regression tests
and a real SSH-hosted WebUI run both pass.

The planned order is:

1. process and token lifecycle;
2. Web Layer startup/shutdown lifecycle;
3. project catalog and SQLite persistence;
4. recoverable state events;
5. task logs and typed timeline;
6. session lifecycle;
7. workspace files and artifacts;
8. Codex runner and transcript capture;
9. evolution orchestration;
10. final single-process composition.

The lifecycle shell, Web Layer lifespan migration, project catalog, recoverable
state events, and task journal extraction are complete.  Task logs and timeline
still use `development_task_logs_v2` and `development_task_timeline_v2`, share
the same state transaction, and backfill historical session rows at startup.
No remote data migration is required.  The session lifecycle row and its
running/completed/failed/cancelled transitions are the next authority boundary.

The lifecycle layout is adapted from the MIT-licensed HKUDS/nanobot gateway.
Nanobot source may be copied for lifecycle, authentication, atomic persistence,
and HTTP/WebSocket edge mechanics with source attribution.  OpenEvo keeps its
own wire models, SSH topology, scientific project semantics, and evolution
runtime.
