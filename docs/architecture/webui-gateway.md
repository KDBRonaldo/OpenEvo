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
  `- /openevo-dev-agent/v1 compatibility proxy and daemon credential isolation
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
and Task authority and mutations. Readable transcript/workspace presentation and standalone
Evolution actions remain explicit development-bridge calls until their typed product contracts
exist. Promotion to the release Daemon remains gated on removing that remaining compatibility
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
- `/openevo-dev-agent/v1/events`: authenticated development-daemon long poll used only by the
  Web Layer. An omitted cursor establishes the current daemon sequence; `after=<sequence>` returns
  contiguous committed events and `410 event_cursor_expired` forces snapshot resynchronization.
- `/openevo-dev-agent/v1/*`: authenticated development-only presentation and standalone
  Evolution compatibility surface; it no longer owns Project or Task browser mutations.

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
