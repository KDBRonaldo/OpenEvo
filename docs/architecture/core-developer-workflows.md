# OpenEvo Core Developer Workflows

OpenEvo does not ship a separate developer product or product CLI. Developer
workflows wrap OpenEvo Core Backend through source checkout utilities,
Python-facing Core modules, the `openevo-backend` server-side launcher,
standalone benchmark automation, method-development helpers, artifact
inspection, and regression-test fixtures.

Desktop is not a developer console. Developer benchmark work should use
standalone benchmark automation outside Core and Desktop. That automation should
import and call Core capabilities, and it should produce Core records, datasets,
metrics, jobs, artifacts, and context inputs.

## Ownership

OpenEvo Core developer workflows own tasks that require source checkout,
Core maintenance automation, or method-development context:

- Server-side backend runner commands such as `openevo-backend run`.
- Module-level evolution utilities invoked from a source checkout.
- Source-facing Python helpers and Core facades used by tests and standalone
  benchmark automation.
- Regression-test fixtures for Core records, datasets, jobs, artifacts, context
  resolution, and runtime injection.
- Method-development helpers for inspecting capabilities, constructing jobs,
  registering artifacts, and comparing outputs.
- Artifact inspection and debugging tools for developer or internal methods.

Standalone benchmark automation owns benchmark-specific harness execution,
transcript/result parsing, benchmark scoring, artifact materialization into that
benchmark harness, and release gate summaries. It must live outside
`src/openevo` and `desktop`.

OpenEvo Desktop owns the ordinary-user science workflow. It should help a
science user configure a project, connect a remote machine, run Core-backed
experiments, and monitor results. It should not expose benchmark controls,
method registry editing, artifact debugging, raw backend mutations, or hidden
developer-console behavior.

## Method Catalog Lifecycle

OpenEvo Core is the source of truth for built-in method capabilities. Desktop
and maintainer automation must not maintain separate hardcoded method tables.

The lifecycle for a new built-in method is:

1. Implement the method without adding target-specific scheduler, store, Gateway,
   benchmark, or Desktop branches.
2. Add one `EvolutionMethodDescriptor` to the built-in catalog, or return it
   from an explicitly locked research-plugin catalog.
3. Declare target, ordered input bindings, output types, four support axes,
   closed config schema, exposure, maturity, and immutable implementation identity.
4. Freeze and validate the descriptor graph, then verify every frozen identity's
   installed distribution and entry point. Publish the loaded registry only if
   both phases succeed.
5. Expose the target-rooted capability projection from that same snapshot.
6. Let Desktop consume only `desktop` exposure; maintainers and benchmark
   automation may consume `maintainer` and `internal` entries through Core.

During A2.2 only, existing built-ins remain in `METHOD_REGISTRY` for unchanged
worker dispatch, and `METHOD_METADATA` remains for the old capabilities path.
Any built-in added to one legacy table during this interval must be added to the
other with the same key; CI enforces equality. Exact-object tests prevent
callable drift. A2.3 cuts dispatch/capabilities over; A2.5 deletes both duplicate
legacy tables.

A2.2 can verify a locked research-plugin wheel and its entry points, but current
workers do not invoke those handles. In-process plugin execution and registry
composition are A2.3 behavior. Until then, new plugin code is testable through
its direct contract or the existing external-worker protocol, not as a claimed
release capability.

Framework exposure uses `desktop`, `maintainer`, and `internal`. The historical
legacy metadata values `ordinary_user` and `dev_kit` remain only until A2.5;
they do not define or imply a separately released developer product surface.

## Standalone Benchmark Automation

Benchmark automation translates external benchmark tasks, transcripts, scores,
and artifacts into the OpenEvo Core contract. It may normalize
benchmark-specific metadata, redact protected material, compute metrics, run the
external harness, and materialize artifacts back into that harness, but it must
produce Core records, datasets, metrics, jobs, artifacts, and context inputs.

Benchmark automation must live in standalone packages/modules outside
`src/openevo` and `desktop`, for example under `benchmarks/<benchmark>/` or in a
separately packaged automation module. Core must not import benchmark
automation. Desktop must not expose benchmark controls or benchmark gates.

Benchmark automation must not implement a separate evolution backend, method
registry, artifact type system, context resolver, or promotion path. If a
benchmark needs a new evolution algorithm, add it through the Core framework
catalog lifecycle. If it needs a new output shape, prefer a typed
Core artifact with a manifest before introducing backend schema changes.

Automation should preserve benchmark provenance in structured metadata while
keeping oracle answers, reference patches, secrets, and protected literals out
of learnable records. Any score that affects method selection or promotion
should be written into Core metrics, artifact scores, or review material so the
same resolver and gate logic applies across benchmarks.
