# Desktop Active-Tunnel Core Bridge v1

`desktop/sidecar/core_bridge_v1.py` is the release-sidecar ownership boundary
between saved Desktop project intent and the frozen Core Control API v1 client.
It implements the bridge contract without starting science runs, harnesses, or
child services over SSH.

## Injected Boundaries

- `CoreHostService` ensures or attaches the host-global Core and returns a
  profile-bound remote port, bearer, and stable Core host identity.
- `CoreTunnelFactory` opens one private loopback tunnel from only the profile
  identity and remote port. It does not receive the bearer.
- `WorkspaceArchiveSource` resolves an already adopted
  `WorkspaceImportRefV1` to a read-only binary stream. Its contract has no path.
- `DesktopCoreBridgePersistence` durably transitions project create, workspace
  abort, and project patch ownership, and compare-and-swaps the local-to-Core
  mapping while retaining adapter-owned history.

The persisted create operation binds local project, profile, Core host
identity, the full canonical Core `ProjectCreateV1`, its digest, idempotency
key, returned Core project ID, and workspace upload ID plus its owning project
snapshot. A successful workspace finalize is CAS-persisted on the same operation
before mapping commit, including the complete pre-finalize upload, canonical
request and key, and exact strict-client-validated finalize response. Its create
state is explicit:

- `pre_create` proves no create request has been dispatched. A deterministic
  failure in version/capability/bootstrap preparation leaves this state, so a
  later Local retry action may atomically reserve a new key.
- `unknown` is persisted immediately before project create transport. Only the
  exact canonical request and original key may replay it.
- `bound` records the Core-assigned project ID. Later Local activation keys
  resume that binding and never issue another project create. The original
  canonical request remains durable even if the Local draft changes before a
  completed mapping can be committed; recovery verifies that request against
  the bound Core project before patching the edited Local intent.

An open upload superseded by a later imported workspace remains attached to the
create operation until Core returns a terminal abort result. The operation
stores the complete open upload authority, canonical abort request and digest,
idempotency key, and `pre_abort`/`unknown` state. It transitions to `unknown`
before transport. A missing response never permits a GET-based inference or a
new abort request. Recovery calls the strict client's public
`abort_persisted_workspace_upload` transaction, which validates the exact
persisted open representation, ETag, and idempotency key, restores authority,
executes abort, and commits result delivery under one client generation
barrier. A concurrent client close rolls the restored authority back. Clearing
the abort and stale upload binding is one create-operation CAS. Already terminal
uploads need no abort and may be cleared after their exact identity is read.

A persisted nonterminal workspace finalize is recovered before any pending
project patch is interpreted or any patch for newer Local intent is reserved.
The bridge first replays the original open-upload request with its exact ETags
and idempotency key and persists the validated terminal response. It then
commits the already-applied project intent as the next mapping generation and
converges separately to the latest Local intent. This also applies when the
newer edit changes only project/task fields and keeps the imported workspace;
`patch=applied` plus `finalize=unknown` cannot strand the project or be bypassed
by a newer workspace selection.

Each Local project may also have one durable patch operation. It stores the
canonical old and new `ProjectCreateV1` intents and digests, canonical
`ProjectPatchV1` and digest, deterministic key, Core project identity, complete
pre-patch Core authority including ETag/snapshots, and the validated Core
outcome. An applied row additionally persists explicit projections of that
outcome's immutable content authority and mutable publication/runtime
authority; the projections cover the complete `ProjectV1` rather than leaving
fields implicitly classified. Its states are `pre_patch`, `unknown`, and
`applied`. Persistence must:

1. reserve without replacing a different pending operation;
2. full-row CAS `pre_patch` to `unknown` before transport;
3. exact-replay every `unknown` operation, even when a Core read resembles the
   intended result;
4. full-row CAS the complete validated response to `applied`; and
5. atomically append the mapping version and remove that exact applied
   operation.

The bound create operation first persists the create response's complete
immutable projection: Core project ID, canonical `ProjectCreateV1`, task
snapshot, and `created_at`. The initial project GET and every initial workspace
finalize must match it exactly. A Desktop-authorized patch may replace canonical
project intent and task snapshot according to its signed-snapshot rules, but it
must preserve the Core project ID and `created_at`; this check runs before the
response can become an applied durable outcome.

The last transaction compares the complete previous mapping. A rollback leaves
the old mapping and applied operation intact. If Local intent advanced from A
to B after Core applied A, recovery proves the persisted A outcome, commits A
as the next mapping generation, then reserves a distinct A-to-B operation.
Recovery does not require a pre-finalize imported-project outcome to equal the
current project as a whole. A workspace finalize may legitimately advance the
project snapshot, workspace snapshot, status, publication, ETag, readiness, and
revision authority. In that case the durable finalize response must bind a
predecessor project snapshot and ETag exactly matching the applied patch's
mutable authority, plus the exact final project snapshot, workspace snapshot,
and publication observed now. If the applied imported-draft outcome has no active
revision, its pre-patch base revision remains the effective predecessor authority.
If both are absent, the finalize/current authority may remain absent or first
appear only as a same-project generation-zero revision. Later successor-only
mutable authority uses the same transition validator as mapping and patch
recovery. The same revision requires an exact complete mutable projection;
a direct successor requires a new ETag, strictly newer `updated_at`, and no
change outside active revision, registry digest, ETag, and timestamp. Recovery
rejects rollback, same-generation ID or manifest rewrites, generation jumps,
reused successor ETags, time rollback, and mutable publication drift before
another workspace mutation, mapping commit, or current ETag adoption. Only
after this proof may Desktop commit mapping A;
a requested B then starts from A's current ETag and gets a separate mapping
generation.

The completed mapping also stores the canonical mapped request, exact
project/task/workspace content snapshots, a complete immutable authority
projection (Core project ID, canonical project intent, task snapshot, and
`created_at`), a complete mutable authority projection (status,
project/workspace snapshots, publication, revision, registry, model preparation,
timestamp, and ETag), monotonic mapping generation, and predecessor request
digest. The scalar snapshot/revision/registry/ETag/time indexes must exactly
mirror those projections. Mapping commit receives the complete expected prior
mapping; a durable adapter must retain the ordered audit history and reject lost
updates. Every load recomputes the canonical request digest and validates both
projection bindings before Core transport.

## Durable Store

`desktop/sidecar/core_bridge_store_v1.py` implements
`DesktopCoreBridgePersistence` in a dedicated private state root. It does not
extend the public Local API resource schema. The root is a stable owner-held
mode-`0700` directory; the SQLite database and process-owner lock are no-follow,
link-count-one mode-`0600` files with pinned device/inode identities. A
nonblocking cross-process `flock` and process-local reentrant transaction lock
make one connection the only writer/reader owner. Forked children reject the
inherited store and do not explicitly unlock the parent's lease.

The database is opened no-follow relative to the held root and remains pinned by
that FD. Linux SQLite receives a `file:/dev/fd/<fd>?mode=rw` URI. Darwin SQLite
instead receives the managed database pathname: its native VFS derives rollback
journal names from the supplied database name, and `/dev/fd/<fd>` would derive
the unusable `/dev/fd/<fd>-journal` rather than a journal beside the database.
Before SQLite receives either target, the release runtime opens a separate new
in-memory connection and requires the current SQLite library's default numeric
`PRAGMA synchronous` value to be `FULL`. The target connection must report the
same default immediately after connect and must retain `FULL` after explicit
configuration. A non-`FULL` library default fails before SQLite opens or
recovers the target database, so hot-journal rollback does not depend on a
host's incidental SQLite build default. Before any SQLite configuration or
schema write, the store requires the connection-reported database inode, held
descriptor inode, and managed pathname inode to equal the original pin. A
pathname replacement at the `sqlite3.connect` boundary therefore opens the
pinned Linux inode or opens a different Darwin inode that is rejected before
initialization. Darwin accepts an OS-canonical ancestor alias only when all
three references identify that same inode.

This owner-private persistence boundary detects accidental replacement and
cooperating-process conflicts. The unsigned preview does not claim isolation
from an arbitrary malicious process running as the same UID and able to perform
and restore pathname replacement between validation points; that requires a
platform credential boundary outside the store.

SQLite private schema v3 uses DELETE journaling and `synchronous=FULL`; WAL and SHM are
forbidden. The store enforces 1-GiB database and 2-GiB journal limits, exact
schema rows plus a bound schema-fingerprint metadata row, SQLite integrity and
foreign-key checks, and canonical authority-graph recovery. Recovery admits at
most 120,000 rows and 512 MiB. Each document is capped at 4 MiB and is fetched
only after SQL byte-length probes permit an exact-length guarded read. Mapping
history is ordered and contiguous from generation one through the current row,
with a default global cap of 100,000 versions.

Persistence is explicit closed canonical JSON, never pickle. Every row stores a
SHA-256 of its exact bytes. Decode validates exact object keys, strict Core DTOs,
all bridge dataclass invariants, indexed scalar agreement, canonical request and
outcome digests, and byte-identical reserialization. The serializer has no
field for bearer secrets, credentials, environment, commands, URIs, or host
paths. Full-row create/patch/mapping CAS rejects lost updates. Mapping history
append, current mapping replacement, and exact matching applied-patch deletion
share one transaction; rollback preserves the prior mapping and patch. An exact
retry recognizes the fully committed state after an ambiguous commit without
adding another history row, but fails if a later pending patch now owns the
project transition.

Fresh identity bootstrap uses an explicit database `pending` to `bound`
protocol. Schema, the generation-zero store identity, and empty authority are
committed first. The inner identity marker and parent anchor are then published
and verified before the database identity becomes `bound`. Restart may complete
that sequence only when the pending row names the exact database, lock, root,
marker and anchor inodes, both authority digests equal the canonical empty
store, generation is zero, and all authority tables are empty. Every
`store_identity` read first retrieves only SQL types and byte lengths, then uses
closed `CASE` guards before returning bounded values to Python. Before either
pending authority digest, count/length-only aggregate queries must prove all
four authority tables empty within the shared row and byte recovery limits; an
over-capacity corrupt pending store fails without selecting authority rows.
Either marker
may be empty, contain that exact pending identity, or have a torn first-slot
publication while its never-used inactive slot remains entirely zero. Only this
exact pending state may rewrite an invalid primary slot; a valid different
marker or a dirty inactive slot fails closed. A bound store requires both valid
markers and never interprets marker corruption as an unpublished bootstrap.
Unknown entries in a fresh dedicated state root, unrecognized schemas, nonempty
pending authority, or mismatched marker identity fail closed instead of being
claimed as a new store.

The pre-connect database size is not fresh authority because opening SQLite may
first roll back a hot journal left by an uncommitted initial schema transaction.
Fresh eligibility is recomputed after that recovery through the inode-pinned
connection: the held FD must be zero bytes, while the connection reports zero
pages, `user_version=0`, and an empty `sqlite_schema`, with both durable markers
still unpublished. A rollback that restores those exact conditions may start
the generation-zero protocol. Failed or nonempty recovery, a physically
nonempty database with an apparently empty schema, and any published or foreign
marker remain ineligible and fail closed.

## Session Ownership

One `DesktopCoreBridgeV1` owns at most one candidate or active generation. A
generation token owns every client, tunnel, archive context, and unfinished
blocking-adapter future created for that candidate. Every Core call and every
host/tunnel/archive/persistence callback enters the token's external-call gate,
checks generation and deadline before and after the call, and cannot overlap
successful retirement. The strict client supplies the inner HTTP/SSE response
and cache delivery barrier.

The published session and `CoreActivationV1` retain a non-secret Local binding:
the Local project ID, profile ID, saved Local ETag, and SHA-256 of the canonical
mapped `ProjectCreateV1` intent. The activation also carries its bridge
generation and a process-local authority whose object identity can be produced
only by that activation path. Bridge capabilities, project validation, and run
creation accept the complete saved Local `ProjectV1`, not a project ID.
They recompute and compare that binding after acquiring the active generation's
external lease. Every following Core transport re-enters the same token gate,
so cancellation between the comparison and transport fails with
`active_project_session_superseded` without sending the request. A different
project ID fails with `active_project_mismatch`; profile, ETag, or mapped-intent
drift fails with `active_local_project_version_mismatch`. Both are typed 409
errors raised before Core transport.

The Local provider publishes activation state in a separate durable
transaction, so the resulting `ProjectV1` has a new Local ETag and a
`RemoteProjectStateV1`. It must then call `commit_local_activation()` with that
exact object and the `CoreActivationV1` that authorized the durable transaction.
The bridge verifies the activation generation and unforgeable authority before
the Local ID, profile, mapped intent, active state, Core project ID, active
revision, registry digest, model preparation, and Core ETag. Under the shared
transition lock it performs one CAS from the activation's original Local ETag
to the complete committed project. A retry is accepted only when the complete
`ProjectV1` equals the first committed result. A late activation, altered
source ETag or authority, second committed result, changed intent, or different
Core projection fails closed. This is a post-commit acknowledgement, not a
general ETag bypass.

The inexpensive project/profile/Local-ETag comparison precedes canonical
mapping. If an otherwise valid Local model cannot satisfy the narrower Core
mapping contract, including archive declaration invariants, the mapper and all
public bridge methods return a closed `invalid_local_project` 422 instead of a
Pydantic exception.

Every config-dependent capability, validation, and run call also rereads the
Core project before using it. The active session retains the completed durable
mapping: its complete immutable projection and project/task/workspace content
snapshots remain fixed. The refresh compares Core project intent through the
same canonical project-identity helper used by activation and compares Core
project ID, canonical intent, task snapshot, and `created_at` through the shared
immutable authority validator. The last validated session project is
the mutable predecessor. It may remain byte-for-byte equal or advance by one
revision generation with a new ETag and strictly newer `updated_at`; only the
active revision, matching registry digest, ETag, and timestamp may differ.
Status, model preparation, publication, and content snapshots cannot drift.
The accepted successor becomes the next in-session predecessor, allowing a
fully observed direct-successor chain. Config changes cannot be legitimized by
an otherwise valid successor revision, and same-revision mutable changes fail
closed before Core validation or run mutation.

Activation, switch, and close are serialized. A switch first cancels and fully
retires the previous candidate or active token; no new host/Core work starts
until its clients, adapter work, archive contexts, and tunnel are closed. A
successful `close()` therefore proves that the old generation can perform no
later capability, create, upload, validation, or persistence work. A failed or
timed-out cleanup does not mark the bridge or tunnel closed and prevents a new
session from publishing. Cleanup is observable through a typed retryable error
and tunnel `close_failure`; calling close or activation again retries ownership
of the same close operation. If the close future completes at the timeout
boundary, the bridge consumes its actual result: success closes the handle once,
while only an actual callback exception clears the future for a new callback
attempt. Deadline expiry while computing the wait immediately after submission
also retains that future; retry waits for the same callback instead of invoking
it twice. Activation acknowledgement enters the same transition serialization:
an acknowledgement already inside the lock commits before retirement, while a
deactivate, close, or replacement activation that wins the lock invalidates the
old generation before its acknowledgement can inspect or mutate active state.

An active project config edit uses `deactivate_project()` rather than closing
the bridge permanently. The transition rejects a different Local project and
an in-flight candidate, retires all generation resources through the same
bounded path, and leaves the bridge available for later activation of the new
draft intent.

The forward activation path uses one finite wall-clock deadline across host
attach, tunnel open, version negotiation, capabilities, project create/read,
workspace publication, revision-head read, validation, persistence, and
publication. Failed-candidate retirement receives a separate bounded cleanup
window so resource ownership is not abandoned when the forward deadline
expires.

Each release activation also binds the exact Local operation ID to a cancellation
event on its candidate token. Cancelling another ID cannot affect it. Cancelling
the owner sets that event, cancels queued adapter futures, requests Core client
and tunnel closure, and disconnects the lifecycle transport outside provider
locks. The transport sends termination to its owned SSH subprocess groups and
rejects their late results. The serialized project executor releases the worker
after a bounded join, so retry and provider shutdown do not wait for the original
300-second activation deadline. A late adapter success or failure is gated by
the cancelled generation and cannot publish mapping, activation, or online state.

Potentially blocking Python adapters execute on a fixed-size, bounded daemon
executor. Deadline expiry stops delivery immediately; unfinished work remains
owned by the cancelled generation, and successful close/switch waits for it.
Retirement itself is bounded and fails closed if that work does not finish, so
the bridge never converts an unbounded callback into a false successful close.
Resources returned after the original deadline are adopted before their future
completes and are closed during retirement.

## Deterministic Project Mapping

Local project fields map as follows:

| Desktop Local v1 | Core Control v1 |
| --- | --- |
| name and task | `ProjectCreateV1.name` and closed `TaskSpecV1` |
| `codex_subscription_transcript` | Codex harness, transcript capture, selected Codex model |
| `self-deployed` | Codex harness, transcript capture, exact Hugging Face model ref |
| `evolution.targets` | exact closed Core evolution target map |
| scratch source | Core scratch workspace with signed empty snapshot |
| native folder source | archive declaration derived from opaque adopted ref |

The local project ID, profile ID, import ID, host path, command, credential
reference, and bearer are not fields in Core `ProjectCreateV1`. Archive bytes
are re-counted and re-hashed while streaming. Upload create, each fixed chunk,
and finalize use deterministic sub-keys bound to the Core project snapshot. A
persisted upload ID is reused only for that exact snapshot, project ETag,
archive declaration, and base workspace snapshot. A changed imported workspace
therefore gets a new upload instead of reusing the prior version's session.

For an existing mapping, Desktop first rereads the exact Core project. Unchanged
intent must match the stored canonical request, content snapshots, and complete
immutable projection, including exact `created_at`. The mapping's complete
mutable authority must be identical when the active revision is unchanged.
Cross-session activation may accept one direct revision successor without
changing Local intent: it must issue a new project ETag, strictly increase
`updated_at`, preserve status, publication, model preparation, task snapshot,
and the complete immutable projection, and may update the active revision,
project/workspace snapshots, and registry as one successor publication.
Desktop persists the complete Project/Head/Revision closure, including the
canonical revision-manifest digest and activation timestamps, before replacing
the durable mapping. A head that already advertises another successor does not
invalidate that active-revision proof, but it blocks run admission until Core
resolves the advertised successor.

Changed name, task, model/execution, evolution config, or workspace is sent
through frozen Core `patch_project` with the freshly read Core ETag and a
deterministic key derived from old and new canonical request digests. Core must
return a new project snapshot and ETag, plus a new task/workspace snapshot state
when those inputs changed, while retaining the exact Core project ID and
`created_at`. Replacing one unpublished imported draft with another may legally
keep the workspace snapshot `null`; the new project snapshot and ETag version
that draft transition. A Core reread validates ownership but is never proof of
which request produced observed content. Every unknown patch replays the
durable canonical request with its original ETag and key until the exact response
is persisted. Mapping CAS occurs only after workspace publication, readiness,
revision-head agreement, and Core validation, preserving the prior mapping as
traceable history until then. The mapping commit and applied-patch cleanup are
one transaction. Authority-only CAS versions increment the mapping generation
even though their predecessor request digest equals the current request digest.

## Run And Resource Proxy

The renderer-facing run route supplies only the active local project ID and
idempotency key. The release provider atomically loads the saved `ProjectV1`
selected by the route's Local ETag and passes that complete object to the
bridge. The bridge verifies its activation binding, then rereads
the Core project, pinned capabilities, validation, and revision head. A
revision head with any successor state, including failed, cancelled, or
unavailable, blocks a new run. Otherwise the active head is required. The
bridge then constructs Core `RunCreateV1` from the authoritative
project/task/workspace snapshot refs and registry digest. Core repeats the
no-successor check while atomically pinning project/revision authority through
run persistence, so a Desktop read cannot race successor publication. A
Core-only direct revision successor may change Core ETag, revision authority,
and Core-owned project/workspace snapshots without requiring a new Local ETag
because it does not change the saved Local intent.

Run list/get/cancel/retry/timeline/log/context, artifacts, services, Core
operations and referenced logs, diagnostics, maintenance, and events have
strict `CoreControlClientV1` bridge methods. Every method accepts the complete
saved Local `ProjectV1` and verifies it against the active generation before
transport, but bridge availability alone does not make a release feature. The
current release provider exposes read-only services and omits diagnostic,
maintenance, and service-mutation handlers because their Core owners return
typed unavailable responses. The provider holds its project-session transition lock from
that Local lookup through result delivery, so an active-project edit cannot
retire and replace the session while an old request is being returned. Core DTOs
are returned unchanged. The strict client
continues to enforce project membership, private-value scanning, bounded
responses, ETags, idempotency, and release contract pins. No public bridge
method exposes `CoreClientErrorV1`: an exact Core `ApiErrorV1`, including HTTP
503, is retained inside `DesktopCoreBridgeErrorV1`, while a strict-client local
error is converted to a closed user-safe `ApiErrorV1`. The same rule covers
deferred SSE iteration. The bridge does not synthesize readiness.

## Release Wiring Status

Packaged startup composes `DesktopCoreSshBridgeAdapterV1`,
`DesktopCoreBridgeStoreV1`, `DesktopCoreBridgeV1`,
`DesktopEventBrokerV1`, and the Core event relay through the single owner in
`release_runtime.py`. The exact embedded wheel/framework-lock pair is verified
before construction, and the dedicated bridge state is rooted under the private
Desktop provider state. The provider advertises exactly `remote_profiles`,
`project_validation`, `operation_events`, `run_observability`, and
`artifact_inspection` when both the owned bridge and broker are present. It does
not advertise `service_control` or `diagnostics`. A missing or invalid
asset pair, bridge, broker, or active project fails closed; there is no reduced
release composition or direct-backend fallback.

The relay opens Core SSE with the complete active Local project binding. It
advances the Core cursor but drops heartbeat frames and publishes one complete
Desktop state invalidation for every other validated frame. Core remains the
authority for event payload and resource state; Desktop neither projects those
payloads into a second event model nor persists a second Core event log.

The bridge remains covered across process restart, scratch/imported workspace
activation, finalize recovery, unknown abort replay, production runtime
composition, and provider routing. Adapter protocol tests use controlled SSH and
HTTP transports; real remote GPU/Codex subscription evidence is a separate
release E2E gate rather than an excuse for a runtime fallback.
