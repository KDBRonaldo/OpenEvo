# Core Control API v1 Contract

This directory owns the canonical contract between the Desktop sidecar and
remote OpenEvo Core, plus the phase-one business provider. Calling
`create_core_control_contract_app()` without a provider remains schema-only and
returns HTTP 501. `create_core_control_app()` binds the real provider to those
same routes by canonical operation ID without changing the snapshots.

## Sources And Snapshots

- `models.py` owns strict, frozen Pydantic request, response, and SSE models.
- `app.py` owns the exact HTTP surface, headers, status codes, and OpenAPI
  metadata.
- `openapi.json` and `events.schema.json` are canonical generated snapshots.
- `tests/backend/test_contract_v1.py` owns contract behavior, malicious-input,
  closure, and snapshot digest tests.
- `provider.py`, `store.py`, and `workspace.py` implement the phase-one provider;
  `docs/architecture/core-control-v1-provider.md` records endpoint ownership and
  explicit fail-closed gaps.

Regenerate snapshots only after model and route tests pass:

```bash
.venv/bin/python -c \
  'from openevo.backend.contracts.v1.snapshots import write_contract_snapshots; write_contract_snapshots()'
```

Then update the two expected SHA-256 values in the contract test and run the
full backend contract and evolution framework suites.

## Required Invariants

All objects are closed and coercion-free. Mutable resources use strong SHA-256
ETags. Imported workspace requests declare archive digest, size, and frozen
deterministic ustar policy without inventing a Core content ID. Upload creation
binds a project snapshot and ETag; finalization compares upload and project
ETags and returns one `WorkspacePublicationV1` that atomically binds the archive
declaration, newly issued content, workspace snapshot, and persisted project
state. The finalize body carries only the archive digest; provider conformance
requires `If-Project-Match == upload.project_etag == current project.etag` and
requires the upload's frozen project snapshot to remain current.

Runs preserve exact required-revision reachability, explicit `admitted_at`,
nullable pre-admission pins, and at most 100 ordered attempts. Environment
repair, service restart, and cache cleanup use the recoverable `OperationV1`
resource with typed request/result, logs, ETag, and a kind-bound cancellation
descriptor. Only cancellable kinds may enter `cancelling|cancelled`; unsupported
cancel requests return `409 ApiErrorV1` with code
`operation_kind_not_cancellable`; the same response also admits the global
`idempotency_key_reused` conflict. Every page satisfies `has_more` if and only if
`next_cursor` is present.
When a `CoreRunControl` is injected, its operation-ID set must exactly equal all
frozen `/v1/runs*` routes, including `listCoreRunArtifactsV1`. Retryable errors
from that owner are not persisted as failed idempotency results, so an exact
same-key mutation retry calls the owner again; non-retryable failures retain the
existing replay policy.

`SseFrameV1` freezes matching wire `id`, `event`, and typed `data`; every
non-heartbeat envelope binds its change resource identity, ETag or digest, and
applicable parent identity. A frame ID is a stream record cursor, while
`change_id` remains stable for the same logical mutation across replay or
re-emission. Artifact diffs use an `added|removed|modified|renamed` document
change union and retain document identity on every hunk.
`revision.activated.v1` accepts only an active `RevisionV1`; non-genesis
transition payloads retain the revision model's exact predecessor/successor
closure and active transition state.
`CapabilitiesResponseV1` remains the framework-owned `EvolutionCapabilitiesV1`
type without a local copy or projection.

The provider's private SQLite schema is exact-fingerprinted and is not part of
the frozen HTTP schema. The exact preceding bound schemas are upgraded
transactionally by adding the revision ledger or its private activation-request
binding; near matches and older state-bearing schemas fail closed. New revision
rows sign the canonical activation request digest and add a private binding to
the owning idempotency row. That binding survives project deletion for the
idempotency retention window and is cascade-cleaned when the response expires.
Legacy revision rows retain their old identity until a still-retained
activation response is validated and atomically backfills both bindings.
Startup guarded reads and ordinary write transactions use one closed
recovery-table accounting specification. Every transaction rechecks its final
row count, bounded TEXT/BLOB byte count, and per-value lengths before `COMMIT`,
including revision, activation-binding, event, and idempotency rows written by
the same request. Startup recovery performs the same check again after legacy
request-binding backfill, so an over-budget backfill rolls back rather than
publishing state that the next startup must reject.
Successful idempotency rows retain the
canonical request and semantic headers, validate each operation's
request/response relationship, and require a ready project response to name the
exact ledger revision activated by that same request during replay and startup.
Project validation
constructs its framework profile from the persisted execution mode, capture
mode, and harness ID. SQLite/workspace recovery is descriptor-bound and
quota-limited across every persisted TEXT/BLOB value. The Linux provider opens
SQLite through its held `/proc/self/fd` authority path and verifies the resolved
managed path around connection setup. It fixes any pre-existing rollback
journal by no-follow FD before that connection and accepts recovery only when
the same inode remains canonically bound or SQLite consumes that inode without a
replacement pathname. Fresh, legacy, and pending identity binds hold both
managed-root FDs across the initial inventory, durable identity-marker
publication, and the final pre-bound inventory; a new node or root replacement
fails before cleanup. Workspace publication ownership is unique
to one project/upload pair and is enforced by a signed, transactionally inserted
private owner row. Abort/delete success persists cleanup intents; exact replay
and startup must converge them before dropping those intents. Linux cleanup uses random
atomic no-replace quarantine names, revalidates the first observed inode after
rename, and applies one cumulative recovery budget before quota is checked over
live owned entries only.

Verified Codex subscription projects with a ready workspace atomically publish
generation-zero active `RevisionV1`; ready project PATCH and final workspace
publication atomically publish one direct successor while retaining history.
The private ledger authenticates canonical manifests, enforces contiguous
predecessors, requires every successor timestamp to be strictly later than its
predecessor even when the wall clock stalls or moves backward, and validates
active ProjectV1 head closure during bounded startup recovery. It backs the
frozen revision list/head/get routes with signed cursors and strong ETags.
Missing registry, unresolved self-deployed model preparation, or an unpublished
imported workspace remains draft and does not synthesize a revision. Project
and revision activation events commit with the mutation and idempotency
response. Replay pruning retains complete project-update/revision-activation
pairs; startup requires a contiguous retained event suffix and verifies each
pair's order, generation, payload, timestamps, change identity, and exact
revision-ledger binding.
Synchronous store work runs on a bounded executor rather than the ASGI event
loop.

See `docs/architecture/desktop-core-contract-v1.md` for the product boundary and
`docs/architecture/core-control-v1-provider.md` for implemented ownership. The
provider does not implement the run owner, evolution-produced queued successor
orchestration, serving preparation, service restart, diagnostics, or artifact
exhibition paths; those routes fail closed.
