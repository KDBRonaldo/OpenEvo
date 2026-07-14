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
- `DesktopCoreBridgePersistence` durably transitions project create ownership,
  records snapshot-bound upload identity, and compare-and-swaps the
  local-to-Core mapping while retaining adapter-owned history.

The persisted create operation binds local project, profile, Core host
identity, the full canonical Core `ProjectCreateV1`, its digest, idempotency
key, returned Core project ID, and workspace upload ID plus its owning project
snapshot. Its state is explicit:

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

The completed mapping also stores the canonical mapped request, exact
project/task/workspace content snapshots, project ETag, active revision,
project `updated_at`, registry digest, monotonic mapping generation, and
predecessor request digest. Mapping commit receives the complete expected prior
mapping; a durable adapter must retain the ordered audit history and reject lost
updates. Every load recomputes the canonical request digest before Core
transport.

## Session Ownership

One `DesktopCoreBridgeV1` owns at most one candidate or active generation. A
generation token owns every client, tunnel, archive context, and unfinished
blocking-adapter future created for that candidate. Every Core call and every
host/tunnel/archive/persistence callback enters the token's external-call gate,
checks generation and deadline before and after the call, and cannot overlap
successful retirement. The strict client supplies the inner HTTP/SSE response
and cache delivery barrier.

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
attempt.

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
Project ETag, active revision, `updated_at`, and registry digest are mutable Core
authority: cross-session successor activation may legitimately update them
without changing Local intent or content snapshots. Desktop accepts and CAS
versions that authority only after capabilities, project readiness,
revision-head agreement, and Core validation all succeed.

Changed name, task, model/execution, evolution config, or workspace is sent
through frozen Core `patch_project` with the freshly read Core ETag and a
deterministic key derived from old and new canonical request digests. Core must
return a new project snapshot and ETag, plus a new task/workspace snapshot state
when those inputs changed. Unknown patch outcomes are recovered by rereading
Core; if the patch did not apply, retry uses the exact same key. Mapping CAS
occurs only after workspace publication, readiness, revision-head agreement,
and Core validation, preserving the prior mapping as traceable history until
then. Authority-only CAS versions increment the mapping generation even though
their predecessor request digest equals the current request digest.

## Run And Resource Proxy

Local run creation supplies only the active local project ID and idempotency
key. The bridge rereads the Core project, pinned capabilities, validation, and
revision head. A reachable successor whose transition is not failed, cancelled,
or unavailable is required; otherwise the active head is required. The bridge
then constructs Core `RunCreateV1` from the authoritative project/task/workspace
snapshot refs and registry digest.

Run list/get/cancel/retry/timeline/log/context, artifacts, services, Core
operations and referenced logs, diagnostics, maintenance, and events delegate
to `CoreControlClientV1`. Core DTOs are returned unchanged. The strict client
continues to enforce project membership, private-value scanning, bounded
responses, ETags, idempotency, and release contract pins. Core HTTP 503 errors
remain the exact typed Core error; the bridge does not synthesize readiness.

## Release Wiring Status

The bridge is tested with fake host/tunnel/archive/persistence adapters and a
real strict client over `httpx.MockTransport`. `DesktopProviderStore` does not
yet implement the persistence protocol, and `DesktopReleaseProvider` does not
yet own production host/tunnel/archive adapters. No release feature flag is
enabled by this module. Until those adapters and Local API operation wiring are
implemented and tested, the corresponding release provider routes continue to
return typed HTTP 503.
