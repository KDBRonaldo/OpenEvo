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
project/task/workspace content snapshots, a complete mutable authority
projection (status, project/workspace snapshots, publication, revision,
registry, model preparation, timestamp, and ETag), monotonic mapping generation,
and predecessor request digest. The scalar snapshot/revision/registry/ETag/time
indexes must exactly mirror that projection. Mapping commit receives the
complete expected prior mapping; a durable adapter must retain the ordered audit
history and reject lost updates. Every load recomputes the canonical request
digest and validates the projection binding before Core transport.

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
mapped `ProjectCreateV1` intent. Bridge capabilities, project validation, and
run creation accept the complete saved Local `ProjectV1`, not a project ID.
They recompute and compare that binding after acquiring the active generation's
external lease. Every following Core transport re-enters the same token gate,
so cancellation between the comparison and transport fails with
`active_project_session_superseded` without sending the request. A different
project ID fails with `active_project_mismatch`; profile, ETag, or mapped-intent
drift fails with `active_local_project_version_mismatch`. Both are typed 409
errors raised before Core transport.

The inexpensive project/profile/Local-ETag comparison precedes canonical
mapping. If an otherwise valid Local model cannot satisfy the narrower Core
mapping contract, including archive declaration invariants, the mapper and all
public bridge methods return a closed `invalid_local_project` 422 instead of a
Pydantic exception.

Every config-dependent capability, validation, and run call also rereads the
Core project before using it. The active session retains the completed durable
mapping: its canonical `ProjectCreateV1` and project/task/workspace content
snapshots remain fixed. The refresh compares Core project intent through the
same canonical project-identity helper used by activation and compares the
complete immutable authority projection. The last validated session project is
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
it twice.

The forward activation path uses one finite wall-clock deadline across host
attach, tunnel open, version negotiation, capabilities, project create/read,
workspace publication, revision-head read, validation, persistence, and
publication. Failed-candidate retirement receives a separate bounded cleanup
window so resource ownership is not abandoned when the forward deadline
expires.

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
intent must match the stored canonical request and immutable content snapshots.
The mapping's complete mutable authority must be identical when the active
revision is unchanged. Cross-session activation may accept one direct revision
successor without changing Local intent: it must issue a new project ETag,
strictly increase `updated_at`, preserve status, snapshots, publication, and
model preparation, and may update only active revision and registry alongside
that ETag/time pair. Desktop accepts and CAS versions that authority only after
capabilities, project readiness, revision-head agreement, and Core validation
all succeed.

Changed name, task, model/execution, evolution config, or workspace is sent
through frozen Core `patch_project` with the freshly read Core ETag and a
deterministic key derived from old and new canonical request digests. Core must
return a new project snapshot and ETag, plus a new task/workspace snapshot state
when those inputs changed. Replacing one unpublished imported draft with another
may legally keep the workspace snapshot `null`; the new project snapshot and
ETag version that draft transition. A Core reread validates ownership but is never proof
of which request produced observed content. Every unknown patch replays the
durable canonical request with its original ETag and key until the exact response
is persisted. Mapping CAS occurs only after workspace publication, readiness,
revision-head agreement, and Core validation, preserving the prior mapping as
traceable history until then. The mapping commit and applied-patch cleanup are
one transaction. Authority-only CAS versions increment the mapping generation
even though their predecessor request digest equals the current request digest.

## Run And Resource Proxy

The renderer-facing run route still supplies only the active local project ID
and idempotency key. Its future release-routing adapter must atomically load the
saved `ProjectV1` selected by the route's Local ETag and pass that complete
object to the bridge. The bridge verifies its activation binding, then rereads
the Core project, pinned capabilities, validation, and revision head. A
reachable successor whose transition is not failed, cancelled, or unavailable
is required; otherwise the active head is required. The bridge then constructs
Core `RunCreateV1` from the authoritative project/task/workspace snapshot refs
and registry digest. A Core-only direct revision successor may change Core ETag
and revision authority without requiring a new Local ETag because it does not
change the saved Local binding.

Run list/get/cancel/retry/timeline/log/context, artifacts, services, Core
operations and referenced logs, diagnostics, maintenance, and events delegate
to `CoreControlClientV1`. Core DTOs are returned unchanged. The strict client
continues to enforce project membership, private-value scanning, bounded
responses, ETags, idempotency, and release contract pins. No public bridge
method exposes `CoreClientErrorV1`: an exact Core `ApiErrorV1`, including HTTP
503, is retained inside `DesktopCoreBridgeErrorV1`, while a strict-client local
error is converted to a closed user-safe `ApiErrorV1`. The same rule covers
deferred SSE iteration. The bridge does not synthesize readiness.

## Release Wiring Status

The bridge is tested with fake host/tunnel/archive/persistence adapters and a
real strict client over `httpx.MockTransport`. `DesktopProviderStore` does not
yet implement the persistence protocol, and `DesktopReleaseProvider` does not
yet own production host/tunnel/archive adapters. No release feature flag is
enabled by this module. Until those adapters and Local API operation wiring are
implemented and tested, the corresponding release provider routes continue to
return typed HTTP 503.
