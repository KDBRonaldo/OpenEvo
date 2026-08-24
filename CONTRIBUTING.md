# Contributing

Read `AGENTS.md` and `docs/maintainer/productization/spec.md` before changing
the product path.

The current rebuild has one supported development entry point:

```bash
cd desktop
npm run dev:agent:webui:remote -- --host <host> --user <user> --ssh-port <port>
```

Do not restore the deleted Tauri host, release sidecar, Core Control daemon, or
managed deployment framework as a shortcut. New product behavior should extend
the local WebUI gateway and remote lightweight daemon in small, tested steps.

Before submitting a change, run the relevant Python tests plus:

```bash
cd desktop
npm test -- --run
npm run typecheck
npm run build:webui-gateway
```

Keep benchmark-specific code under `benchmarks/`, preserve `OPENEVO_*` runtime
identity, and do not commit credentials, private keys, tokens, or host-specific
state.
