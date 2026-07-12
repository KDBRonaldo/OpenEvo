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

## Method Metadata Lifecycle

OpenEvo Core is the source of truth for built-in method capabilities. Desktop
and developer workflow utilities must not maintain separate hardcoded method
tables.

The lifecycle for a new built-in method is:

1. Add the method implementation to `openevo.evolution.methods.METHOD_REGISTRY`.
2. Add matching `METHOD_METADATA` with the same method ID.
3. Include visibility, display text, artifact target, input requirements,
   supported execution modes, default config, config schema, and stability level.
4. Expose the metadata through `openevo.capabilities`.
5. Let Desktop filter to ordinary-user-visible methods with
   `visible_in_desktop=true`.
6. Let developer workflow utilities inspect the broader set of `ordinary_user`,
   `dev_kit`, and internal methods when method development or benchmark work
   needs them.

`visibility=ordinary_user` methods are eligible for Desktop when also marked
Desktop-visible. `visibility=dev_kit` methods are for benchmark, research, and
debugging workflows. `visibility=internal` methods are plumbing or migration
surfaces and should stay out of ordinary-user UI.

The historical metadata value `dev_kit` means source-checkout developer,
benchmark, or maintainer workflow visibility only. It does not define or imply
a separately released developer product surface.

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
benchmark needs a new evolution algorithm, add it through the Core method
registry and metadata lifecycle. If it needs a new output shape, prefer a typed
Core artifact with a manifest before introducing backend schema changes.

Automation should preserve benchmark provenance in structured metadata while
keeping oracle answers, reference patches, secrets, and protected literals out
of learnable records. Any score that affects method selection or promotion
should be written into Core metrics, artifact scores, or review material so the
same resolver and gate logic applies across benchmarks.
