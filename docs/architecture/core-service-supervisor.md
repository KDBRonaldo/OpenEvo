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
post-attach service mutations. The release launcher injects this supervisor into
the Core provider. `/v1/services` and `/v1/services/{id}` therefore project the
supervisor's verified read-only summaries alongside `core-control`. Service
restart/log routes remain unavailable until their frozen operations have durable
provider ownership and idempotency semantics. Injection does not make the
supervisor a run owner, revision ledger, Desktop component, or evolution method.

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
- the dedicated SID/PGID and a non-secret generation ownership digest needed to
  recognize a stale Core-owned process group;
- typed state and bounded, redacted service log entries.

Raw argv, environment values, framework-lock paths, service-root paths, tokens,
credentials, URL userinfo/query values, and host paths are not persisted in
observable service/log snapshots. The child environment is rebuilt from a small
non-secret runtime allowlist instead of inheriting the complete Core daemon
environment. `PYTHONPATH`, `PYTHONHOME`, virtual-environment hints, and arbitrary
parent variables are absent. Every managed Python command uses isolated `-I`
mode. Core opens the owner-only `child-cwd` directory no-follow, verifies its
inode and mode, and the child changes directory through that inherited FD before
exec; neither the supervisor's caller cwd nor a replaced pathname can shadow the
installed `openevo` package.

Construction has an explicit `release` versus `development_test` boundary.
Release accepts only a loader-sealed `VerifiedExecutableRegistry`; it rejects an
injected digest, interpreter, process backend, health checker, port probe, or
runtime probe. Construction and every `ensure`/`restart`, including completed
restart replays, re-read the sealed registry's exact installed distribution
inventories through a private framework-owner helper and require the resulting
install/registry identity to remain unchanged. The helper first checks the
unforgeable registry and distribution seals and is not exported by the framework
public API.
`development_test` must instead be selected explicitly and is the only mode that
accepts test identities and runtime dependency injection. It cannot alter the
release path.

Each started generation receives a new high-entropy internal credential. The
credential is written to one child-specific pipe and inherited by FD; only the
FD number is present in the child environment. It is never present in argv,
topology, ledger, durable logs, dataclass repr, or health output. Evolution and
rollout protect their complete release-owned HTTP surfaces. Gateway protects
health, session, event, model-inventory, and admin routes while preserving the
separate agent-facing completion/session-identity protocol. Worker traffic uses
the same authenticated evolution client. Requests must carry the exact bearer,
generation, and registry identity; bearer comparison is constant-time and every
missing or mismatched value fails closed.

Durable output filtering treats JSON as a closed diagnostic object. Known
credential, authorization, cookie, API key, token, password, private-key, and
AWS-secret fields are redacted; values under unknown structured fields are not
persisted. Header, environment, JSON, URI, and key-value forms receive the same
bounded scalar filter before storage. URI filtering follows the closed scheme
syntax and removes userinfo, query, and fragment data for non-HTTP schemes such
as PostgreSQL and Redis as well as HTTP. Space-separated secret options and
environment forms such as `--api-key value` and `OPENAI_API_KEY value` redact
only the value. Values may be unquoted tokens or complete single-/double-quoted
values with backslash escapes; an opening quote without a matching close redacts
the complete remaining line instead of retaining a quote tail. Allowlisted JSON
strings use this same closed scalar grammar as plain text. A per-process streaming
redactor retains only one
bounded incomplete line, so every secret form is recognized even when any token,
JSON field, URL, or header is split across arbitrary stdout/stderr chunks. Lines
over 16 KiB are discarded through the next newline and represented only by
`<redacted-oversize-line>`; an unterminated oversize line receives the same marker
at EOF. Normal residual lines are sanitized at EOF. An exact generation credential
suffix is treated as a sensitive partial prefix only from a deterministic eight-byte
minimum, preserving benign text that happens to end in the credential's random first
character while still suppressing meaningful partial credentials. No raw prefix is
emitted before the complete line has passed the generic and generation-credential
filters.

The real subprocess backend starts a dedicated session/process group and binds
the leader PID to `/proc` start ticks, uid, exact cmdline digest, SID, PGID, and
the inherited non-secret ownership digest. Liveness and signal operations scan
the complete group, including grandchildren, and fail closed if any member does
not match owner, session, group, and generation evidence. On Linux, direct
children also receive a parent-death signal.

Startup recovery may signal only a stale group recognized by that full persisted
identity. It sends TERM and then KILL within a bound, verifies convergence, and
only then clears the ledger pin. An absent old group is safe; a reused PID,
foreign member, unreadable identity, changed SID/PGID, or ownership mismatch
blocks startup without signaling. A leader exit callback captures generation
and complete process identity, so a delayed callback cannot clear a replacement
generation. If the leader exits while a grandchild remains, the handle and group
identity are retained for close/restart recovery. The real backend reserves from
a fixed tracked-process capacity before spawn. It releases a live identity only
after the exit callback completed and the full process group disappeared; a
separately bounded completed-result tombstone preserves concurrent/repeated
`wait` observations. Capacity exhaustion fails before creating another child.

## Supported Service Group

`codex_subscription_transcript` is the fully implemented group. Core writes one
closed generation topology with prebound dynamic ports and directly launches
these entry points in dependency order:

1. `python -I -m openevo.evolution.cli serve` on a prebound loopback listener;
2. `python -I -m openevo.rollout.server` on a prebound loopback listener;
3. `python -I -m openevo.gateway.server` on a prebound loopback listener;
4. `python -I -m openevo.evolution.cli worker` against the same verified framework
   lock and evolution artifact root.

Each subscription ensure request also carries the bounded Codex model and one of
the existing Core-owned managed Science runtime image tags. Before any service
is spawned, `ManagedScienceRuntimeProbe` revalidates the pre-Core bootstrap
boundary under the same total deadline. The default local probe uses controlled
argv, never a shell, to check `codex --version`, `codex login status`,
`docker --version`, and `docker image inspect`. Login status must report a
ChatGPT subscription login; an absent login, failed status command, malformed
output, or API-key login fails closed. The image must have a SHA-256 image ID and the
`io.openevo.managed-runtime=true` label produced by the managed Science
Dockerfile. It opens `~/.codex/auth.json` no-follow, requires a link-count-one
owner-owned `0600` file, and hashes only file metadata. It never reads or
persists auth content or the login-status output. Probe stdout and stderr are drained concurrently without
`communicate()`. One hard aggregate byte budget covers both streams; crossing it
immediately kills the complete probe process group and performs a bounded leader
reap. Cancellation and deadline paths use the same group-wide bounded cleanup.

The probe returns a closed `ServiceRunReadinessCode`: `ready`,
`codex_cli_unavailable`, `codex_subscription_auth_unavailable`,
`runtime_executable_unavailable`, `runtime_image_unavailable`, or
`runtime_evidence_invalid`. Probe output is never used as a readiness message.
The code and non-secret runtime identity are retained in the private service
ledger so a missing executable cannot be reported as a missing image and a
failed auth check cannot be presented as a ready generation.

The resulting non-secret runtime evidence digest, exact managed image tag, and
Codex model are bound into the service generation. The Codex model is also the
topology's non-serving model identity. The supervisor topology deliberately has
no evolution context, latest-promoted lookup, event-export fail-open policy, or
runtime injection choice. Per-task `RuntimeSpec`, an admission-pinned exact
revision, and its strict materialized context must later come from the Core run
owner. The supervisor does not resolve, select, promote, inject, or rewrite any
task context.
If bootstrap evidence is unavailable, all four services report `unavailable`,
no child is spawned, and single-service restart cannot bypass the probe.

The runtime probe and its bounded command runner are injectable. Tests therefore
exercise service lifecycle failures without Docker, while separate probe tests
cover the managed image/Codex/private-auth evidence contract. This is a
revalidation boundary for the existing bootstrap result, not a second image
builder or a return to Desktop-owned post-attach service commands.

The Codex subscription is consumed only by the harness during an
admission-owned transcript-mode session. The supervisor does not start a Codex
API client, expose a direct model API, or claim token-level capture. Gateway
health explicitly reports `capture_mode=transcript`,
`token_level_metrics_available=false`, and `direct_model_api=false`.

HTTP components become running only after their process-group identity and an
authenticated closed health document match service ID, generation, framework
lock digest, and registry digest. Evolution backend and worker independently
load and verify the copied framework lock; worker startup registers its actual
generation/lock/registry identity. Gateway must authenticate to rollout, and
rollout health must prove that the exact gateway node has registered and is
schedulable. An arbitrary 2xx response is never readiness.

`ensure` is serialized and generation-idempotent. Before every ensure/restart it
re-runs Codex auth/version, managed-image, framework-lock pathname/content, and
release inventory checks. The original framework lock remains held by stable FD;
its exact bytes are copied to the owner-only service root, and replacement or
in-place mutation fails before current children are stopped. Every HTTP listener
is bound to dynamic loopback port zero by Core and inherited by FD, eliminating
port availability/bind TOCTOU. One total startup deadline applies and partial
groups roll back in reverse order.

`restart` is idempotent for the same `(service_id, operation_id)`. Operation IDs
cannot be reused for another service. The process-local replay table has a fixed
capacity: exact completed replays remain available at capacity, while a new
operation is rejected before changing service state. Entries live until the
supervisor closes; eviction cannot silently turn an old replay into a new restart.
Release restart requests reverify the sealed installed inventory after acquiring
the lifecycle mutex and validating the supervisor root, before any completed
replay can return. An attestation change therefore fails closed without spawning
or mutating service state. A new restart performs its second plan-execution
reverification through a private force-restart path; both checks complete before
the active plan key, process group, ledger, replay table, or spawn backend can
change. This keeps a change detected between the two inventory reads transactional.
Because auth,
registration, and health are generation-scoped, a restart rotates the credential
and replaces the complete four-service group rather than leaving a mixed
generation. Child exits are monitored and persisted as failures. `close` and
`cancel` share one total deadline, send `SIGTERM`, escalate owned children to
`SIGKILL`, and do not signal unverified recovery groups. Ensure/restart publish a cancellation
token before invoking probes or readiness; `cancel` sets it without waiting for
the lifecycle mutex. Initial startup and existing-generation health checks receive
that same token, and cancellation remains the typed `service operation was
cancelled` error instead of being wrapped as listener or health failure. Command
and HTTP probes poll it with a short bound before the sole owner performs rollback.
If an injected backend still proves a child live after escalation, close fails
and retains host-global ownership; it does not admit a replacement supervisor
beside an unkillable child.

## Service Availability Versus Run Readiness

`ServiceGroupSnapshot.services_available` means only that the authenticated
internal service graph above is live. `run_ready` additionally requires typed
runtime readiness evidence and the private generation-bound run-admission
endpoint installed by the release launcher. Otherwise `run_readiness_code`
names the closed prerequisite or service/admission failure. `run_binding()`
requires that stronger state and carries the exact execution mode, managed image,
and runtime evidence digest to the science execution compiler.

The run owner still revalidates the exact project snapshots, immutable revision,
registry, and generation before dispatch. Evolution outputs apply only to a
later session after the revision contract commits. A promoted artifact, legacy
context result, or caller-provided readiness field cannot make a generation
runnable.

Internal bearer authentication is not run admission. In a release-owned service
generation, `POST /rollout/task/submit` and both forms of gateway `POST /sessions`
must also pass an injected `GenerationBoundRunAdmissionVerifier`. The verifier's
closed check contains only operation, generation/registry/framework-lock digests,
task/session identity, and the SHA-256 digest of the canonical validated payload.
It does not receive the credential, raw instruction, runtime, environment, or an
open request object. Request fields such as `run_ready` or `admission` have no
authority and are excluded by validation before the canonical digest is built.

The supervisor now binds each release child to the host-global Core daemon's
fixed loopback-only private verifier endpoint. Rollout and Gateway forward only
the closed digest check over that channel, using their ephemeral generation
credential. The supervisor separately issues an in-memory `ServiceRunBinding`
to the trusted run owner; it contains the exact generation and service URLs plus
request headers whose credential is excluded from repr and durable state.

The private endpoint is installed only when a real run control owner is injected.
Without that owner, release-owned submissions still return a fail-closed typed
error before manager dispatch or session registration. The endpoint is excluded
from OpenAPI, accepts at most 4096 bytes, validates a closed JSON object, and
authenticates it against the supervisor's current generation before invoking the
authority. Verifier failures never fall back to `run_ready`, legacy context, or
caller-provided instruction/runtime data.

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
is not a substitute for the provider's durable mutation contract. Its bounded
fail-closed behavior protects this internal slice, but the provider must retain
durable operation identity/results across Core process restarts.

## Verification And Residual Risks

Focused coverage is in `tests/backend/test_core_service_supervisor.py` and
`tests/backend/test_internal_service_auth.py`. It includes total deadlines,
prebound listeners, partial rollback, delayed exit callbacks, idempotent
concurrent ensure/restart, lock replacement, malicious ledger/symlink rejection,
bounded cancellation and close escalation, every-boundary generic log redaction,
oversize-line discard, aggregate probe-output termination, release-registry
anti-bypass/fresh-import checks, tracked/replay capacity, unauthenticated HTTP
rejection, exact worker/gateway registration, and real child-to-grandchild
process-group termination and stale-owner recovery probes. No test downloads a model.

Residual integration risks remain explicit:

- the existing managed image tag is bound to the locally inspected Docker image
  ID and managed-runtime label, but this slice does not add signed container
  image provenance; release bootstrap remains responsible for preparing the
  expected image;
- Linux `/proc` and parent-death behavior are the release-host process identity
  path; other platforms need an equivalent verified backend before support;
- automatic crash restart is intentionally absent; Core reports failure and a
  caller performs an idempotent restart;
- durable run execution, revision readiness, model preparation, Desktop run
  forwarding, and release E2E remain downstream integration work.
