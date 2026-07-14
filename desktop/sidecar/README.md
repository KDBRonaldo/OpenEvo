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

Connection mutations atomically reserve idempotency capacity, profile action
ownership, and a running operation before external SSH work. Replacing profile A
with B durably disconnects A, cancels A's obsolete operation, closes A's
transport before resolving B's credential, and records B as the current failed
owner if synchronous resolution fails. Success or failure finalizes the reserved
operation without another capacity or request-ETag check. A final persistence
error closes the successful transport and compensates the reservation to
failed/disconnected, including the case where SQLite committed before reporting
an error. Failed operations retain their bounded `ApiErrorV1`, so exact replays
return the same error and do not probe, accept, connect, or disconnect again.
Successful connect, host-key accept, and disconnect responses carry the
operation ETag in both the frozen body and `ETag` header.

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
