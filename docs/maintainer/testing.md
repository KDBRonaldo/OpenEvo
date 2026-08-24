# Testing the self-hosted rebuild

## WebUI

```bash
cd desktop
npm install
npm test -- --run
npm run typecheck
npm run build:webui-gateway
```

The production-style build is written to `src/openevo/web_gateway/static/` and
validated by `desktop/scripts/verify-webui-gateway-build.mjs`.

## Python product path

Run the retained launcher, Web Layer, daemon, WebUI host, v2 wire-contract, and
runtime tests:

```bash
uv run pytest \
  tests/daemon \
  tests/dev/test_openevo_webui_cli.py \
  tests/dev/test_run_remote_agent_development.py \
  tests/dev/test_live_agent_daemon.py \
  tests/dev/test_development_agent_web_layer.py \
  tests/openevo/sidecar/test_browser_host.py \
  tests/openevo/sidecar/test_desktop_contract_v2.py \
  tests/openevo/sidecar/test_event_broker_v2.py \
  tests/backend/test_evolution_runtime.py \
  tests/backend/test_workspace_handoff_v2.py
```

Run broader Core tests when changing capture, evolution, gateway, rollout, or
runtime code.

The new daemon lifecycle can also be smoke-tested independently without
switching the product path:

```bash
openevo-daemon start --state-root ./tmp/daemon-smoke --port 18887
openevo-daemon status --state-root ./tmp/daemon-smoke
openevo-daemon logs --state-root ./tmp/daemon-smoke
openevo-daemon stop --state-root ./tmp/daemon-smoke
```

## Manual remote smoke

First verify SSH-config discovery and the formal entry:

```bash
uv run openevo webui
```

The explicit compatibility acceptance remains:

```bash
cd desktop
npm run dev:agent:webui:remote -- \
  --host <host> \
  --user <user> \
  --ssh-port <port>
```

Verify that existing projects remain listed after creating another project,
sessions survive a browser reload, and `status`, `logs`, and `stop` operate on
the same launcher state.
