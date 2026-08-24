# Repository structure

- `desktop/`: React self-hosted WebUI and static host.
- `scripts/dev/`: remote launcher, SSH tunnel, Web Layer, and lightweight daemon.
- `src/openevo/web_gateway/`: built WebUI distribution boundary.
- `src/openevo/`: reusable Core implementation for harness, capture, evolution,
  runtime, rollout, and supporting APIs.
- `benchmarks/`: independently maintained benchmark automation.
- `tests/`: regression and contract tests for the retained boundaries.

The removed Tauri package, release sidecar, Core Control service, and managed
deployment stack are not part of this branch's product architecture.
