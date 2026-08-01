# OpenEvo Desktop And Core Contract v2

Status: active implementation contract for the 0.1.10 lifecycle-authority release

Canonical product and release acceptance remain defined by
`docs/maintainer/productization/spec.md`. This document fixes the implementation
boundary for Desktop Local API v2 and Core Control API v2. It does not make an
unimplemented capability available and does not reduce a canonical gate.

The frozen v1 contract in `desktop-core-contract-v1.md` describes the 0.1.8
Preview. V1 is read-only migration input in 0.1.10. Release providers and the
release renderer must not fall back to v1 mutations.

## Ownership

```text
React renderer
  -> authenticated Desktop Local API /desktop/v2/*
packaged sidecar
  -> pre-Daemon system OpenSSH bootstrap
  -> active-project private SSH tunnel
  -> strict Core Control API /v2/*
remote OpenEvo Daemon
```

The Tauri host owns the sidecar process, Desktop session credential, packaged
native helper inventory, native file selection, bounded native diagnostics, and
shutdown of the complete sidecar process group.

The sidecar owns local profiles/drafts, bounded OpenSSH host discovery,
system-OpenSSH process and control-socket lifecycle, askpass authorization,
pre-Core bootstrap, the private tunnel, response validation, local projection,
and event aggregation.

The Daemon owns every authoritative project, workspace, execution, task,
attempt, transition, dataset, evolution, artifact, service, diagnostic, and
history resource.

React never receives an SSH command, SSH config or identity path, prompt
secret, secret reference, known-host path, Core URL, backend token, host path,
raw exception, or benchmark concept.

## Negotiation

Unversioned `/version` and `/health` are discovery/readiness endpoints only.
Before mutation, both local and remote sessions pin:

- API major and exact OpenAPI digest;
- event schema digest;
- Desktop/sidecar/Daemon release and build identity;
- compatible feature set;
- active project/profile connection generation;
- verified evolution registry digest and evaluated release profile.

Any identity change retires the mutation session. Release refuses simulator,
scaffold, dry-run, direct backend, source-sidecar, incompatible digest, missing
registry, and legacy route fallback.

## System OpenSSH Profile

The v2 connectable profile has the closed authority shape:

```json
{
  "name": "Lab server",
  "connection_authority": "system_openssh",
  "ssh_host_alias": "configured-alias"
}
```

The request has no host, user, port, identity path, authentication method,
password, passphrase, ProxyJump, ProxyCommand, known-host path, or resolved
command. A response may contain a bounded non-authoritative display snapshot,
catalog generation, connection state, and typed failure. It still does not
contain source paths or effective commands.

The host catalog lexically discovers bounded literal `Host` tokens and static
`Include` files as UI hints. Wildcards, negated patterns, conditional names, and
external-tool aliases are not enumerable; the user may type a bounded literal
alias. Catalog load never runs `ssh -G`, `Match exec`, ProxyCommand, or a shell.

After an explicit selection, a bounded `ssh -G` probe may provide display or
trust-review facts only when conditional execution is safely excluded. The
actual connection always uses the literal alias.

## SSH Execution And Prompt Boundary

The release sidecar runs exact `/usr/bin/ssh <alias>`. OpenSSH remains final
authority for host resolution, user, port, identity files, agent and Keychain
behavior, password/passphrase prompts, ProxyJump, ProxyCommand, canonicalization,
and known-host policy.

OpenEvo may set only reviewed process/session safety options: its own
multiplexing socket, no ambient master adoption, no local command, no
unrequested TTY or remote command, no unowned forwarding, exact intentional
Core forwarding through the owned master's stdio `-W` channel,
keepalive/deadline, and bounded cleanup. `ClearAllForwardings=yes` is not
combined with `-L`: supported macOS OpenSSH clears that explicit forward too.
It must not use
`-F /dev/null`, flatten connection values, select authentication, or replace
known-host files.

One connection generation owns one private OpenSSH master. Bootstrap commands,
uploads, and the Core tunnel reuse that master. The sidecar binds process birth,
process group, socket identity, alias, and generation and never adopts an
ambient master. Cancellation, replacement, disconnect, or app shutdown closes
and reaps the generation within a hard deadline.

After that master authenticates, the sidecar privately discovers the effective
`id` username and UID plus the single matching `getent passwd <uid>` home. It
requires the NSS name/UID to match `id`, the home to be a normalized safe
absolute path, its physical path to equal its lexical path, and the directory to
be owned by and writable for the effective UID. The bounded probe output bypasses
the lifecycle log observer and becomes a sealed, process-local authority bound
to the exact profile and connection generation. It is never a profile field,
Local API value, persisted row, event value, diagnostic, or log value.

The v2 workspace and Daemon bundle roots are derived only as
`<nss-home>/.openevo/workspaces` and
`<nss-home>/.openevo/daemon-bundles`. `/root`, `/home/<user>`, and safe custom
homes such as `/srv/research/alice` use the same path. Rich SSH commands repeat
the account/NSS/home guard before execution. The release system-OpenSSH
authority exposes no rsync follower: Daemon bundle and managed-runtime bytes
stream over the owned SSH command's stdin into bounded receivers, and legacy
generic-upload/Core-asset/runtime transfer entry points fail before any remote
mutation. The Daemon staging script independently repeats account validation
before creating owner-private directories; it pins and rechecks the
service-root identity through streaming, hashing, and no-overwrite publication.
The system-OpenSSH v2 path has no username-derived fallback and does not require
`rsync` or Python on the server. The raw Core tunnel remains the non-shell
`ssh -W` channel and does not run a home-derived command.

The separately inventoried native askpass helper accepts only a bounded prompt
from a descendant of the current owned OpenSSH generation and a single-use
sidecar authorization. It shows AppKit confirmation or secure-input controls.
Password/passphrase bytes travel only from the secure field to helper stdout
and OpenSSH. They do not enter React, Local API, sidecar, logs, diagnostics,
argv, persisted state, or an OpenEvo Keychain item.

First-host approval is delivered to the same real OpenSSH handshake. A changed
key always blocks. Replacement through `/usr/bin/ssh-keygen` is offered only
after explicit review and proof of one ordinary writable `UserKnownHostsFile`,
no `KnownHostsCommand`, and no ambiguous `HostKeyAlias` or trust source.
Otherwise Desktop returns a typed administrator action.

## Desktop Local API v2 Resources

The authenticated route families are:

```text
GET    /desktop/v2/state
GET    /desktop/v2/ssh-hosts
POST   /desktop/v2/ssh-hosts/rescan
GET    /desktop/v2/profiles
POST   /desktop/v2/profiles
GET    /desktop/v2/profiles/{profile_id}
PATCH  /desktop/v2/profiles/{profile_id}
DELETE /desktop/v2/profiles/{profile_id}
POST   /desktop/v2/profiles/{profile_id}/rebind
POST   /desktop/v2/profiles/{profile_id}/connect
POST   /desktop/v2/profiles/{profile_id}/disconnect
POST   /desktop/v2/profiles/{profile_id}/host-key/review

GET    /desktop/v2/operations/by-action
GET    /desktop/v2/operations/{operation_id}
GET    /desktop/v2/operations/{operation_id}/logs
POST   /desktop/v2/operations/{operation_id}/cancel
POST   /desktop/v2/operations/{operation_id}/acknowledge
GET    /desktop/v2/core-operations/{operation_id}
POST   /desktop/v2/core-operations/{operation_id}/cancel

GET    /desktop/v2/projects
POST   /desktop/v2/projects
GET    /desktop/v2/projects/{project_id}
PATCH  /desktop/v2/projects/{project_id}
POST   /desktop/v2/projects/{project_id}/activate
GET    /desktop/v2/projects/{project_id}/capabilities
POST   /desktop/v2/projects/{project_id}/validate

GET    /desktop/v2/tasks
POST   /desktop/v2/tasks
GET    /desktop/v2/tasks/{task_id}
POST   /desktop/v2/tasks/{task_id}/cancel
POST   /desktop/v2/tasks/{task_id}/retry
GET    /desktop/v2/tasks/{task_id}/timeline
GET    /desktop/v2/tasks/{task_id}/logs
GET    /desktop/v2/tasks/{task_id}/context
GET    /desktop/v2/tasks/{task_id}/artifacts

GET    /desktop/v2/project-heads/{project_head_id}
GET    /desktop/v2/evolution-revisions/{evolution_revision_id}
GET    /desktop/v2/runtime-contexts/{runtime_context_snapshot_id}
GET    /desktop/v2/transitions/{transition_id}
POST   /desktop/v2/transitions/{transition_id}/retry
POST   /desktop/v2/transitions/{transition_id}/replace
POST   /desktop/v2/transitions/{transition_id}/abandon

GET    /desktop/v2/artifacts/{artifact_id}
GET    /desktop/v2/artifacts/{artifact_id}/content
GET    /desktop/v2/artifacts/{artifact_id}/diff
GET    /desktop/v2/services
POST   /desktop/v2/services/{service_id}/restart
GET    /desktop/v2/services/{service_id}/logs
POST   /desktop/v2/maintenance/cache-cleanup
POST   /desktop/v2/diagnostics
GET    /desktop/v2/diagnostics/{diagnostic_id}
GET    /desktop/v2/events
```

Routes are contract inventory, not automatic feature claims. Unavailable
provider-owned behavior returns a typed 503 and is absent from negotiated
mutation features. The provider never synthesizes success or progress.

All mutations require Desktop session authentication, idempotency, resource
generation, and `If-Match` where applicable. Lists/logs are bounded and
cursor-based. SSE events are replayable and bind event ID to canonical payload
digest.

Profile connect/disconnect, host-key review, native workspace preparation,
project create, and project activation return durable Desktop lifecycle
operation authority with HTTP 202. Renderer mutation intent is persisted before
send. If that response is lost, the renderer resolves the exact operation by
the original action ID and expected operation kind before it permits any retry.
Multi-step native project creation derives a distinct sidecar idempotency key
for each step while retaining one renderer action identity.

Lifecycle operations expose ordered phases, typed progress, a cancellable flag,
recoverable SSE updates, acknowledgement, and bounded SSH/Daemon process logs.
Normal refresh reads at most the current 200-entry tail using an observed log
sequence watermark; older pagination is explicit user action. Logs may contain
user-visible process output, but the sidecar removes credentials, Desktop/Core
capabilities, loopback endpoints, and host paths before persistence or API
projection. Cancellation is accepted only before a durable non-cancellable
mutation barrier; after that barrier it returns a typed conflict instead of
reporting an already-applied external mutation as cancelled.

Sidecar restart invalidates process-local SSH/Core authority before lifecycle
execution resumes. If a queued or running project create/activation owns work on
the invalidated profile, that same recovery transaction reserves a deterministic
durable profile-connect prerequisite bound to the parent operation and the new
disconnected generation. The executor defers the parent, completes the
prerequisite through system OpenSSH, and resumes the same parent operation and
Core mutation identity without requiring a second renderer action. An already
pending profile lifecycle operation is resumed instead of duplicated, and an
explicitly disconnected profile is never inferred to be restart-owned.

## Core Control API v2 Identity Model

V2 has no generic `revision` resource or field. It uses these distinct closed
references:

- **Project Head**: opaque head ID, project ID, generation, manifest digest,
  predecessor, Workspace Snapshot, Evolution Revision, Runtime Context
  Snapshot, Effective Execution Snapshot, and registry digest.
- **Evolution Revision**: opaque artifact-set ID, manifest digest, ordered typed
  artifact membership, and lineage.
- **Runtime Context Snapshot**: opaque materialization ID and canonical digest
  for one verified registry/runtime contract.
- **Effective Execution Snapshot**: opaque verified snapshot ID, canonical
  digest, producer ID, harness/capture/model/runtime/serving/network policy.
- **Workspace Snapshot**: opaque content identity and digest.
- **Task Admission**: immutable task/admission identity and digest pinning the
  exact predecessor head and complete execution/context closure.
- **Attempt**: append-only infrastructure-attempt identity and ordinal under one
  immutable admission.
- **Successor Transition**: transition identity and exact expected predecessor
  head plus typed phase/state.

Artifact, history, timeline, context, and event payloads use these exact types.
They never infer one identity from another or call an Evolution Revision a
Project Head.

## Executable Project And Workspace Authority

Core stores the canonical bytes behind every digest it publishes. Project
create/update therefore accepts one closed Science project document rather
than only a caller-computed digest. The release document contains the task
title/objective, `codex_subscription_transcript` settings, workspace-source
kind, and `evolution.targets.<target_id> = {enabled, method, config}`. Core
computes `project_config_sha256` after strict validation against the active
verified registry.

Native workspace data is staged through Core-owned v2 upload resources:

```text
POST /v2/projects/{project_id}/workspace-uploads
PUT  /v2/projects/{project_id}/workspace-uploads/{upload_id}/chunks/{index}
POST /v2/projects/{project_id}/workspace-uploads/{upload_id}/finalize
POST /v2/projects/{project_id}/workspace-uploads/{upload_id}/abort
```

The archive protocol is bounded, digest-verified, resumable, no-follow, and
publishes an immutable Workspace Snapshot only after complete validation. No
request or response exposes a host path. Scratch projects receive a real empty
snapshot owned by the same store.

Task submission contains expected project/head/config identities, not
caller-authored task-envelope, normalized-intent, registry, or workspace
digests. The Daemon derives and seals those values from its saved project,
staged workspace, current head, verified registry, and verified effective
execution snapshot in the admission transaction. Full method schemas,
defaults, support axes, and accepted/resolver selections are forwarded from
the Daemon capability envelope; a target-ID-only summary is not sufficient for
renderer configuration.

## Readiness, Admission, And Retry

Unsaved work is a Desktop draft. While workspace publication, settings
transition, runtime-context rebind, or a prior successor is unresolved,
submission returns not-ready before creating any Task, admission, or Attempt.
It never silently submits against a stale Project Head.

Admission atomically creates one immutable Task and closed admission pinning:

- predecessor Project Head;
- saved project and task snapshots;
- Workspace Snapshot;
- Evolution Revision;
- Runtime Context Snapshot;
- verified Effective Execution Snapshot;
- registry and normalized evolution intent.

Infrastructure retry appends an Attempt under that admission. It cannot alter a
pin, clear terminal history, or recompute the request. Idempotency reuse with a
different request is a conflict.

The Subscription production issuer seals transcript capture with
`token_level_metrics_available=false`, exact Codex harness/model, managed
runtime, task-network policy, and no serving endpoint. Ordinary callers cannot
construct a verified execution snapshot. Self-Deployed remains unavailable
until its deployment/serving issuer is verified.

An admitted Attempt is executed by the Daemon's production run owner through
the generation-bound managed service graph. Its terminal receipt binds the
actual service generation, runtime, harness, capture, model, task-network,
workspace-input and runtime-context identities to the immutable admission.
Only that verified receipt can make an Attempt authoritative and start the
successor transition. The Gateway exports a bounded, digest-verified workspace
result through an internal opaque handoff before session cleanup; Core adopts
it into the project workspace store without putting a host path in public or
persisted contract data.

## Atomic Successor

After a successful attempt, the Daemon run owner:

1. accepts the workspace result and seals the transcript dataset;
2. runs enabled methods through the verified registry outside inference;
3. validates all typed outputs;
4. materializes the complete runtime context;
5. commits one Evolution Revision and Runtime Context Snapshot; and
6. atomically publishes one adjacent successor Project Head containing the
   accepted workspace and complete context.

Any method, validation, materialization, workspace, DB, or recovery failure
leaves the predecessor active and exposes no partial successor. The next draft
stays not ready. Evolution output never affects the producing Task; it is
available only to a later admission on the committed successor.

Transition close, retry, replacement-plan, abandon, and historical restore are
idempotent compare-and-set actions. The renderer may initially expose only the
actions supported by the negotiated Subscription profile, but the provider
state model remains closed.

## Daemon And SSH Boundary

Before compatibility, SSH may inspect the host, stage/verify/activate or roll
back the exact Daemon Bundle, recover its manifest-bound maintenance protocol,
and establish the private loopback tunnel. It must not read or mutate project,
task, attempt, artifact, evolution, model, service, or history business state.

After v2 negotiation, all such operations use the active project tunnel and
Core Control API `/v2/*`. A Core error never activates SSH command fallback.

## V1 Migration

The v2 local store uses a separate versioned namespace. V1 rows are immutable
input:

- explicit profiles become non-connectable `legacy_explicit` entries;
- rebind requires the user to select a literal configured alias;
- local drafts may be copied only after complete v2 validation;
- authoritative projects are adopted only from a compatible v2 Daemon that
  proves every typed identity;
- cached v1 generic revision data is never promoted;
- corrupt or over-budget legacy input yields a bounded diagnostic without
  preventing unrelated startup.

There is no hostname-based silent conversion and no v1 mutation fallback.

## Errors And Diagnostics

Every renderer-visible failure is a strict code with retryability, affected
resource, safe summary, and a bounded action such as retry, rescan, review host
key, rebind, reconnect, install/repair Daemon, or administrator action.

Failure to establish the exact remote account/home authority is always projected
as `ssh_remote_account_unavailable`, with the fixed summary "Desktop could not
verify a supported writable remote account home.", retry enabled, and
`administrator_action`. The original probe, exception, account, UID, NSS record,
home, and command are discarded from every public and persistent surface.

Raw SSH output/commands, prompt text containing paths, config/identity/trust
paths, remote paths, Core URLs/tokens, exception text, and secret values do not
cross the boundary. Startup and runtime diagnostics bind attempt ID, ordered
stage, last completion, first failure, release/build, and bounded environment
categories.

## Release Acceptance

Provider/consumer conformance, malicious payloads, schema digests, idempotency,
ETag, cursor, SSE replay, v1 migration, system OpenSSH, askpass, host-key,
Daemon, task/admission/attempt, atomic successor, and next-session reuse all
have focused tests.

Release evidence additionally requires the exact downloaded 0.1.10 DMG on the
target Tahoe Mac, the real configured SSH alias, successful private validation
of a safe non-conventional NSS home without recording that path, exact Daemon
Bundle, real Codex Subscription task, committed successor, second-session
context use, quit/relaunch/reconnect, retained and clean local state, and
bounded diagnostic export. Component or simulator evidence cannot substitute
for that path.
