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
- `DesktopCoreBridgePersistence` durably reserves exact create intent, records
  upload progress identity, and atomically commits the local-to-Core mapping.

The persisted create operation binds local project, profile, Core host
identity, canonical Core `ProjectCreateV1` digest, idempotency key, returned
Core project ID, and workspace upload ID. The completed mapping additionally
binds the exact project/task/workspace snapshot refs and registry digest.
Unknown create outcomes are retried with the same canonical request and key;
changed intent fails before HTTP transport.

## Session Ownership

One `DesktopCoreBridgeV1` owns at most one `DesktopCoreActiveSessionV1`. The
session owns one tunnel and one project-bound `CoreControlClientV1`. Activation,
switch, and close advance a bridge generation. Calls snapshot the active session
and generation before invoking Core and check both again before returning. The
strict client supplies the inner response/cache delivery barrier; sealing it
prevents an old HTTP or SSE result from committing after a switch.

An activation uses one finite wall-clock deadline across host attach, tunnel
open, version negotiation, capabilities, project create/read, workspace
publication, revision-head read, validation, persistence, and publication. A
candidate that misses its generation or deadline is closed and never becomes
active.

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
and finalize use deterministic sub-keys; a persisted upload ID permits recovery
from an unknown chunk/finalize outcome through Core's authoritative upload and
project state.

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
