# OpenEvo Productization

Current productization work is tracked by #131.

- `spec.md` is the only canonical product specification. It defines what
  OpenEvo External Beta ships and how release readiness is judged.
- `implementation-plan.md` is a non-normative execution tracker. It records
  current gaps and sequencing but cannot add or change product requirements.

API fields belong in `docs/core/` or `docs/architecture/`; implementation
decisions belong in the owning issue, code, tests, and module documentation;
release commands belong in the release workflow and maintainer release guide.

OpenEvo has exactly two release-facing product surfaces:

- OpenEvo Daemon on the remote Linux server;
- OpenEvo Desktop Client for ordinary science users on macOS.

OpenEvo Core is the implementation used by the Daemon, not a third application.
Benchmark automation, backend launchers, maintenance scripts, and historical
development records are not additional product surfaces.

Do not add a second release specification or duplicate product requirements in
the implementation plan. If implementation exposes a missing product decision,
amend `spec.md` once and make the implementation documents conform to it.
