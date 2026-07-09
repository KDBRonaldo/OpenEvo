# OpenEvo Release Process

The release artifacts are:

- OpenEvo Core Backend wheel: `openevo-<version>-*.whl`
- OpenEvo Core Backend wheel checksum: `openevo-<version>-*.whl.sha256`
- OpenEvo Desktop macOS disk image: `OpenEvo Desktop_<version>_<target>.dmg`
- OpenEvo Desktop disk image checksum: `OpenEvo Desktop_<version>_<target>.dmg.sha256`
- Release notes: `release-notes.md`

The wheel is used for remote backend installation and automation. It is not the
ordinary-user Desktop app. The `.dmg` is the macOS user-facing release artifact.
The release artifact validator rejects non-OpenEvo wheels, unknown files,
orphan checksums, and checksums that are not siblings of the artifact they
describe.

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
python scripts/ci/write_sha256.py dist/*.whl
```

The GitHub release artifact workflow builds the `.dmg` on macOS, smokes the
packaged sidecar, writes checksums for binary artifacts, and validates the final
artifact list with `scripts/ci/check_openevo_release.py --artifact`.
Signing, notarization, and update policy remain separate release operations.
