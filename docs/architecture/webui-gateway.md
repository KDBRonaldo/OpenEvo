# WebUI Gateway architecture

OpenEvo's product entry is a bundled React application served by the remote Web
Layer and exposed through one local loopback SSH tunnel.

## Components

- `desktop/`: React source and v2 browser client.
- `src/openevo/web_gateway/static/`: committed, version-matched WebUI bundle.
- `src/openevo/launcher.py`: SSH-config discovery, release/Git-bundle delivery,
  explicit install/update/start, lifecycle, SSH tunnel, browser bootstrap, and
  status/log/stop commands behind `openevo webui`.
- `src/openevo/release_bundle.py`: deterministic runtime-only release builder
  and strict manifest/archive verifier.
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

For a versioned delivery artifact, the maintainer builder takes one exact Git
commit and creates a deterministic `.oevobundle`. Its closed manifest binds the
product version, source commit, allowed runtime file inventory, byte sizes, and
SHA-256 digests into one release ID. The payload includes only the Python
runtime, built WebUI, Web Layer/daemon code, server-side Desktop v2 contract,
and dependency lock. It excludes tests, Git metadata, and React source.

The launcher verifies every bundle entry before upload. The remote installer
then verifies the transport digest, manifest identity, archive shape, and every
file digest before atomically publishing
`~/.openevo/dev-agent/releases/<release-id>`. Existing SQLite/workspace state
and the release-specific Python environment under
`~/.openevo/dev-agent/runtimes/<release-id>` remain outside that immutable
directory. The original Git-bundle path remains available for source
development.

## Design direction

The implementation should stay small and inspectable.  HKUDS/nanobot is a
useful source-level reference for its one-command WebUI, version-matched
bundled assets, shared gateway lifecycle, persistent browser history, and
localhost-by-default exposure.  OpenEvo retains its own project/evolution data
model and SSH-remote topology.
