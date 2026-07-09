# OpenEvo Release Process

The release artifacts are:

- OpenEvo Core Backend wheel: `openevo-<version>-*.whl`
- OpenEvo Desktop macOS disk image: `*.dmg`

The wheel is used for remote backend installation and automation. It is not the
ordinary-user Desktop app. The `.dmg` is the macOS user-facing release artifact.

## Required Checks

```bash
cd desktop
npm ci
npm audit --audit-level=high
npm test -- --run
npm run build:openevo
cd ..
diff -qr desktop/dist desktop/packaging/web

rm -rf .openevo-remote-wheel src/openevo/wheels dist
python -m build --wheel --outdir .openevo-remote-wheel
mkdir -p src/openevo/wheels
cp .openevo-remote-wheel/openevo-*.whl src/openevo/wheels/
python -m build --wheel
python scripts/ci/check_openevo_release.py --wheel dist/*.whl
```

The GitHub release artifact workflow builds the `.dmg` on macOS. Signing,
notarization, and update policy are tracked as release hardening work.
