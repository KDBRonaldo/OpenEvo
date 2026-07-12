# Core Install Artifact

> Target contract: no External Beta Core descriptor/artifact pair has been
> published yet. Workstream B2 implements and tests this format before Desktop
> or release docs may rely on it.

The Core install artifact is the exact OpenEvo Core Backend package that
OpenEvo Desktop installs on the remote server. Desktop is the ordinary-user
application, but Core is the backend that owns execution, datasets, evolution
jobs, artifacts, context resolution, runtime injection, and typed service APIs.

## Descriptor Contract

Every External Beta release publishes one small `core-install-artifact.json`
descriptor. Its implementation must add a typed model or JSON schema and
negative tests in the same PR; this document does not claim that validator
already exists.

```json
{
  "core_install_artifact": {
    "type": "wheel",
    "version": "<version>",
    "filename": "openevo-<version>-py3-none-any.whl",
    "release_asset_url": "github-release://<owner>/<repo>/releases/tags/<tag>/assets/openevo-<version>-py3-none-any.whl",
    "resource_relative_path": "core/openevo-<version>-py3-none-any.whl",
    "sha256": "...",
    "size_bytes": 123,
    "source_commit": "...",
    "python_requires": ">=3.11,<3.13",
    "supported_platforms": ["linux-x86_64"]
  }
}
```

The descriptor is the release identity for Core. Validators must compare the
descriptor file, bundled app resource, GitHub Release asset, remote upload, and
installed backend import origin against this same object.

## Artifact Type

`type` may be `wheel` or `remote_bundle`. External Beta normally uses a Python
wheel. If a `remote_bundle` is used, it must still provide package inventory,
entrypoint, source commit, SHA256, and clean-install evidence equivalent to the
wheel path.

Desktop must not install Core from a source checkout, editable install, stale
cache, package-relative fallback, or locally rebuilt artifact when release mode
is enabled.

## SHA256

`sha256` is the digest of the install artifact bytes. The DMG build report,
remote bootstrap report, Core install smoke, benchmark release gate,
release-notes validation, and downloaded GitHub Release validation must all
reference the same hash. Any mismatch requires rebuilding upstream release
artifacts rather than patching downstream evidence.

## Source Commit

`source_commit` identifies the Git commit that produced the artifact. Clean
install smoke must prove the running `openevo` package imports from the
installed release artifact and not from the repository checkout.

## Clean Install

The clean install smoke creates a fresh Python environment, installs only the
declared artifact, starts `openevo-backend serve`, and verifies:

- `/version`;
- `/health`;
- auth failure behavior;
- loopback binding;
- state root initialization;
- typed errors;
- import origin;
- artifact SHA256 and descriptor SHA256.

## Bootstrap Use

OpenEvo Desktop bundles `core-install-artifact.json` and the exact install
artifact bytes under:

```text
OpenEvo Desktop.app/Contents/Resources/core/
```

Remote bootstrap uploads or verifies those bytes, creates a user-level remote
environment, installs Core, writes the backend API token, starts the backend,
and opens the localhost SSH tunnel. After Core is healthy, Desktop forwards run
and service operations through Core APIs.

## Cache Use

Desktop may reuse a local or remote cached Core artifact only when the cached
bytes match `core_install_artifact.sha256`. The bootstrap report must record
whether the source was `bundled`, `local_cache`, or `verified_redownload`.
Unverified cache reuse is release-blocking.

## PyPI

External Beta does not publish the Core install artifact to PyPI. The PyPI
workflow stays disabled and contains no publishing credential or upload step.
Any future PyPI design must reuse the validated Core bytes rather than
introduce a second descriptor or rebuild path.
