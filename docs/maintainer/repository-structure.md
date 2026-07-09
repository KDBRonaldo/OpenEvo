# Repository Structure

OpenEvo has two release-facing product surfaces:

- `desktop/`: OpenEvo Desktop, the macOS app for ordinary science users.
- `src/openevo/`: OpenEvo Core Backend, the remote Python backend/runtime.

Core Backend must not import Desktop code. Desktop may depend on Core contracts
through the local sidecar and remote backend API.

Internal development history and productization plans live under
`docs/maintainer/`. They are not user quickstarts or release notes.

Research benchmark examples live under `examples/research-benchmarks/` and are
not ordinary-user Desktop quickstarts.
