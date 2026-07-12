# Core Backend Release Architecture

OpenEvo Core Backend is the server-side execution and evolution backend used by
OpenEvo Desktop. Desktop configures the remote server and monitors runs, while
Core owns run lifecycle, service supervision, datasets, jobs, artifacts,
context resolution, and runtime injection.

## Core-Owned Service Supervision

External Beta Core must expose typed service state through backend APIs. It
must supervise or report the status of the services needed for a run, including
backend API, gateway, rollout, worker, and optional model-serving processes.
Desktop may request repair or restart through typed APIs, but it must not
directly execute release-mode gateway, rollout, worker, benchmark, or run
commands after bootstrap.

## Sidecar Boundary

The Desktop sidecar owns local app integration and remote bootstrap:

- keychain references and local sidecar token;
- SSH connection and tunnel setup;
- uploading or downloading the verified Core artifact;
- creating the remote user-level install;
- starting Core Backend if it is absent;
- forwarding Desktop requests to Core through loopback.

After Core is healthy, ordinary run, log, artifact, doctor, repair, and service
operations go through Core APIs.

## State Layout

Remote Core state is rooted in the configured workspace and OpenEvo state root.
Evolution state uses `.openevo/evolution` for datasets, jobs, artifacts, and
context records. Per-session runtime state uses `/openevo/session` inside the
runtime. Release docs and diagnostics must not introduce legacy runtime markers.

## Runtime Paths

Core stages selected evolution artifacts into the runtime under
`/openevo/session/evolution`. Harnesses consume staged files through
`OPENEVO_*` environment variables and harness-specific instruction paths.

## Migration

Core owns state schema version checks and migrations. Release-mode startup must
reject too-new state, record migration evidence, and make failed migrations
recoverable through typed repair actions.

## Deletion

Deleting local Desktop state does not delete remote Core state. Core should
provide project/run cleanup APIs or documented repair actions so users can
remove remote datasets, jobs, artifacts, logs, and diagnostics intentionally.

## Cleanup

Bootstrap rollback should remove incomplete user-level installs and leave a
repairable journal. Runtime cleanup should avoid deleting promoted artifacts or
datasets that a later run may need.
