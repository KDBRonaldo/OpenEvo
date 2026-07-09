# Contributing

OpenEvo development is issue-first for non-trivial changes. Before changing
public behavior, runtime contracts, backend APIs, artifact schemas, Desktop
flows, or developer workflows, open or update the relevant GitHub issue.

## Workflow

1. Start from `stable`.
2. Create a focused branch.
3. Preserve existing evolution algorithm behavior unless the issue explicitly
   scopes an algorithm change.
4. Update docs for public API, runtime, Desktop, deployment, release, or user
   workflow changes.
5. Run focused tests and `git diff --check`.
6. Open a PR against `stable` and link the issue with `Fixes`, `Closes`,
   `Resolves`, or `Part of`.

## Local Checks

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ruff check src tests scripts
pytest -q
```

Desktop checks:

```bash
cd desktop
npm ci
npm test -- --run
npm run build:openevo
cd src-tauri
cargo test --locked
```

## Scope Boundaries

OpenEvo Desktop is the ordinary-user macOS app. OpenEvo Core Backend owns
execution, runtime, trajectory, evolution, artifacts, deployment, and backend
APIs. Developer automation and benchmark adapters must reuse Core contracts
instead of adding a second method registry, artifact model, or context resolver.
