# Core Internal Service Supervisor

`CoreServiceSupervisor` is the Core Backend infrastructure owner for the
evolution backend, rollout server, gateway, and evolution worker subprocesses.
It replaces the old product assumption that Desktop can start individual remote
services over SSH after attaching to the host-global Core daemon.

This module is not an evolution algorithm, method registry, experiment runner,
or public product surface. It does not select methods or change their inputs,
outputs, timing, promotion, or revision semantics.

## Ownership Boundary

The lifecycle boundary is:

```text
Desktop -> tunneled frozen Core Control API -> Core provider
  -> injected CoreServiceSupervisor -> owned local subprocess groups
```

Desktop may bootstrap and attach to the Core daemon, but it must not invoke the
supervisor directly, construct service argv, or fall back to SSH commands for
post-attach service mutations. The current frozen provider does **not** inject
this supervisor yet. Its service restart/log routes therefore remain unavailable
until a later provider slice wires the internal typed API and durable operation
semantics. This implementation must not be described as provider, Desktop, run
owner, or revision-ledger integration.

## Private State And Process Identity

The supervisor has one owner-only host-global service root. It opens every path
component relative to held directory FDs without following symlinks, rejects
unsafe writable ancestors, requires the managed root and child directories to be
owned by the Core user with mode `0700`, and retains an exclusive owner lock.
Every operation rechecks the held root `(device, inode, uid)` against the
pathname binding. On Linux, a uid-and-canonical-path-derived abstract Unix socket
also holds the host-global owner identity, so replacing the complete root cannot
admit a second Core owner on a different inode.

`ledger.json` and `topology.json` are link-count-one `0600` regular files written
as canonical JSON through temporary-file `fsync`, atomic rename, directory
`fsync`, and exact readback. The ledger is bounded and closed. It persists only:

- verified install, framework-lock, and executable-registry digests;
- per-service argv, environment, topology, port, and combined identity digests;
- PID plus a Linux process birth token while the current supervisor owns it;
- typed state and bounded, redacted service log entries.

Raw argv, environment values, framework-lock paths, service-root paths, tokens,
credentials, URL userinfo/query values, and host paths are not persisted in
observable service/log snapshots. The child environment is rebuilt from a small
non-secret runtime allowlist instead of inheriting the complete Core daemon
environment.

The real subprocess backend starts a dedicated process group and binds the PID
to `/proc` start ticks, uid, and the exact cmdline digest. Signal operations are
allowed only for handles spawned by the current supervisor and revalidate that
birth token first. On Linux, children receive a parent-death signal. Startup
recovery never signals a PID read from an old ledger: any residual starting,
running, degraded, or PID-bearing row becomes `service_prior_owner_lost` with no
PID pin. This prevents PID reuse or a modified ledger from authorizing signals
to an unrelated process.

## Supported Service Group

`codex_subscription_transcript` is the fully implemented group. Core writes one
deterministic topology and directly launches these existing entry points in
dependency order:

1. `python -m openevo.evolution.cli serve` on loopback port `8200`;
2. `python -m openevo.rollout.server` on loopback port `8080`;
3. `python -m openevo.gateway.server` on loopback port `8100`;
4. `python -m openevo.evolution.cli worker` against the same verified framework
   lock and evolution artifact root.

Each subscription ensure request also carries the bounded Codex model and one of
the existing Core-owned managed Science runtime image tags. Before any service
is spawned, `ManagedScienceRuntimeProbe` revalidates the pre-Core bootstrap
boundary under the same total deadline. The default local probe uses controlled
argv, never a shell, to check `codex --version` and `docker image inspect`; the
image must have a SHA-256 image ID and the
`io.openevo.managed-runtime=true` label produced by the managed Science
Dockerfile. It opens `~/.codex/auth.json` no-follow, requires a link-count-one
owner-owned `0600` file, and hashes only file metadata. It never reads or
persists auth content.

The resulting non-secret runtime evidence digest, exact managed image tag, and
Codex model are bound into the service generation. The Codex model is also the
topology's `model_served`, matching the existing Science services plan and
providing the correct base-model identity to transcript/context processing.
The topology keeps the existing evolution context and event-export settings.
Per-task `RuntimeSpec` still comes from the compiled Science task through rollout
and gateway; the supervisor does not duplicate or rewrite task runtime setup.
If bootstrap evidence is unavailable, all four services report `unavailable`,
no child is spawned, and single-service restart cannot bypass the probe.

The runtime probe and its bounded command runner are injectable. Tests therefore
exercise service lifecycle failures without Docker, while separate probe tests
cover the managed image/Codex/private-auth evidence contract. This is a
revalidation boundary for the existing bootstrap result, not a second image
builder or a return to Desktop-owned post-attach service commands.

The Codex subscription is consumed only by the harness during a transcript-mode
session. The supervisor does not start a Codex API client or claim token-level
capture. HTTP components become running only after both their PID birth identity
and health endpoint are live. The worker requires repeated live process-identity
observations. Spawn success alone is never readiness.

`ensure` is serialized and generation-idempotent. It preflights all ports,
applies one total startup deadline, and rolls a partially started group back in
reverse order. `restart` is idempotent for the same `(service_id, operation_id)`.
Child exits are monitored and persisted as failures. `close` and `cancel` share
one total deadline, send `SIGTERM`, escalate owned children to `SIGKILL`, and do
not signal unowned recovery PIDs. If an injected backend still proves a child
live after escalation, close fails and retains host-global ownership; it does
not admit a replacement supervisor beside an unkillable child.

## Self-Deployed Boundary

`self-deployed` uses the same typed `ensure` interface but is intentionally not
ready in this slice. It returns an `inference` service with status `unavailable`
and model preparation status `unresolved`; no OpenEvo service or vLLM process is
started. The state names the required next integration interface:
`model_preparer_v1`.

That future interface must own all of the following before it can return ready:

- validate the pinned self-deployed reference profile and model revision;
- accept proxy and Hugging Face credentials through an ephemeral secret channel,
  while exposing only non-secret identity digests to the supervisor;
- verify/install allowed user-space dependencies within explicit version and
  download boundaries;
- download and content-verify the model into an owner-only managed cache;
- launch vLLM as an owned process group with the same PID-reuse protections;
- verify the health endpoint and exact served model, then publish preparation
  progress and readiness atomically;
- define restart requirements for adapters and serving configuration.

Until that interface exists, a model name, cache directory, installed `vllm`
module, open port, or successful spawn must not be converted into `ready`.

## Internal Typed Projection

The supervisor exposes typed list, get, restart, group snapshot, and bounded log
snapshot methods. `SupervisorServiceSummary.to_contract()` and
`SupervisorLogEntry.to_contract()` map to the frozen `ServiceSummaryV1` and
`LogEntryV1` without changing their schemas. Evolution backend and rollout map
to the frozen `control` kind, gateway to `gateway`, worker to
`evolution_worker`, and future model serving to `inference` with mandatory model
preparation state.

Provider wiring must add API authorization, durable operation records, event
publication, pagination/cursors, and request idempotency around these internal
objects. The supervisor's in-memory restart-operation cache is process-local and
is not a substitute for the provider's durable mutation contract.

## Verification And Residual Risks

Focused coverage is in `tests/backend/test_core_service_supervisor.py` and
includes total deadlines, port conflicts, partial rollback, crash observation,
idempotent concurrent ensure/restart, malicious ledger and symlink rejection,
startup PID recovery, bounded redacted logs, bounded close escalation, and a
real lightweight local subprocess smoke test that downloads no model.

Residual integration risks remain explicit:

- the port preflight and child bind are separate operations; the real health
  check detects a bind race and rolls back, but cannot reserve the port for the
  child;
- the existing managed image tag is bound to the locally inspected Docker image
  ID and managed-runtime label, but this slice does not add signed container
  image provenance; release bootstrap remains responsible for preparing the
  expected image;
- Linux `/proc` and parent-death behavior are the release-host process identity
  path; other platforms need an equivalent verified backend before support;
- automatic crash restart is intentionally absent; Core reports failure and a
  caller performs an idempotent restart;
- provider, run owner, revision readiness, model preparation, Desktop forwarding,
  and release E2E are not connected by this change.
