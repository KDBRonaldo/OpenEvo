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

Release packaging and the generated POSIX remote installer are covered by:

```bash
uv run pytest tests/release tests/dev/test_run_remote_agent_development.py
```

Build a candidate only after committing the exact tree being released:

```bash
uv run python scripts/release/build_self_hosted_bundle.py \
  --output dist/openevo-self-hosted.oevobundle
```

The builder packages committed files, not dirty working-tree bytes.

The repository-free local launcher and its per-user installer are covered by:

```bash
uv run pytest tests/release/test_launcher_distribution.py
uv run python scripts/release/build_launcher_distribution.py \
  --output dist/openevo-launcher.tar.gz
```

Extract the result into a clean directory, run the following commands, and
confirm they work without `PYTHONPATH` or access to the checkout:

```bash
sh openevo-launcher/install.sh --prefix <temporary-prefix>
<temporary-prefix>/bin/openevo webui --help
```

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
