# OpenEvo Dev Kit

OpenEvo Dev Kit is the developer wrapper around OpenEvo Core. It includes CLI entrypoints, Python-facing Core facades, benchmark adapters, method development helpers, artifact inspection, and regression-test fixtures.

Desktop is not a developer console. Developer benchmark work should use Dev Kit and should produce Core records, datasets, metrics, jobs, artifacts, and context inputs.

## Ownership

Dev Kit owns OpenEvo workflows that require source checkout, command-line
automation, benchmark integration, or method-development context:

- CLI workflows such as `openevo run`, `openevo science`, and `openevo sidecar`.
- Source-facing Python helpers and Core facades used by tests and benchmark
  adapters.
- Regression-test fixtures for Core records, datasets, jobs, artifacts, context
  resolution, and runtime injection.
- Benchmark adapters that ingest external benchmark tasks or results.
- Method-development helpers for inspecting capabilities, constructing jobs,
  registering artifacts, and comparing outputs.
- Artifact inspection and debugging tools for developer or internal methods.

OpenEvo Desktop owns the ordinary-user science workflow. It should help a
science user configure a project, connect a remote machine, run Core-backed
experiments, and monitor results. It should not expose benchmark controls,
method registry editing, artifact debugging, raw backend mutations, or hidden
developer-console behavior.

## Method Metadata Lifecycle

OpenEvo Core is the source of truth for built-in method capabilities. Desktop
and Dev Kit must not maintain separate hardcoded method tables.

The lifecycle for a new built-in method is:

1. Add the method implementation to `polar_evolution.methods.METHOD_REGISTRY`.
2. Add matching `METHOD_METADATA` with the same method ID.
3. Include visibility, display text, artifact target, input requirements,
   supported execution modes, default config, config schema, and stability level.
4. Expose the metadata through `openevo.core.capabilities`.
5. Let Desktop filter to ordinary-user-visible methods with
   `visible_in_desktop=true`.
6. Let Dev Kit inspect the broader set of `ordinary_user`, `dev_kit`, and
   internal methods when developer workflows need them.

`visibility=ordinary_user` methods are eligible for Desktop when also marked
Desktop-visible. `visibility=dev_kit` methods are for benchmark, research, and
debugging workflows. `visibility=internal` methods are plumbing or migration
surfaces and should stay out of ordinary-user UI.

## Benchmark Adapters

Benchmark adapters translate external benchmark tasks, transcripts, scores, and
artifacts into the OpenEvo Core contract. They may normalize benchmark-specific
metadata, redact protected material, and compute metrics, but they must produce
Core records, datasets, metrics, jobs, artifacts, and context inputs.

Benchmark adapters must not implement a separate evolution backend, method
registry, artifact type system, context resolver, or promotion path. If a
benchmark needs a new evolution algorithm, add it through the Core method
registry and metadata lifecycle. If it needs a new output shape, prefer a typed
Core artifact with a manifest before introducing backend schema changes.

Adapters should preserve benchmark provenance in structured metadata while
keeping oracle answers, reference patches, secrets, and protected literals out
of learnable records. Any score that affects method selection or promotion
should be written into Core metrics, artifact scores, or review material so the
same resolver and gate logic applies across benchmarks.
