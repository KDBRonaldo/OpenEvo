# OpenEvo WebUI Gateway

Status: experimental architecture on `nanobot-webui-architecture`; it does not change the
External Beta product boundary or replace the canonical product specification.

## Purpose

This slice adopts the useful part of nanobot's WebUI shape: one remote gateway origin serves
the browser application and REST calls. OpenEvo keeps its existing Daemon authority. A durable
event channel is not part of this development slice yet.

```text
browser (desktop/src exactly as of 52ed54a)
  |  authenticated same-origin development REST
  v
OpenEvo development Web Layer (remote loopback service)
  |- bundled copy of the existing Desktop renderer
  |- existing browser bootstrap -> Desktop session
  `- /openevo-dev-agent/v1 compatibility proxy and daemon credential isolation
  |
  v
development agent daemon
  `- Project Head, Task admission, run, artifact, and Evolution authority
```

The Gateway owns no project, task, run, artifact, capability, or evolution state. It never
returns the Daemon bearer to the browser. Native Desktop/maintenance callers can continue to
use the same `/v2/*` routes with the Daemon bearer.

The first migration step deliberately leaves the formal release Daemon composition unchanged.
`dev:agent:webui:remote` starts the Web Layer beside the development daemon and opens one SSH
tunnel to it. Promotion to the release Daemon remains gated on a complete `/desktop/v2` to
canonical `/v2` adapter and the full browser acceptance path.

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
- `/openevo-dev-agent/v1/*`: authenticated development-only compatibility proxy used by the
  unchanged `52ed54a` renderer.
- `/desktop/v2/*`: retained projection for contract migration and tests; it is not used to
  replace or reinterpret the current WebUI.

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
