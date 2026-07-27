# OpenEvo Desktop v0.1.10 Lifecycle Operations Design

Status: approved for implementation

Issue: [#220](https://github.com/CompLifeLab-ZJU/OpenEvo/issues/220)

Target release: OpenEvo Desktop v0.1.10

## Problem

The immutable public v0.1.9 macOS release uses a 15-second renderer deadline
for `POST /desktop/v2/projects`, while the sidecar allows the remote project
activation path to run for as long as 900 seconds. On a real cold connection to
the configured `evolab` system-OpenSSH alias, the renderer reported
`Desktop Local API request timed out`; the sidecar continued and successfully
created the project. A user retry minted a new action identity, so the Core
bridge durably recorded two different `create_project_v2` mutations and two
different remote projects as applied.

This exposes two broader product gaps:

1. Desktop-owned long work is still implemented as a synchronous HTTP request,
   even though the canonical product spec requires asynchronous operation IDs,
   resumable progress, bounded logs, safe cancellation, and idempotent retry.
2. Renderer mutations pass an idempotency key, but most UI call sites mint a
   new key for every click. After an ambiguous timeout, disconnect, process
   termination, or application restart, the user cannot replay the original
   mutation intent.

v0.1.10 fixes the lifecycle model rather than merely increasing one timeout.

## Goals

- Return a durable operation identity promptly for every implemented
  Desktop-owned long lifecycle mutation.
- Report authoritative phase changes, measurable progress when available,
  indeterminate activity otherwise, elapsed time, bounded process logs, typed
  failure, retryability, and cancellation capability.
- Display actual SSH and Daemon child-process stdout/stderr in the Desktop UI.
- Make ambiguous retries reuse the exact original idempotency identity across
  renderer reload and Desktop restart.
- Audit every renderer mutation, including fast mutations and Core-owned
  operations, for the same exact-retry rule.
- Preserve the system-OpenSSH authority model and the post-negotiation active
  project tunnel boundary.
- Prove the fix with OpenSSH-signed real-macOS evidence in which a cold
  lifecycle operation exceeds 15 seconds and creates exactly one remote
  project.

## Non-goals

- Process output is not success authority and will not be parsed to infer
  completion.
- v0.1.10 does not add a remote shell, arbitrary command execution, or command
  editing to the renderer.
- v0.1.10 does not expose child process environment variables, SSH invocation
  arguments, Core URLs, bearer tokens, askpass values, or secret references.
- Existing duplicate projects are not deleted automatically.
- Task execution and evolution do not move back to SSH. Core-owned work remains
  on the negotiated active project tunnel and uses Core operation/timeline
  authority.
- Progress percentages are not estimated from elapsed time.

## Scope

### Desktop-owned long lifecycle work

The asynchronous lifecycle contract covers every currently implemented path
that may wait on native I/O, SSH, Daemon preparation, or project activation:

- system-OpenSSH profile connect and disconnect;
- host-key review continuation;
- Daemon preflight, transfer, install, verification, startup, repair, and
  readiness;
- native workspace traversal, archive preparation, and sidecar adoption;
- remote project create and activation.

Native app/sidecar startup precedes the Local API and therefore cannot be a
sidecar lifecycle operation. Tauri exposes a closed `NativeStartupStatusV2`
for the already-owned startup epoch. The bootstrap screen polls that authority
and shows its real fixed stage, checkpoint progress, elapsed time, retry, and
safe cancellation while the sidecar is unavailable. It does not expose raw
bootloader/Python stderr; the existing bounded classified startup diagnostic
remains the failure authority.

SSH catalog scans and ordinary bounded CRUD remain synchronous because their
current implementations perform no unbounded external work. If implementation
inspection disproves that premise, the affected route must join the lifecycle
contract before v0.1.10 release. Core-owned Task, transition, service,
diagnostic, and maintenance operations retain their Core authority. Desktop
adds only renderer-safe projections for Core operation lookup/cancel, service
logs, and cache cleanup through the active project tunnel. Task
state/timeline/logs, transition progress, diagnostic status, service logs, and
`OperationV2` progress feed the shared presentation component without
inventing Desktop-owned phases or copying Core operations into the provider
store.

### Mutation idempotency audit

Every renderer mutation is classified as one of:

- fast, deterministic Desktop-local mutation;
- Desktop-owned asynchronous lifecycle mutation;
- Core-owned asynchronous mutation;
- native-host mutation.

Every class must retain one stable action ID through an ambiguous result and
must bind it to the exact canonical request and observed authority. A later
user action that is intentionally different receives a new action ID.

## Authority And Data Flow

```text
renderer form/action
  -> native durable mutation-intent journal (fsync + CAS)
  -> authenticated Desktop Local API mutation with stable Idempotency-Key
  -> atomic sidecar operation/idempotency reservation
  -> HTTP 202 LifecycleOperationV2
  -> bounded sidecar worker
       -> authoritative phase/progress checkpoints
       -> SSH/Daemon child stdout/stderr log records
       -> remote mutation using the same exact intent
       -> verified terminal result
  -> Desktop SSE operation invalidation
  -> renderer GET operation + paginated logs
  -> authoritative resource refresh
  -> native journal clear after proved terminal reconciliation
  -> idempotent sidecar terminal acknowledgement
```

The initial mutation request performs validation and durable reservation only.
It does not hold an HTTP connection while remote work runs. The default Local
API request deadline may therefore remain short and bounded; remote lifecycle
deadlines live exclusively in the operation worker.

## Desktop Local API v2 Changes

The bundled v0.1.10 renderer and sidecar negotiate a new exact OpenAPI digest,
event schema digest, feature-set digest, build identity, and release identity.
There is no fallback to the v0.1.9 schema.

### Operation model

`LifecycleOperationV2` is a strict closed model with:

- `schema_version`;
- opaque `operation_id`;
- closed `kind`;
- closed resource reference;
- canonical `request_sha256`;
- `status`: `queued`, `running`, `succeeded`, `failed`, or `cancelled`;
- closed `phase` plus monotonic `phase_index` and fixed `phase_total`;
- optional measurable sub-progress;
- `cancellable`;
- nullable typed terminal `result`;
- nullable typed `failure`;
- monotonic `log_sequence_high_watermark`;
- `created_at`, nullable `started_at`, `updated_at`, and nullable
  `finished_at`;
- strong `etag`.

The measurable sub-progress union is:

- `indeterminate`, with no fabricated count;
- `bytes`, with non-negative `completed` and positive `total`;
- `items`, with non-negative `completed` and positive `total`.

`completed` never exceeds `total`. Phase index, progress, log high-water mark,
status, and timestamps cannot regress. A terminal operation is immutable.
Operation kind constrains its allowed phase sequence and terminal result type.

### Routes

Long mutation routes return HTTP 202 with `LifecycleOperationV2` immediately:

- profile connect/disconnect and host-key continuation;
- project create/activate;
- native workspace preparation after the native folder picker has returned.

Native workspace preparation is reserved through the authenticated hidden
native-to-sidecar workspace-import route because the renderer must never receive
the selected host path. The Tauri command returns the same renderer-safe
`LifecycleOperationV2`; the public observation, log, cancellation, state, and SSE
routes remain the sole renderer-visible operation authority.

Observation and control use:

```text
GET  /desktop/v2/operations/{operation_id}
GET  /desktop/v2/operations/{operation_id}/logs
POST /desktop/v2/operations/{operation_id}/cancel
POST /desktop/v2/operations/{operation_id}/acknowledge
```

Core-owned observation remains namespaced and tunnel-only:

```text
GET  /desktop/v2/core-operations/{operation_id}
POST /desktop/v2/core-operations/{operation_id}/cancel
GET  /desktop/v2/services/{service_id}/logs
POST /desktop/v2/maintenance/cache-cleanup
```

The Core operation responses preserve Core `OperationV2` status, progress,
failure, and ETag. They never fall back to SSH execution or the Desktop
lifecycle store.

The log route uses a signed cursor, a maximum page size of 100, stable ascending
sequence order, explicit truncation metadata, and the same Desktop session
authentication as every other renderer route. An expired cursor returns the
typed 410 reset response and the renderer reloads the retained tail.

Project create changes from synchronous HTTP 201 `ProjectV2` to HTTP 202
`LifecycleOperationV2`. Its successful terminal result contains the authoritative
Core project ID. The renderer refreshes the project collection and accepts only
the exact matching project. It never fabricates a pending `ProjectV2`.

`DesktopStateV2` gains bounded pending operation references so restart and SSE
recovery can rediscover work without depending on component-local state. The
list includes every nonterminal operation and every terminal operation whose
native journal reconciliation has not yet been acknowledged.

Acknowledgement is an idempotent terminal-only handshake. The renderer first
reconciles the exact terminal result and clears the matching native journal row,
then acknowledges the immutable terminal operation. A crash after native clear
but before acknowledgement leaves the terminal operation discoverable in
`DesktopStateV2`; the next launch can finish the handshake. Acknowledgement is
stored separately and does not mutate the terminal operation document or ETag.

### Events

The Desktop SSE union adds a strict `lifecycle_operation_changed` invalidation
payload containing only:

- operation ID and kind;
- status and phase;
- operation ETag;
- log high-water mark.

SSE does not carry process log bodies. It tells the renderer to fetch the
authoritative operation and missing bounded log pages. Existing event replay,
cursor, sequence, payload digest, memory budget, and gap behavior remain in
force.

## Phase And Progress Semantics

The closed phase vocabulary covers the actual implementation checkpoints:

- validation;
- queued;
- resolving system OpenSSH;
- connecting;
- waiting for authentication or host-key decision;
- remote preflight;
- transferring;
- verifying;
- starting Daemon candidate;
- waiting for Daemon readiness;
- opening project tunnel;
- negotiating Core authority;
- preparing native workspace;
- creating or loading remote project;
- verifying project authority;
- activating;
- finalizing.

Each operation publishes a fixed phase plan when reserved. Inapplicable phases
are marked complete as skipped; they are not silently removed after work starts.
The main progress bar advances only when a checkpoint is durably completed.
Within transfer, hashing, or archive phases, a secondary determinate byte/item
value may advance. While a phase is not measurable, the bar shows an
indeterminate animation and the exact phase text.

No log text, process exit alone, timeout estimate, or elapsed-time heuristic can
advance phase authority or publish success.

## Process Logs

The user-visible log is actual stdout/stderr emitted by the SSH and Daemon child
processes used for the selected lifecycle operation. It is not a synthetic list
of friendly messages. Desktop-authored checkpoint lines may also be present and
are identified as `desktop` source.

Each `LifecycleLogEntryV2` contains:

- operation ID;
- monotonic sequence;
- timestamp;
- source: `desktop`, `ssh_stdout`, `ssh_stderr`, `daemon_stdout`, or
  `daemon_stderr`;
- text;
- a boolean indicating line truncation.

The sidecar strips terminal escape/control sequences before persistence and
rendering. It never sends the child command line or environment. Known
credentials, authorization headers, bearer tokens, proxy userinfo, askpass
values, configured secret canaries, Core endpoints, and absolute host paths are
replaced before a log record can be committed. This mandatory contract
boundary remains even on a single-user Mac because the child process itself may
echo authentication material or private host authority.

One entry is limited to 16 KiB of UTF-8 text. One operation retains at most
4,096 entries and 4 MiB; the provider store retains at most 32 MiB of lifecycle
log text across operations. When per-operation or global retention would be
exceeded, the oldest complete entries are evicted, preferring acknowledged
terminal operations, and each affected operation records a durable
dropped-before sequence. Operation/result/idempotency authority is never
evicted as a substitute for log eviction. Writes, cursor reads, recovery, and
eviction enforce the same row and aggregate byte accounting before loading text
into Python. Renderer memory keeps only a bounded visible tail; older retained
pages load on demand.

## Durable Operation Store

The provider v2 store gains strict operation, operation-result, log, and
idempotency authority in one schema migration. Reservation atomically writes:

- operation identity and fixed phase plan;
- exact profile/project/resource generation and ETag inputs;
- canonical request digest and idempotency key;
- initial queued state;
- reserved row/byte capacity for terminal operation and replay documents.

The worker cannot begin external work before queued authority is committed. Each
phase, progress, and log append is an atomic monotonic transition. Terminal
result and idempotency replay commit together. Same key plus exact request and
authority returns the same operation/result. Same key plus different bytes or
authority fails before any external work.

The lifecycle executor retains one external-work worker. The store admits at
most 16 recoverable authorities total: nonterminal operations plus terminal
operations still awaiting reconciliation acknowledgement. This intentionally
serializes the process-global SSH/Daemon/project-session authority and ensures
every item fits in `DesktopStateV2`. Conflicting operations that cannot safely
wait fail with a typed busy response; exact replay remains available at
capacity, and no caller can create a seventeenth recoverable operation.

Terminal operation, replay, and retained-log authority remains readable for
seven days after terminal reconciliation acknowledgement. Cleanup never removes
a nonterminal operation, an unacknowledged terminal operation, or an idempotency
row whose terminal result has not been reconciled. Capacity pressure fails new
reservation before external work; it does not evict live or unreconciled
authority.

On sidecar restart, every nonterminal operation is reconciled from durable
state. The worker uses the existing remote lifecycle and Core bridge ledgers to
determine whether it can safely resume the same operation ID, recover an applied
result, or publish a retryable typed failure. It never starts a new remote
mutation merely because the prior HTTP caller disappeared.

## Renderer Mutation-Intent Journal

Idempotency must survive WebView loss, so component state alone is insufficient.
The Tauri native host owns a private, bounded `PendingMutationIntentV2` journal
using the same identity-verified directory, kernel-lock, atomic write, fsync,
and compare-and-swap principles as the existing retry recovery journal.

Each journal entry contains only non-secret authority:

- action ID;
- mutation kind and resource scope;
- canonical request digest;
- observed ETag/generation and provider stream identity;
- current chain step;
- optional current accepted operation ID and at most two completed operation
  IDs;
- lifecycle state and timestamps.

The journal holds at most 16 entries, at most 64 KiB of canonical bytes per
entry, and at most 1 MiB in aggregate. The UI currently serializes ordinary
mutation controls, but storage does not assume a single row. A mutation writes
its intent before transport. An HTTP 202 binds the returned operation ID. A
direct deterministic response is journaled before UI publication. Capacity
failure disables a new mutation with a typed local error and preserves every
existing unresolved entry.

The journal is retained for:

- Local API timeout;
- connection reset or aborted fetch;
- Desktop process termination;
- typed unknown-outcome response;
- failure to prove whether a terminal response was durably observed.

It is cleared only after one of:

- an exact successful result is journaled and reconciled with authoritative
  state;
- an exact deterministic rejection proves no side effect can later publish;
- a terminal failed/cancelled operation is authoritatively observed;
- the user abandons an operation through a contract action that first proves
  there is no live or unknown side effect.

Changing form data, resource generation, ETag, or action scope cannot silently
overwrite an unresolved intent. The UI first offers resume/reconcile; a truly
new action receives a new ID only after the prior intent reaches a proved
terminal disposition.

Native-folder project creation is one fixed two-step mutation chain because the
existing private workspace-import ownership derives from the action ID. Before
the picker opens, the journal binds the current project draft, profile authority,
and `native_folder_snapshot` source kind; conflicting form controls then freeze.
The native preparation operation can become a completed intermediate operation,
be acknowledged, and advance the same journal row to `project_create` without
clearing or changing its action ID. Project creation binds the second operation.
The row clears only after that project is reconciled, or after an explicit
discard proves the prepared import and both operations have no live side effect.

## User Experience

One reusable lifecycle panel is embedded in the native startup screen,
connection drawer, project creation drawer, project activation flow, native
workspace preparation flow, Task/evolution view, transition view, diagnostic
view, and service/maintenance operation view. Each adapter preserves its native,
Desktop, or Core authority. It contains:

- operation title and current authoritative phase;
- determinate checkpoint/byte/item progress where available;
- indeterminate animation otherwise;
- elapsed time without an estimated completion time;
- the latest process log lines in a monospace tail;
- an expandable, paginated retained log history;
- typed terminal result or failure and next action;
- Cancel only while the operation says cancellation is safe;
- Resume/Reconcile for a retained ambiguous intent.

Controls that could conflict with the operation are disabled. Closing a drawer
does not abandon work. A global operation indicator keeps it discoverable, and
relaunch reopens the authoritative active operation. Accessibility includes
status text independent of color, `aria-live` updates that do not announce every
log line, keyboard access to log expansion/cancel, and reduced-motion behavior.

## Failure And Cancellation

- Cancellation is compare-and-set against the exact operation ETag.
- The sidecar terminates only the process/tunnel authority owned by that
  operation and ignores late completion.
- Non-cancellable commit/readiness barriers expose `cancellable=false` instead
  of pretending cancellation succeeded.
- A deadline produces a typed operation failure; it does not cause the renderer
  request to guess whether work succeeded.
- Process log capture failure is a typed observability degradation unless the
  failure could block child pipes or violate secret handling; those cases fail
  the operation closed.
- SSE failure falls back to bounded polling of the known operation ID without
  creating another mutation.

## Compatibility And Migration

- v0.1.9 remains immutable.
- v0.1.10 uses a new Desktop Local API v2 OpenAPI digest, event schema digest,
  feature set, source commit, and release manifest.
- The Desktop and packaged sidecar remain an exact pair; mismatched schema
  digests fail closed.
- Existing profile, project-draft, bridge mapping, and applied mutation rows are
  preserved by an explicit provider-store migration.
- The two projects already created during the reported reproduction remain
  intact. No migration chooses one for deletion.
- Existing remote Daemon business authority remains authoritative. Desktop
  lifecycle operation state does not import or rewrite Core project history.

## Test-Driven Implementation Requirements

Every production change starts with a focused failing test. Required coverage
includes:

### Contract and client

- strict Python and TypeScript models reject unknown fields, invalid phase
  transitions, regressing progress, oversized logs, and cross-wired identity;
- route status/model, OpenAPI digest, event digest, and feature negotiation;
- asynchronous mutation requests return before a simulated remote delay longer
  than 15 seconds;
- SSE invalidation plus cursor-paginated operation/log refresh.

### Store and worker

- atomic reservation before external work;
- exact replay returns one operation/result;
- same key with changed request/authority performs no external work;
- crash/restart at every reservation, phase, remote-mutation, terminal, and
  journal-clear boundary;
- monotonic progress and terminal immutability;
- bounded executor admission and conflict fencing;
- log line/row/aggregate budgets, eviction gaps, cursor expiry, malformed UTF-8,
  control sequences, and pre-Python byte guards;
- cancellation races and late worker completion;
- secret-canary coverage for stdout and stderr.

### Renderer and native host

- native app/sidecar startup exposes fixed-stage progress, elapsed time,
  retry/cancel state, and a closed failure without waiting for Local API
  availability;
- stable action ID on ambiguous retry and across relaunch;
- changed request cannot reuse or overwrite the retained intent;
- success/rejection/terminal reconciliation clears only the exact journal row;
- live phase, progress, elapsed state, log tail, expandable history, cancel, and
  resume UI;
- keyboard, accessibility, reduced-motion, responsive layout, and visual
  baselines;
- no 15-second timeout error while remote lifecycle work is legitimately
  running.
- Task/evolution, transition, diagnostic, service, and maintenance views render
  their existing Core state/progress/log authority through the shared panel and
  never create a Desktop lifecycle shadow operation.

### Real release E2E

The exact ad-hoc-signed, non-notarized candidate DMG is installed under
`/Applications` on
the target Mac and tested against the real configured system-OpenSSH alias. A
cold project lifecycle must:

1. exceed 15 seconds;
2. return an operation promptly;
3. show at least two authoritative phase changes and real child log text;
4. survive one SSE reconnect and one Desktop quit/relaunch, or an equivalently
   strong restart-recovery scenario;
5. finish with one authoritative Core project and one applied mutation;
6. run a real science Task and retain the v0.1.9 two-session successor/context
   acceptance path;
7. emit signed release evidence with secret-canary checks.

## Release And Documentation

Implementation updates:

- `docs/maintainer/productization/spec.md` for renderer-visible bounded child
  process logs and lifecycle operation semantics;
- `docs/architecture/desktop-core-contract-v2.md` for the exact API, event,
  authority, and secret boundary;
- `desktop/src/product/README.md` and `desktop/sidecar/README.md` for module
  behavior and verification;
- checked-in contracts, digests, release assets, release acceptance manifests,
  handoff/release notes, and user-facing troubleshooting.

The implementation PR must resolve #220. After all local and CI gates pass, the
exact candidate is pushed to `stable`, packaged under the canonical unsigned
DMG/ad-hoc app-signature policy, installed,
tested on the real remote workspace, and published as a new immutable public
v0.1.10 Preview. The v0.1.9 release and tag are never modified.

## Design Self-Review Checklist

- No placeholder, TODO, or unresolved product choice remains.
- Long-operation scope is explicit and distinguishes Desktop from Core
  authority.
- Progress cannot imply unverified success.
- Process log visibility honors the requested user experience while retaining
  mandatory credential/token exclusion and bounded rendering.
- Idempotency survives ambiguous transport and renderer restart.
- Existing user data is preserved.
- Test and release evidence directly reproduce the reported defect.
