# OpenEvo WebUI Gateway

Status: experimental architecture on `nanobot-webui-architecture`; it does not change the
External Beta product boundary or replace the canonical product specification.

## Purpose

This slice adopts the useful part of nanobot's WebUI shape: one remote gateway origin serves
the browser application, REST calls, and a WebSocket event channel. OpenEvo keeps its existing
Daemon authority and its frozen `/v2/*` contract.

```text
browser (the unchanged desktop/src renderer)
  |  Desktop Local API v2 on one loopback origin
  v
OpenEvo development Web Layer (remote loopback service)
  |- bundled copy of the existing Desktop renderer
  |- existing browser bootstrap -> Desktop session
  `- /desktop/v2 projection and daemon credential isolation
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

The response contains the existing Desktop session token. Browser REST requests place it in
`X-OpenEvo-Desktop-Session`; the Web Layer retains the daemon bearer and never exposes it to
the renderer.

## Initial endpoints

- `GET /openevo`: the bundled, unchanged Desktop renderer.
- `POST /openevo-native/browser/bootstrap`: existing browser bootstrap exchange.
- `/desktop/v2/*`: existing renderer-safe Desktop contract projected onto the development daemon.
- `/openevo-dev-agent/v1/*`: authenticated development-only compatibility proxy where the
  current renderer still requires it.

## Deliberate boundaries

- This is not a third public application and does not add an `openevo webui` public CLI.
- The Web Layer owns no domain data; the daemon remains the backend and lifecycle owner.
- No `chat_id` is treated as authorization. OpenEvo typed project/task identities remain data,
  not capabilities.
- No host paths, local media paths, SSH commands, or Daemon credentials are browser-visible.
- The existing Desktop chain remains available until this branch proves the same real-browser
  connect -> project -> task/session -> result/log -> Evolution acceptance path.
