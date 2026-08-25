# Changelog

- Added a deterministic repository-free launcher archive containing a
  standard-library Python launcher and its exact server Release Bundle, plus a
  verified idempotent per-user installer and atomic version activation; users
  no longer need the source tree, Git, uv, Node, or npm on their computer.
- Added deterministic, versioned self-hosted Release Bundles containing the
  built WebUI, Web Layer, daemon, server contract code, and dependency lock;
  the formal launcher now verifies and installs these bundles by immutable
  release ID over SSH without Docker or a server-side GitHub checkout.
- Promoted the proven SSH/Git-bundle/tunnel path into `openevo webui`, with
  bounded `~/.ssh/config` host discovery, interactive selection, last-server
  persistence, automatic browser opening, and a thin historical script adapter.
- Moved browser authentication, Desktop v2 projection, daemon event relay,
  static WebUI hosting, and Web Layer process startup into
  `openevo.web_gateway.product_app`; the remote launcher now starts the formal
  module while the former development Web Layer remains a thin compatibility
  adapter.
- Moved the accepted daemon HTTP models, runtime-context projection, dependency
  initialization, and final Project/Session/Workspace/Artifact/Agent/Evolution
  process composition into `openevo.daemon.product_app`; the remote launcher
  now starts that formal module while the former development daemon and
  contract modules remain thin compatibility adapters.
- Extracted transcript dataset sealing, capability-driven fixed-input Evolution
  jobs/retries, output validation, artifact publication, and explicit
  multi-Session candidate runs into `openevo.daemon` without changing the
  browser API or SQLite identities.
- Extracted normalized Codex harness execution and the full admitted Session
  workspace/context/result transaction into `openevo.daemon`, preserving live
  cancellation, failure semantics, file mutation capture, and optional
  Evolution evidence sealing behind the existing API.
- Extracted managed project workspaces and durable dataset/evolution artifact
  ownership into `openevo.daemon`, retaining safe file bounds, document
  projections, digest pagination, promoted-context selection, existing SQLite
  rows, and all current WebUI/API behavior.
- Extracted asynchronous Session execution ownership into `openevo.daemon`,
  including exclusive admission, exact idempotency, live cancellation-signal
  delivery, worker failure handling, and unconditional operation-lock cleanup,
  without changing the working WebUI/API/SQLite path.
- Open the Session conversation immediately after Start Session, show explicit
  Agent startup/working states while the daemon is running, stream refreshed
  replies into the chat, and expose Session cancellation in both the live chat
  and project overview.
- Extracted durable Session schema, migrations, context pinning, lifecycle
  transitions, cancellation, and restart recovery into `openevo.daemon` while
  retaining the existing SQLite data, API payloads, and task journal transaction.
- Reworked remote source delivery so the server never fetches OpenEvo from
  GitHub: matching commits start directly, changed commits travel as verified
  local Git bundles over SSH, install/update/start are explicit actions, and
  network-sensitive phases have finite timeouts.
- Extracted durable task logs and typed task timeline persistence into
  `openevo.daemon` while retaining existing SQLite tables, pagination cursors,
  historical backfill, and Web Layer payloads.
- Extracted the recoverable SQLite state event journal into `openevo.daemon`
  without changing event routes, payloads, sequence continuity, bounded replay,
  or commit-time long-poll wakeups.
- Extracted durable project catalog and active-project SQLite ownership into
  `openevo.daemon` while preserving the working remote WebUI command, existing
  database layout, event behavior, and multi-project visibility.

## Unreleased — self-hosted rebuild

- Retain the working remote agent WebUI path driven by
  `npm run dev:agent:webui:remote`.
- Keep remote project and session state visible across project creation.
- Remove the former packaged Tauri Desktop, release sidecar, Core Control
  service, managed deployment stack, release workflows, and their tests.
- Reset product documentation and CI around the self-hosted WebUI architecture.
- Start the replacement loopback daemon with authenticated health/status APIs,
  private atomic lifecycle state, PID identity checks, and lifecycle commands.

Earlier packaged Desktop preview history remains available in Git history and
is not a compatibility contract for this branch.
