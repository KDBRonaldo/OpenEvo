# OpenEvo Productization

Current productization work is tracked by #131 and has two authoritative files:

- `spec.md`: what OpenEvo External Beta ships and how release readiness is
  judged;
- `implementation-plan.md`: the current workstreams and execution order.

The specification is intentionally concise. API fields belong in
`docs/core/` or `docs/architecture/`; implementation decisions belong in the
owning issue, code, tests, and module documentation; release commands belong in
the release workflow and maintainer release guide.

OpenEvo has exactly two release-facing product surfaces:

- OpenEvo Core Backend on the remote server;
- OpenEvo Desktop for ordinary science users on macOS.

Benchmark automation, backend launchers, maintenance scripts, and historical
development records are not additional product surfaces.

Do not add a second release specification or duplicate release checklist. If
implementation exposes a missing product decision, amend `spec.md` narrowly and
continue execution.
