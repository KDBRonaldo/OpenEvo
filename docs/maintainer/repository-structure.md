# Repository Structure

OpenEvo has two release-facing product surfaces:

- `desktop/`: OpenEvo Desktop, the macOS app for ordinary science users.
- OpenEvo Daemon: the remote Linux application assembled from `src/openevo/`
  and the release-managed runtime.

Core must not import Desktop code. Desktop may depend on Core contracts only
through the local sidecar and remote Daemon API.

Internal development history and productization plans live under
`docs/maintainer/`. They are not user quickstarts or release notes.

## Core Package

`src/openevo/` is the shared Core implementation assembled into OpenEvo Daemon.
It owns backend APIs, deployment lifecycle, trajectory/transcript capture,
dataset/job/artifact stores, method registry, context resolution, runtime
injection, and remote server supervision. The Core package may contain backend
launchers and maintainer automation helpers, but it must not present a public
CLI product, Desktop installer, or benchmark runner inside the release
artifact.

## Desktop App

`desktop/` is the OpenEvo Desktop macOS app. It controls the Daemon through the
local sidecar and remote API, manages ordinary-user profiles and science
projects, configures remote servers, installs or attaches the Daemon, and
monitors runs and evolved artifacts. Desktop is the only ordinary-user entry
point for External Beta.

## Benchmark Automation Boundary

Benchmark automation is release-maintainer material. Terminal Bench automation
must live outside `src/openevo/` and `desktop/`, under a standalone
`benchmarks/terminal_bench/` package or an external automation repository. It may
call Core APIs and import stable Core contracts, but Core and Desktop release
artifacts must not package benchmark runners, scorers, materializers, or
benchmark-specific command entrypoints.

The A3 mechanical migration establishes `benchmarks/terminal_bench/` as an
independently installable package with the maintainer-only
`openevo-terminal-bench` entrypoint. Core owns the data-only agent-system GEPA
selection/state kernel in
`src/openevo/evolution/agent_system_gepa_kernel.py`; the benchmark runner calls
that kernel and does not duplicate its policy. The old Core modules and CLI
wiring are removed without wrappers or re-exports. B3 revision admission and
queued/not-ready integration remain a later Issue #156 PR.

## Legacy Quarantine

Historical legacy identity, old runtime markers, old Terminal Bench paths, and
pre-External-Beta smoke wrappers belong only in migration notes, protected
fixtures, or maintainer-only history. Public docs, release notes, Desktop UI,
package metadata, and ordinary-user workflows must use OpenEvo Daemon, OpenEvo
Desktop, `OPENEVO_*`, `/openevo/session`, `.openevo/evolution`, and
`openevo.session_completed`. OpenEvo Core names the implementation, not a third
release-facing application.
