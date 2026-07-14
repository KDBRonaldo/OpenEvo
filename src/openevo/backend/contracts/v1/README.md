# Core Control API v1 Contract

This directory is the canonical, schema-only contract between the Desktop
sidecar and remote OpenEvo Core. It is not a business provider: every route in
`app.py` returns HTTP 501.

## Sources And Snapshots

- `models.py` owns strict, frozen Pydantic request, response, and SSE models.
- `app.py` owns the exact HTTP surface, headers, status codes, and OpenAPI
  metadata.
- `openapi.json` and `events.schema.json` are canonical generated snapshots.
- `tests/backend/test_contract_v1.py` owns contract behavior, malicious-input,
  closure, and snapshot digest tests.

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

See `docs/architecture/desktop-core-contract-v1.md` for the product boundary and
provider requirements. Do not use this schema-only module as evidence that a
production provider or cross-session activation path is implemented.
