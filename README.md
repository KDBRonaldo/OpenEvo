# OpenEvo

OpenEvo is being rebuilt as a self-hosted WebUI for running a real agent on a
remote Linux workspace and evolving reusable context between sessions.

The current product path is intentionally small:

```text
browser -> local SSH tunnel -> remote Web Layer -> remote OpenEvo daemon
        -> agent harness / OpenEvo Core
```

The previous packaged macOS/Tauri application, release sidecar, Core Control
service, and managed deployment stack have been removed from the experimental
`nanobot-formal-rebuild` branch. They are not compatibility targets.

## Run the working remote WebUI

From WSL:

```bash
cd /mnt/c/Users/18083/Desktop/OpenEvo/desktop

npm run dev:agent:webui:remote -- \
  --host js4.blockelite.cn \
  --user root \
  --ssh-port 27104
```

The launcher compares the server's managed checkout with the current committed
local branch head. If they differ, it creates a verified Git bundle locally and
uploads it through SSH; the server never fetches OpenEvo source from GitHub.
When the commits already match, source delivery is skipped. The launcher then
starts the remote processes, opens the SSH tunnel, and serves the React UI.

Installation, update, and start can also be run separately:

```bash
# First source and runtime installation; does not start services.
npm run dev:agent:webui:remote -- \
  --host <host> --user <user> --ssh-port <port> \
  --source-action install

# Deliver a newer commit and prepare its runtime; does not start services.
npm run dev:agent:webui:remote -- \
  --host <host> --user <user> --ssh-port <port> \
  --source-action update

# Start only when the installed and local commits already match.
npm run dev:agent:webui:remote -- \
  --host <host> --user <user> --ssh-port <port> \
  --source-action start
```

The default command uses `--source-action auto`, preserving the one-command
workflow. Source delivery requires a local commit, but it does not require a
push. SSH connection, source transfer, remote command, dependency bootstrap,
and health checks are bounded so a failed network phase reports an error.

## OpenEvo daemon

The remote daemon is composed under `src/openevo/daemon/product_app.py`. It owns
Project, Session, workspace, artifact, Agent Runner, and Evolution services
behind the existing loopback-only API and SQLite state. Managed lifecycle
commands remain available:

```bash
openevo-daemon start
openevo-daemon status
openevo-daemon stop
```

The working remote WebUI launcher now starts this formal module directly. The
former `scripts/dev/live_agent_daemon.py` path is retained only as a thin
compatibility launcher, so existing persisted state and browser behavior do not
change.

The remote Web Layer is likewise composed under
`src/openevo/web_gateway/product_app.py`; it serves the committed React bundle,
authenticates browser sessions, relays daemon events, and projects the daemon
into the existing Desktop v2 browser contract. Its former development script
is also only a compatibility launcher.

Useful companion commands:

```bash
npm run dev:agent:webui:status
npm run dev:agent:webui:logs
npm run dev:agent:webui:stop
```

## Local development

```bash
cd desktop
npm install
npm test -- --run
npm run typecheck
npm run build:webui-gateway
```

Python tests for the retained product path live mainly under `tests/dev/`,
`tests/backend/test_evolution_runtime.py`, and the retained WebUI contract tests.

## Repository boundary

- `desktop/`: React WebUI and the small static host used by the gateway.
- `scripts/dev/`: local launcher, SSH tunnel orchestration, remote WebUI layer,
  and thin compatibility entry points.
- `src/openevo/web_gateway/`: formal Web Layer composition and built,
  version-matched WebUI assets.
- `src/openevo/daemon/`: formal process composition, closed daemon contracts,
  project/event/task/Session persistence, managed workspace and artifacts,
  Agent harness execution, and Evolution orchestration.
- `src/openevo/`: reusable agent, capture, evolution, runtime, and rollout code.
- `benchmarks/`: standalone benchmark automation.

The rebuild contract is documented in
[`docs/maintainer/productization/spec.md`](docs/maintainer/productization/spec.md).

## License

OpenEvo is distributed under the terms in [LICENSE](LICENSE).
