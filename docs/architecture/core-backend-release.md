# OpenEvo Daemon Release Architecture

OpenEvo Daemon is the remote Linux application used by OpenEvo Desktop. It is
assembled from the shared Core implementation and the release-managed runtime.
Desktop configures the remote server and monitors runs, while the Daemon owns
run lifecycle, service supervision, datasets, jobs, artifacts, context
resolution, and runtime injection.

## Core-Owned Service Supervision

The External Beta Daemon must expose typed service state through backend APIs.
It must supervise or report the status of the services needed for a run,
including backend API, gateway, rollout, worker, and optional model-serving
processes. Desktop may request repair or restart through typed APIs, but it
must not directly execute release-mode gateway, rollout, worker, benchmark, or
run commands after bootstrap.

The Core Control API itself is one host/user-global loopback daemon, not one
daemon per project or run. Its filesystem, release identity, pidfd lifecycle,
readiness, and attach contract are defined in
`docs/architecture/core-control-host-service.md`.

## Sidecar Boundary

The Desktop sidecar owns local app integration and remote bootstrap:

- keychain references and local sidecar token;
- SSH connection and tunnel setup;
- uploading or downloading the verified Daemon Bundle;
- creating the remote user-level install;
- starting the Daemon if it is absent;
- forwarding Desktop requests to the Daemon through loopback.

After the Daemon is healthy, ordinary run, log, artifact, doctor, repair, and
service operations go through its APIs.

## Evolution Capabilities

Core owns evolution capability discovery. The release endpoint is:

```text
GET /capabilities?execution_mode=codex_subscription_transcript
GET /capabilities?execution_mode=self-deployed
```

The query value is a product release mode, not a framework execution-mode ID.
Core maps it once to an `EvolutionExecutionProfile`, evaluates method support,
and returns `EvolutionCapabilitiesV1` from the same startup-verified frozen
registry used by planning and worker dispatch. The response includes the Core
version, registry digest, evaluated profile, target and handler identity,
configured and effective defaults, schemas, ordered inputs, and four independent
support axes. Audience-visible `methods` are separate from `accepted_methods`
used to preserve valid existing configs and from Core-owned
`selection_resolvers` such as `agent_system.method=auto`.

Core returns a typed `503` when no verified registry was supplied at startup.
It does not import legacy method metadata or synthesize a static response. The
Desktop sidecar forwards this endpoint through the active SSH tunnel and also
fails closed when the tunnel is absent or the remote payload is invalid. It
re-fetches the payload and validates the active project selections immediately
before each run launch.

## State Layout

Core Control service state is rooted once per remote host and OS user. Project
and task state is stored as Core-owned resources beneath that service boundary;
it does not choose a second backend root or listener. Remote evolution state is
rooted in the configured OpenEvo state root.
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

Deleting local Desktop state does not delete remote Daemon state. The Daemon
provides project/run cleanup APIs and documented repair actions so users can
remove remote datasets, jobs, artifacts, logs, and diagnostics intentionally.

## Cleanup

Bootstrap rollback should remove incomplete user-level installs and leave a
repairable journal. Runtime cleanup should avoid deleting promoted artifacts or
datasets that a later run may need.
