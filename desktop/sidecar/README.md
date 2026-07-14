# Desktop Sidecar

The sidecar owns the renderer-facing Desktop Local API and the process-owned
connection to remote OpenEvo Core. The canonical public contract is defined once
in `contracts/v1/app.py`; release implementations must use its provider injection
point instead of registering another route table.

## Release Local Provider

`release_app.create_release_desktop_local_api_app()` creates the real Local API v1
application. It owns one `DesktopProviderStore` for the process lifetime and
requires the native host to supply a Desktop session token, native instance ID,
readiness key, source commit, and private state root.

The current provider implements:

- public `GET /version` with `provider_kind=desktop_sidecar` and the canonical
  OpenAPI digest;
- challenge-bound `GET /health` using HMAC-SHA256 over
  `protocol NUL instance_id NUL challenge`;
- constant-time Desktop session authentication for every `/desktop/v1/*` route;
- `GET /desktop/v1/state` with the process-owned SSH/Core lifecycle state;
- profile and project list/create/get/patch/delete through
  `DesktopProviderStore`, including durable idempotency, signed cursors, ETags,
  and restart recovery;
- profile connect/disconnect plus explicit SSH host-key review and acceptance.
  The sidecar probes without trusting, repeats the probe before confirmation,
  gives credential resolution, trust-store load/probe/confirmation, transport
  construction, and the trusted SSH check one shared 12-second deadline,
  stores only the confirmed fingerprint in Local API resources, and owns the
  trusted known-host file under its private state root. Unconfirmed candidates
  remain only in the process-owned review state and restart recovery removes any
  candidate persisted by an older interrupted implementation.

Connection mutations atomically reserve idempotency capacity, two fixed terminal
response slots for the operation and idempotency documents, profile action
ownership, and a running operation before external SSH work. One process-wide
action lock serializes that full reservation, SSH invocation, and finalization
cycle across every profile, route, and idempotency key. Replacing profile A with
B therefore closes and durably disconnects A before invoking B. Disconnect is
non-displacing: its reservation does not publish `connecting` or alter another
profile, and the sidecar rejects a profile that does not own the process
lifecycle before calling the transport. Success, error, and recovery
cancellation finalize within the reserved slots without another capacity or
request-ETag check. If completion reports an error before commit, the running
reservation retains its terminal capacity until failure is durable. If commit
succeeded before returning an error, the frozen success remains authoritative
and its transport stays open even if concurrent CRUD consumed the released
capacity. Failure finalization resolves the same return ambiguity with a
read-only observation bound to the exact idempotency envelope and reserved
operation. It retries only a proven `running` state. A durable failed operation
authorizes cleanup only while the profile remains durably disconnected and the
process transport still has that profile as owner; exact failed replay repeats
this check so interrupted cleanup converges without closing another owner's
transport. Failed operations retain their bounded `ApiErrorV1`, so exact replays
return the same error and do not repeat remote work. Once any operation is
terminal, its body and ETag are immutable; a late complete/fail call only returns
that terminal and may close the transport owned by its own stale result. Restart
only cancels truly nonterminal reservations, updating their operation and
idempotency documents in the same recovery transaction.
Profile deletion checks for queued, running, or cancelling profile operations in
the same write transaction as the delete, so even a non-displacing disconnect on
an already-disconnected profile retains its resource authority through terminal
publication. Terminal historical operations do not prevent later deletion.

The production credential resolver currently supports `ssh_agent`. Profiles
that select native private-key or password authentication fail closed with
`ssh_credential_unavailable` until the Tauri credential broker supplies an
ephemeral `SSHAuthConfig`; credential values must never enter the Local API or
provider store. Profile proxy URLs and `no_proxy` are projected into the remote
profile, but user information in proxy URLs is rejected by the contract.

Core bootstrap/tunnel operations, activation, validation, runs, artifacts,
services, diagnostics, maintenance, and events remain unavailable in this
provider slice and return a closed `ApiErrorV1` with HTTP 503. They never return
fixture data or a synthetic ready/success state. A successful SSH check reports
Core as `offline` with `core_not_started`; it does not claim a live tunnel.

## Provider Extension

`DesktopLocalApiProviderV1.invoke()` receives the canonical OpenAPI
`operation_id` and the already validated endpoint arguments. The release
provider has a small handler map only for implemented operations; unknown
operations fail closed. Later SSH and Core providers should add verified
handlers behind this interface while keeping the decorators and signatures in
`contracts/v1/app.py` authoritative.

Provider and request-validation failures are normalized by `release_app.py`.
Error responses must remain user-safe: do not include local paths, SQLite
messages, credentials, session tokens, remote commands, or backend URLs.
