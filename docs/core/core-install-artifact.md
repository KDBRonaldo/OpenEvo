# Core Install Artifact

> Release-candidate contract: no External Beta Core descriptor/artifact pair
> has been published yet. The unsigned candidate workflow implements the
> bounded descriptor below. Final B2 publication URL, bundled-resource, remote
> bootstrap, and installed-origin attestation remain release blockers.

The A2.3 evolution runtime consumes a bounded `framework-lock.json` containing
the distribution name/version, sibling wheel basename, and SHA-256. Desktop
currently writes that lock from its packaged exact wheel and uploads both files;
Core verifies the installed inventory and entry points before startup. This is
an internal bridge, not this full B2 descriptor: it has no release URL, source
commit or compatibility evidence and therefore does not make the release
artifact contract complete by itself.

The Core install artifact is the exact OpenEvo Core Backend package that
OpenEvo Desktop installs on the remote server. Desktop is the ordinary-user
application, but Core is the backend that owns execution, datasets, evolution
jobs, artifacts, context resolution, runtime injection, and typed service APIs.

## Descriptor Contract

Every unsigned candidate publishes one small `core-install-artifact.json`
descriptor. The candidate creator and validator enforce an exact top-level
key set and exact nested compatibility/file-entry key sets:

```json
{
  "artifact": {
    "byte_size": 123,
    "filename": "openevo-<version>-py3-none-any.whl",
    "role": "core_wheel",
    "sha256": "..."
  },
  "compatibility": {
    "python_requires": ">=3.11",
    "supported_platforms": ["linux-x86_64"]
  },
  "framework_lock": {
    "byte_size": 123,
    "filename": "framework-lock.json",
    "role": "framework_lock",
    "sha256": "..."
  },
  "registry_digest": "...",
  "schema_version": 2,
  "source_commit": "...",
  "version": "<version>"
}
```

The wheel metadata must declare the same `Requires-Python: >=3.11`. Candidate
validation rejects extra compatibility fields, another Python requirement,
another platform list, a wheel/lock digest mismatch, or a Linux verifier that
requests a platform absent from the descriptor. The current workflow proves
only `linux-x86_64`; support must not be widened without a matching clean
install job, negative tests, and descriptor update.

The descriptor is the candidate release identity for Core. Final B2 validators
must additionally compare the bundled app resource, GitHub Release asset,
remote upload, and installed backend import origin against this same object.

## Artifact Type

The implemented candidate schema accepts one Python wheel. A future
`remote_bundle` contract must use a new reviewed schema and still provide
package inventory, entrypoint, source commit, SHA256, compatibility, and
clean-install evidence equivalent to the wheel path.

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

The final B2 clean install smoke must create a fresh Python environment, install
only the declared artifact, start `openevo-backend serve`, and verify:

- `/version`;
- `/health`;
- auth failure behavior;
- loopback binding;
- state root initialization;
- typed errors;
- import origin;
- artifact SHA256, framework-lock SHA256, and descriptor SHA256.

## Bootstrap Use

Final B2 packaging must bundle `core-install-artifact.json` and the exact
install artifact bytes under:

```text
OpenEvo Desktop.app/Contents/Resources/core/
```

The current unsigned candidate publishes the descriptor and wheel as sibling
release assets; it does not prove the resource placement above. Final remote
bootstrap uploads or verifies those bytes, creates a user-level remote
environment, installs Core, writes the backend API token, starts the backend,
and opens the localhost SSH tunnel. After Core is healthy, Desktop forwards run
and service operations through Core APIs.

## Cache Use

Final B2 Desktop may reuse a local or remote cached Core artifact only when the
cached bytes match `artifact.sha256`. The bootstrap report must record
whether the source was `bundled`, `local_cache`, or `verified_redownload`.
Unverified cache reuse is release-blocking.

## PyPI

External Beta does not publish the Core install artifact to PyPI. The PyPI
workflow stays disabled and contains no publishing credential or upload step.
Any future PyPI design must reuse the validated Core bytes rather than
introduce a second descriptor or rebuild path.
