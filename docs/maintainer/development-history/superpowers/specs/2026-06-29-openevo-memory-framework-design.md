# OpenEvo Memory Framework Controls Design

## Goal

Prepare OpenEvo for memory-only evolution experiments on Terminal Bench 2.1 while
keeping subscription-mode harness behavior explicit and reproducible.

The design separates two different memory sources:

- Harness-native memory: state maintained by the agent harness itself, such as
  Codex files under `CODEX_HOME`.
- Polar evolution memory: typed artifacts produced and injected through the
  evolution backend, currently `text_memory` and `parametric_memory`.

The first implementation must preserve existing behavior by default, expose a
safe way to clear Codex-native memory, and make memory-only ablations resistant
to stale promoted artifacts from other evolution runs.

## Current State

Textual memory is already supported through the evolution backend:

- `text_memory` and `text_memory_reflector` are worker methods.
- Text memory artifacts are resolved by the context resolver.
- The gateway writes resolved memory to `/polar/session/evolution/memory.md`,
  sets `POLAR_MEMORY_FILE`, and prepends the rendered memory to the agent
  instruction.

Parametric memory is partially supported:

- `parametric_memory_register` can register an existing adapter artifact.
- The context resolver converts compatible promoted parametric memory artifacts
  into an `adapter_merge_spec`.
- The gateway/proxy applies request-level adapter selection for supported
  serving backends.

OpenEvo does not yet expose first-class `parametric_memory` controls in
`artifacts`, and its runner only compiles `text_memory`, `skill_bundle`, and
`agent_system`. Codex subscription runs also preserve `CODEX_HOME` state but do
not provide an explicit config field for native memory policy.

## Scope

The first implementation covers:

1. A Codex-native memory policy exposed through OpenEvo agent settings.
2. First-class OpenEvo artifact controls for `parametric_memory`.
3. Strict context artifact allowlist behavior for memory-only ablations.
4. Focused tests and documentation for the new config and runtime behavior.

The implementation does not introduce a new artifact type, replace the worker
method registry, train adapters, or implement new memory mining algorithms. New
textual or parametric memory methods should continue to register as evolution
worker methods and emit typed artifacts.

## Config

OpenEvo keeps existing `artifacts.text_memory` compatibility and adds
`artifacts.parametric_memory`:

```yaml
agent:
  preset: codex
  model: gpt-5.4
  auth: subscription
  settings:
    capture_mode: transcript
    native_memory_policy: preserve

artifacts:
  text_memory:
    enabled: true
    method: text_memory_reflector

  parametric_memory:
    enabled: false
    method: parametric_memory_register

  skill_bundle:
    enabled: false

  agent_system:
    enabled: false
```

`agent.settings.native_memory_policy` accepts:

- `preserve`: keep harness-native memory. This is the default and matches the
  current behavior.
- `clear`: remove known Codex-native memory files before the run while preserving
  subscription auth and non-memory session state.

Only the Codex harness consumes this setting in the first implementation. Other
harnesses ignore it unless they explicitly add their own native-memory policy
support later.

## Codex-Native Memory Clearing

When `native_memory_policy=clear`, `CodexHarness.setup()` removes only known
Codex memory paths inside the selected `CODEX_HOME`:

- `memories/`
- `memories_*.sqlite`
- `memories_*.sqlite-shm`
- `memories_*.sqlite-wal`

The clear operation must not remove:

- `auth.json`
- `config.toml`
- `state_*.sqlite`
- `logs_*.sqlite`
- `history.jsonl`
- `session_index.jsonl`
- unrelated user files

The harness already writes `config.toml` during setup, so clearing memory should
run before or near the existing config write and use quoted paths. The behavior
should be best-effort in the same style as existing setup commands, but command
construction must avoid deleting outside `CODEX_HOME`.

## Polar Evolution Memory Controls

`artifacts.parametric_memory` mirrors the existing artifact controls:

- `enabled`: whether OpenEvo creates a parametric memory evolution job.
- `method`: worker method name, defaulting to `parametric_memory_register`.

The runner's evolution order becomes:

1. `text_memory`
2. `parametric_memory`
3. `skill_bundle`
4. `agent_system`

This keeps natural-language memory first, adapter memory before non-memory
artifacts, and preserves the existing relative order of skill and agent-system
evolution. Disabled artifact controls produce no jobs.

`parametric_memory_register` should pass through method config values that the
context resolver depends on, especially `compatibility`, `lineage`, and `scores`,
so adapter artifacts can be routed and ranked consistently with other artifact
types.

## Memory-Only Ablation

A memory-only Terminal Bench 2.1 experiment should be expressible by disabling
non-memory artifact controls:

```yaml
artifacts:
  text_memory:
    enabled: true
  parametric_memory:
    enabled: false
  skill_bundle:
    enabled: false
  agent_system:
    enabled: false
```

For parametric-memory experiments, enable `parametric_memory` and disable the
other artifact families as needed.

The runner already passes active artifact ids into rollout metadata as
`metadata.evolution.context_artifact_ids`. The context resolver must treat this
field as a strict allowlist across every artifact type, including
`parametric_memory`. This prevents compatible promoted artifacts from previous
runs from being injected into a controlled ablation.

When no context artifact ids are provided, existing promoted-artifact resolution
continues to work by compatibility and score.

## Data Flow

Round 0 uses the configured agent and native-memory policy:

```text
OpenEvo config
  -> TaskRequest.agent.settings.native_memory_policy
  -> CodexHarness.setup()
  -> optional CODEX_HOME memory cleanup
  -> rollout transcript/dataset
```

Evolution rounds use typed artifacts:

```text
dataset artifact
  -> memory worker method
  -> text_memory or parametric_memory artifact
  -> OpenEvo records latest artifact ids
  -> next rollout metadata.evolution.context_artifact_ids
  -> context resolver strict allowlist
  -> gateway runtime injection
```

## Error Handling

Invalid `native_memory_policy` values should fail config validation early through
OpenEvo's strict Pydantic models when possible. If the setting is provided
directly to the lower-level harness, Codex should fail with a clear error rather
than silently ignoring an unknown value.

`native_memory_policy=clear` should tolerate missing memory files. It should not
treat absent `CODEX_HOME/memories` or absent SQLite files as an error.

If `parametric_memory` is enabled without the method-specific config needed by
`parametric_memory_register`, the worker method should fail the job with the
existing validation error rather than the compiler inventing adapter defaults.

## Testing

Focused tests should cover:

- OpenEvo config accepts `agent.settings.native_memory_policy=preserve` and
  `clear`, and rejects invalid values.
- Codex setup preserves memory by default.
- Codex setup with `clear` emits a cleanup command for `memories/` and
  `memories_*.sqlite*` without deleting `auth.json`.
- OpenEvo compiler includes `parametric_memory` when enabled and omits it when
  disabled.
- Runner tracks latest parametric memory artifact ids for the next rollout.
- Context resolver applies `context_artifact_ids` as a strict allowlist to
  `parametric_memory`.
- `parametric_memory_register` preserves configured compatibility, lineage, and
  scores.
- Gateway adapter selection regressions continue to pass.

Suggested focused commands:

```bash
pytest tests/test_evolution_agent_harnesses.py -q
pytest tests/openevo/test_experiment_compiler.py -q
pytest tests/openevo/test_experiment_runner.py -q
pytest tests/evolution/test_artifacts_context.py -q
pytest tests/evolution/test_worker_methods.py -q
pytest tests/gateway/test_server_parametric_memory.py -q
```

## Documentation

Update documentation where behavior changes are exposed:

- `docs/architecture/` for memory artifact and context resolver behavior.
- `src/openevo/experiment/README.md` or the nearest OpenEvo experiment docs for
  config examples.
- Codex harness documentation if a module README covers native subscription
  behavior.

Docs should state that subscription auth is independent from capture mode and
from harness-native memory. Subscription experiments using pure-text evolution
still require transcript capture.

## Non-Goals

- No new memory algorithm implementation.
- No adapter training pipeline.
- No physical adapter merge inside the gateway.
- No broad rewrite of OpenEvo artifact controls into a generic backend list.
- No attempt to clear native memory for non-Codex harnesses until each harness
  has a known memory layout and auth-state boundary.
