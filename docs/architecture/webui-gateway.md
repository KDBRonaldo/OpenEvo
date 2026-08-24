# WebUI Gateway architecture

OpenEvo's product entry is a bundled React application served by the remote Web
Layer and exposed through one local loopback SSH tunnel.

## Components

- `desktop/`: React source and v2 browser client.
- `src/openevo/web_gateway/static/`: committed, version-matched WebUI bundle.
- `scripts/dev/run_remote_agent_development.py`: deploy, lifecycle, SSH
  tunnel, browser bootstrap, status/log/stop commands.
- `scripts/dev/development_agent_web_layer.py`: browser authentication and
  Desktop v2 projection.
- `scripts/dev/live_agent_daemon.py`: durable project/session/workspace and
  evolution authority.

## Build

```bash
cd desktop
npm run build:webui-gateway
```

The build writes hashed assets into `src/openevo/web_gateway/static`.  The
launcher deploys those exact committed bytes; it does not build a different
frontend on the server.

## Design direction

The implementation should stay small and inspectable.  HKUDS/nanobot is a
useful source-level reference for its one-command WebUI, version-matched
bundled assets, shared gateway lifecycle, persistent browser history, and
localhost-by-default exposure.  OpenEvo retains its own project/evolution data
model and SSH-remote topology.
