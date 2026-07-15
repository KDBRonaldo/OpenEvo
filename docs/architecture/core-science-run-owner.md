# Core Science Run Owner

This document defines the Core-owned execution boundary behind the frozen Core
Control v1 run routes. OpenEvo Desktop is a remote controller and observer. It
does not run a harness, call a model API, execute an evolution method, or infer
run state locally.

## Ownership

`CoreScienceRunOwner` runs in the same deployed process and Python package as
the Core Control provider. It owns:

- run create, list, detail, cancel, retry, delete, timeline, log, context, and
  artifact operations;
- the immutable project, task, workspace, registry, and revision admission pin;
- managed service generation binding and private rollout admission;
- experiment-runner orchestration for one ordinary-user science task;
- durable projection of successful evolution outputs for Desktop;
- activation of the successor revision used by the next session.

The public request and response shapes remain the frozen files in
`src/openevo/backend/contracts/v1/`. This implementation does not add a second
Desktop-specific method table or run API.

## Lifecycle

Before it persists a new run or acknowledges HTTP 202, Core asks the service
supervisor to prepare and verify the saved project's execution mode. This is
automatic; a prepared remote needs no separate Desktop service action. Missing
Codex CLI, ChatGPT subscription login, runtime executable, or managed image
returns a typed 503 and leaves no durable run. Fixed public messages are selected
from the closed readiness code, so command output and authentication material
cannot enter the response.

Durable idempotency is resolved before this volatile readiness work. Core first
persists an exact `(project_id, idempotency_key, request digest, canonical
request, run_id)` create authority. An exact request replay returns the existing
run even if host readiness has since changed,
and a different payload under the same project/key returns canonical
`idempotency_key_reused` 409 without probing services. That mismatch is not
persisted as a failed replay, so it cannot poison a later exact replay of the
original request after process-local coalescing eviction or restart. A truly
new request obtains a store-owned, per-key admission claim. A process exit before
the run insert leaves that authority for exact retry, while successful commit
atomically consumes it into the run row. The final SQLite insert rechecks the
durable identity before commit. Concurrent
same-key callers therefore cannot both run readiness or create separate runs,
and a failed readiness probe releases the in-memory claim without writing a run,
job, or per-run thread.

A running cancellation is not a terminal-state shortcut. Core persists
`cancelling`, addresses the exact rollout task recorded in the generation-bound
admission, and waits for rollout to cancel every Gateway session. Gateway DELETE
waits for runtime cancellation, harness/postrun cleanup, and dispatcher ownership
removal before it returns. Only that end-to-end acknowledgement permits Core to
publish `cancelled`. On restart, a cancelling run reloads the task and generation
authority and retries termination; missing or mismatched generation authority
stays non-terminal and fail closed. A cancelling run with no rollout admission is
safe to finish because no remote task was submitted.

After that admission preparation, Core persists the run before acknowledging
HTTP 202. The normal state sequence is:

```text
queued -> preparing -> running -> succeeded
                              \-> cancelling -> cancelled
       \-------------------------------> cancelled
                    \-------------------> failed
```

Only the Core worker can move a queued run into preparation and execution.
Worker transitions compare the persisted source state inside the SQLite write
transaction, so a concurrent cancellation cannot resurrect a cancelled run.
Cancel, retry, and delete combine the state mutation and idempotency record in
one transaction. Retry also writes its `Retry queued` timeline entry in that
same transaction; timeline capacity or construction failure rolls back the run
and replay record. Reusing an idempotency key with different request or ETag
identity fails closed as canonical `idempotency_key_reused`; the provider does
not persist that mismatch as a replayable operation failure.

One owner worker executes ordinary-user science runs serially. This keeps the
single managed service generation and subscription identity deterministic for
the first product release. The API can persist multiple queued runs; capacity
and ordering remain Core-owned rather than Desktop-owned.

## Execution

Before execution, Core revalidates the exact saved project and active revision,
then asks `CoreServiceSupervisor` for the matching release mode again. This
second probe closes the gap between HTTP admission and worker dispatch:

- `codex_subscription_transcript` uses the remote machine's logged-in Codex
  subscription and explicit transcript capture;
- self-deployed modes use the managed remote model service and preserve the
  project's transcript or token-level capture mode.

The second probe atomically returns its exact readiness snapshot and a leased run
binding under one supervisor lifecycle lock. The lease prevents another model or
runtime ensure from replacing the generation until runner and private admission
work exits. Before creating service clients, output directories, runner work, or
private admissions, the run owner independently
requires the binding's execution mode and managed image alias to match the saved
project request, and its generation digest, runtime identity digest, mode, alias,
and immutable image reference to equal the readiness snapshot. A
concurrent model ensure therefore cannot move a run onto the replacement
generation. Any mismatch fails the run with the static retryable 503
`run_service_generation_changed`; supervisor detail and private state are not
included.

Core compiles the project through `compile_science_execution()` and invokes the
existing experiment runner. The runner accepts a Core-owned run ID, an exact
initial context from the pinned revision, and a managed worker callback. This
is orchestration only: evolution method descriptors, method invocation, and
algorithm promotion semantics remain owned by the existing evolution framework
and algorithms. For product runs, Core separately records which typed target
outputs are members of the direct successor; that membership is the authority
for the next session and does not rewrite an output's `promoted` field.

`ProjectStatus.READY` remains the immutable project preparation contract: its
config, workspace snapshot, registry, model reference, and active revision are
complete. It is not evidence that the current host can run Codex. Host run
readiness is the supervisor snapshot described above, and both run creation and
execution require it. The compiler also requires a service binding whose
execution mode and managed image match the project; it does not reconstruct
runtime readiness from project fields.

Closed supervisor failures are translated to a static retryable run 503. Raw
supervisor exceptions, command output, authentication status, and runtime paths
are not used as public error text; the provider retains the same static mapping
if a run-control implementation propagates a supervisor failure directly.

The rollout task payload is first validated by the closed `TaskRequest` graph,
including typed agent, MCP, runtime action, shell command, builder, and evaluator
nodes plus all defaults, then serialized once by the shared canonicalizer. An
unknown field at any typed node is rejected instead of being omitted from the
canonical digest. The run owner binds that exact payload to the current service generation, registry
digest, framework-lock digest, task ID, and run and sends the same payload on the
wire. The rollout endpoint applies the same canonicalizer before admission, so
an omitted default cannot produce a different digest from its materialized wire
value.
Gateway, rollout, and evolution private requests are accepted only through the
generation-bound admission verifier. Core never exposes the private credential
or service URLs through the Desktop contract.

For a run with pinned context artifacts, Gateway emits a closed runtime
injection receipt only after the harness and post-processing finish and Gateway
reads the files back from the runtime. Receipt v3 binds the context/revision,
effective instruction digest, the complete runtime file inventory and tree
digest, and each artifact's source content plus actual runtime paths/tree. The
inventory includes canonical context/memory/agent-system/adapter files, every
skill file, and every agent-system target file. Missing, extra, replaced, or
wrong-target runtime bytes fail closed.

Gateway enumerates allowed agent-system targets inside the runtime with a
bounded, component-pinned no-follow reader. It returns only relative target
names, byte counts, and SHA-256 values; target content and absolute paths never
enter the receipt transport.

The rollout wrapper, not the experiment runner, captures that receipt. It reads
the immutable persisted context through the generation-authenticated Evolution
service, independently rebuilds the expected rendering from the original task
instruction and ordered revision membership, and requires exact equality before
success. The wrapper stores a canonical deep copy of both receipt and authority
for restart-safe finalization; runner-returned or runner-mutated metadata cannot
supply or alter either value.

## Cross-Session Evolution

Evolution is deliberately separated from the session that produced its data.
A run uses only the context pinned when that run was created. Successful method
outputs are activated in a direct successor revision and become inputs to the
next run.

```text
session N pinned context
  -> rollout and transcript/trajectory capture
  -> dataset and verified method jobs
  -> validated typed outputs
  -> successor revision activation
  -> session N+1 pinned context
```

No method output is injected back into the still-running session. This rule is
shared by text memory, skill bundle, agent system, and parametric memory.

Core persists the complete bounded runner result before successor activation.
If the process exits after remote work completes, startup does not rerun the
task. It replays the idempotent revision activation and exact artifact/context
publication. An interrupted pre-dispatch run without a completed result becomes
failed. An interrupted running task with a rollout admission is first moved to
`cancelling` and terminated through that exact authority. A persisted
cancellation becomes `cancelled` only after the same termination proof.

## Durable Ledger

The private ledger lives under `<state-root>/science-runs/` in an owner-only
directory and uses a private SQLite database. It stores canonical validated
documents for runs, requests, completed results, mutation replay, timeline,
logs, Desktop artifact summaries, revision context, pending create authority,
and private generation-bound admissions.

All tables have explicit row and document budgets. Timeline and log sequence
numbers are allocated in their insert transaction. A success transition and
its final timeline/log evidence are committed atomically, so Desktop cannot
observe `succeeded` without completion evidence. Revision contexts and
artifact summaries are immutable: an exact replay is accepted and a different
payload under the same identity is rejected.

Deletion is logical in the run row and cascades its private evidence. It does
not delete evolution artifacts or rewrite project revision history.

## Private Job Output Contract

The managed worker polls the authenticated private evolution job endpoint.
Only a succeeded job may expose outputs. Each output is a closed data-only
record containing:

- artifact ID, type, name, manifest, lineage, compatibility, scores, promotion
  state, and creation timestamp;
- a no-follow verified payload tree digest, byte count, and file count.

The response never contains artifact URI, host path, scanner handle, raw worker
error, command, environment, or secret. Payload summaries come from
`ArtifactPayloadService` under the Core-managed artifact root. Missing,
outside-root, linked, mutated, or over-budget payloads fail closed.

Core validates the complete output inventory before activating a successor.
It then projects the four product artifact types into the frozen Desktop
summary models. Invalid or unknown output types cannot advance the project
revision.

## Product Boundaries

- Desktop consumes only Core Control v1 over its active SSH tunnel.
- Benchmark automation remains under `benchmarks/` and calls Core separately;
  it is not presented in Desktop.
- This owner does not implement reflection, synthesis, optimization, ranking,
  or parameter training.
- A method's algorithm output and promotion decision are preserved unchanged.
- Public API hashes must remain unchanged while this implementation evolves.

## Verification

Focused verification includes:

```bash
pytest -q tests/backend/test_science_run_owner.py
pytest -q tests/backend/test_core_control_v1_provider.py
pytest -q tests/openevo/test_experiment_runner.py
pytest -q tests/evolution/test_datasets_jobs.py
```

Release validation must additionally prove the frozen Core OpenAPI/events
hashes, the verified executable registry, real Desktop-to-Core SSH execution,
and the existing evolution-method benchmark gates.
