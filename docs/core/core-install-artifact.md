# Interim Core Wheel Packaging Contract

> Current implementation contract only: the unsigned packaging rehearsal
> implements the bounded wheel descriptor below. It is not the External Beta
> remote-install contract and cannot satisfy the canonical Daemon Bundle,
> clean-host deployment, or release-consistency gates.

The A2.3 evolution runtime consumes a bounded `framework-lock.json` containing
the distribution name/version, sibling wheel basename, and SHA-256. Desktop
currently writes that lock from its packaged exact wheel and uploads both files;
Core verifies the installed inventory and entry points before startup. This is
an internal bridge, not this full B2 descriptor: it has no release URL, source
commit or compatibility evidence and therefore does not make the release
artifact contract complete by itself.

The interim artifact is the exact Core wheel used by the current packaging and
Linux service rehearsal. The final product installs OpenEvo Daemon from the
self-contained, manifest-matched Daemon Bundle defined in
`docs/maintainer/productization/spec.md`; Core remains its implementation.

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

The descriptor is the identity for the current packaging rehearsal only.
External Beta validators must instead bind Desktop, the Daemon Bundle, release
manifest, remote install, and running Daemon identity.

## Artifact Type

The implemented rehearsal schema accepts one Python wheel. It must not be
extended into the External Beta contract by adding a `remote_bundle` variant;
the Daemon Bundle and release manifest use their own closed schemas.

Desktop must not install Core from a source checkout, editable install, stale
cache, package-relative fallback, or locally rebuilt artifact when release mode
is enabled.

## SHA256

`sha256` is the digest of the rehearsal wheel bytes. The current DMG build
report, Linux service smoke, release-notes validation, and downloaded draft
validation reference the same hash. Any mismatch fails the rehearsal rather
than authorizing downstream evidence repair.

## Source Commit

`source_commit` identifies the Git commit that produced the artifact. Clean
install smoke must prove the running `openevo` package imports from the
installed release artifact and not from the repository checkout.

## Current Linux Rehearsal

The current Linux smoke creates a fresh Python environment, installs only the
declared artifact, starts `openevo-backend serve`, and verifies:

- `/version`;
- `/health`;
- auth failure behavior;
- loopback binding;
- state root initialization;
- typed errors;
- import origin;
- artifact SHA256, framework-lock SHA256, and descriptor SHA256.

## Bootstrap Use

The current unsigned draft publishes the descriptor and wheel as sibling
rehearsal assets. It does not prove the External Beta bootstrap path. Release
Desktop must upload its bundled Daemon Bundle or download only the exact
manifest-bound bundle, verify it, install it without remote PyPI, and attach the
healthy loopback Daemon.

## PyPI

External Beta does not publish Core or Daemon installation through PyPI. The
PyPI workflow stays disabled and contains no publishing credential or upload
step.
