# WebUI Gateway architecture

OpenEvo's product entry is a bundled React application served by the remote Web
Layer and exposed through one local loopback SSH tunnel.

## Components

- `desktop/`: React source and v2 browser client.
- `src/openevo/web_gateway/static/`: committed, version-matched WebUI bundle.
- `src/openevo/launcher.py`: SSH-config discovery, local Git-bundle source
  delivery, explicit install/update/start, lifecycle, SSH tunnel, browser
  bootstrap, and status/log/stop commands behind `openevo webui`.
- `scripts/dev/run_remote_agent_development.py`: thin compatibility launcher.
- `src/openevo/web_gateway/product_app.py`: formal browser authentication,
  Desktop v2 projection, daemon event relay, static hosting, and process entry.
- `scripts/dev/development_agent_web_layer.py`: thin compatibility launcher.
- `src/openevo/daemon/product_app.py`: formal durable
  project/session/workspace, Agent, and Evolution authority.
- `scripts/dev/live_agent_daemon.py`: thin compatibility launcher for older
  development commands and imports.

## Build

```bash
cd desktop
npm run build:webui-gateway
```

The build writes hashed assets into `src/openevo/web_gateway/static`.  The
launcher delivers those exact committed bytes from the local checkout over
SSH; it neither fetches source nor builds a different frontend on the server.

## Design direction

The implementation should stay small and inspectable.  HKUDS/nanobot is a
useful source-level reference for its one-command WebUI, version-matched
bundled assets, shared gateway lifecycle, persistent browser history, and
localhost-by-default exposure.  OpenEvo retains its own project/evolution data
model and SSH-remote topology.
