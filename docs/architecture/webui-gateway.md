# OpenEvo WebUI Gateway

Status: experimental architecture on `nanobot-webui-architecture`; it does not change the
External Beta product boundary or replace the canonical product specification.

## Purpose

This slice adopts the useful part of nanobot's WebUI shape: one remote gateway origin serves
the browser application and REST calls. OpenEvo keeps its existing Daemon authority. The Web
Layer exposes the existing strict Desktop v2 SSE channel. The development Daemon owns a bounded,
persistent SQLite event journal and an ordered long-poll interface; the Web Layer projects those
notifications into Desktop v2 SSE. After a browser or Web Layer restart, replay resumes from the
Daemon authority and the renderer reloads the authoritative snapshot rather than treating an
event payload as domain state.

```text
browser (desktop/src exactly as of 52ed54a)
  |  authenticated same-origin development REST
  v
OpenEvo development Web Layer (remote loopback service)
  |- bundled copy of the existing Desktop renderer
  |- existing browser bootstrap -> Desktop session
  |- /desktop/v2/events -> authenticated SSE projection and browser cursor replay
  |- /desktop/v2/development/projects/{id}/workspace* -> daemon /v2 workspace API
  `- remaining /openevo-dev-agent/v1 compatibility and daemon credential isolation
  |
  v
development agent daemon
  |- Project Head, Task admission, run, artifact, and Evolution authority
  `- persistent ordered state-event journal and bounded long poll
```

The Gateway owns no project, task, run, artifact, capability, or evolution state. It never
returns the Daemon bearer to the browser. Native Desktop/maintenance callers can continue to
use the same `/v2/*` routes with the Daemon bearer.

The first migration step deliberately leaves the formal release Daemon composition unchanged.
`dev:agent:webui:remote` starts the Web Layer beside the development daemon and opens one SSH
tunnel to it. The first walking skeleton now uses the strict `/desktop/v2` provider for Project
and Task authority and mutations. Workspace inventory/upload/download now cross an authenticated,
typed development-only Desktop v2 route and a daemon-owned `/v2` route; the old v1 workspace
payload is ignored by the self-hosted provider. Readable transcript presentation and standalone
Evolution actions remain explicit compatibility calls until their typed product contracts exist.
Promotion to the release Daemon remains gated on removing that remaining compatibility
surface and completing the full browser acceptance path.

The repeatable acceptance entry point is:

```bash
cd desktop
npm run dev:agent:webui:remote -- --browser-e2e \
  --host <host> --user <user> --ssh-port <port>
```

The launcher passes its one-time browser bootstrap authority directly to Playwright. It runs the
visible product flow against the real remote daemon and always closes the local SSH tunnel when the
test exits. The managed remote development daemon and Web Layer remain available for the next run.

## Source-level nanobot reference

This branch is based on an implementation-level review rather than only `docs/webui.md`. The first
adapted slice uses the following upstream source boundaries:

- `nanobot/cli/webui.py`: attach to one managed gateway and print explicit operator controls.
- `nanobot/cli/gateway.py`: separate `status`, `logs`, `stop`, and `restart` actions from startup.
- `nanobot/gateway/runtime.py`: record managed process identity and refuse ambiguous lifecycle
  transitions.
- `nanobot/channels/websocket/runtime.py`: authenticated browser bootstrap, bounded messages,
  request identity, reconnect replay, and ordered mutation delivery. OpenEvo already implements
  the corresponding product requirements through its closed `/desktop/v2` HTTP and SSE contract;
  the development Gateway must preserve those semantics rather than introduce a second authority.

The lifecycle implementation is adapted to OpenEvo's remote SSH topology: it manages only the
exact PID receipts under `~/.openevo/dev-agent`, verifies `/proc/<pid>/cmdline` before signaling,
and stops the Web Layer before the daemon. Status and log inspection do not update the checkout,
rotate tokens, restart either process, or open a tunnel.

From `desktop/`, use the same SSH selectors as startup:

```bash
npm run dev:agent:webui:status -- --host <host> --user <user> --ssh-port <port>
npm run dev:agent:webui:logs -- --host <host> --user <user> --ssh-port <port> --tail 200
npm run dev:agent:webui:stop -- --host <host> --user <user> --ssh-port <port>
```

Running `dev:agent:webui:remote` again is the development restart path: it deploys the selected
commit, safely replaces the two managed processes, creates a fresh browser session, and keeps the
new SSH tunnel attached to that terminal.

## Browser authentication

The launcher generates a high-entropy bootstrap token and opens the existing Desktop loopback
URL whose fragment contains that token. URL fragments are not sent in HTTP requests. The
unchanged Desktop renderer exchanges it at `POST /openevo-native/browser/bootstrap`.

The response contains a browser-session token, not the Daemon bearer. The separate browser
entry point attaches it as `X-OpenEvo-Development-Web-Token` only to same-origin
`/openevo-dev-agent/*` requests. Only that scoped browser token is kept in session storage;
the Web Layer retains the Daemon bearer and never exposes it to `desktop/src` or the browser.

## Initial endpoints

- `GET /openevo`: the bundled, unchanged Desktop renderer.
- `POST /openevo-native/browser/bootstrap`: existing browser bootstrap exchange.
- `/desktop/v2/*`: primary Project/Task control plane used by the unchanged renderer through a
  formal provider adapter. `/desktop/v2/events` reports daemon snapshot changes, accepts the
  renderer's `Last-Event-ID`, and wakes the renderer to reload authoritative state.
- `/desktop/v2/development/projects/{project_id}/workspace*`: development-only, Desktop-session
  authenticated Workspace inventory and file transfer. The Web Layer validates closed v2 models,
  enforces the upload bound, computes upload digests, and forwards to the daemon without exposing
  its bearer. The renderer verifies download digests before constructing a browser `Blob`.
- `/desktop/v2/development/artifacts*`: development-only, Desktop-session authenticated rich
  Artifact document projection. The Web Layer validates closed, bounded v2 payloads and project
  authority before the unchanged renderer receives them.
- `/openevo-dev-agent/v1/events`: authenticated development-daemon long poll used only by the
  Web Layer. An omitted cursor establishes the current daemon sequence; `after=<sequence>` returns
  contiguous committed events and `410 event_cursor_expired` forces snapshot resynchronization.
- `/openevo-dev-agent/v1/*`: authenticated development-only Evolution/transcript
  compatibility surface; it no longer owns Project, Task, Workspace browser mutations, Workspace
  reads, Artifact reads, or the terminal Session response.
- Remote daemon `/v2/tasks`, `/v2/tasks/{task_id}`, `/v2/tasks/{task_id}/timeline`, and
  `/v2/tasks/{task_id}/logs`: bounded, bearer-authenticated Task status, durable timeline,
  and persistent log/result observations consumed only by the Web Layer. Timeline and log
  cursors are stable monotonic per-Task sequences retained across daemon restarts.
  The browser never receives the daemon bearer token and continues to call only
  `/desktop/v2/*` for this data.
- Remote daemon `/v2/projects/{project_id}/workspace` and `/workspace/files`: bounded stable
  pagination, manifest drift detection, safe relative paths, digest-verified PUT/GET, and durable
  DELETE over daemon-owned project workspaces. Workspace bytes survive daemon/Web Layer restarts.
- Remote daemon `/v2/tasks/{task_id}/artifacts`, `/v2/artifacts/{artifact_id}*`, and
  `/v2/development/artifacts*`: stable Artifact metadata pagination plus bounded rich documents.
  Artifact rows and contents remain daemon-owned and survive daemon/Web Layer restarts.

## Deliberate boundaries

- This is not a third public application and does not add an `openevo webui` public CLI.
- The Web Layer owns no domain data; the daemon remains the backend and lifecycle owner.
- No `chat_id` is treated as authorization. OpenEvo typed project/task identities remain data,
  not capabilities.
- No host paths, local media paths, SSH commands, or Daemon credentials are browser-visible.
- Session admission and standalone Evolution remain separate UI and backend actions: the
  Session seals reusable transcript evidence; the user later selects evidence and starts an
  Evolution Run from the Evolution workspace.
- The existing Desktop chain remains available until this branch proves the same real-browser
  connect -> project -> task/session -> result/log -> Evolution acceptance path.
