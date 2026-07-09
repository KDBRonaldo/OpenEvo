# Testing

Use focused tests for ordinary changes and broaden the suite when touching
shared contracts.

## Core Backend

```bash
ruff check src/openevo tests/openevo
PYTHONPATH=src:. python -m pytest tests/ci tests/openevo -q
```

Run relevant module suites when touching shared behavior:

```bash
PYTHONPATH=src:. python -m pytest tests/backend tests/evolution tests/gateway tests/trajectory tests/rollout -q
```

## Desktop

```bash
cd desktop
npm ci
npm test -- --run
npm run build:openevo
cd src-tauri
cargo test --locked
```

## Release Identity

```bash
python scripts/ci/audit_openevo_identity.py
python scripts/ci/check_openevo_release.py --wheel dist/*.whl
git diff --check
```
