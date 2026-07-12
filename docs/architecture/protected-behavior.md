# Protected Evolution Behavior

Status: A1 regression boundary for issue #133

This document indexes the focused tests that protect OpenEvo productization. It
does not redefine the algorithms, duplicate the canonical product spec, or lock
source files with hashes. Historical commits `53cb751c` and `21c4d648` may be
used only to resolve ambiguity about behavior that already existed.

## Protected Methods

The following behavior must remain observable after file and import moves:

| Method | Protected evidence |
| --- | --- |
| `text_memory_expel_reflector` | Prompt evidence, success/failure filtering, prior memory, the 20-record default, five required sections, artifact payload, lineage, compatibility, and missing-dataset failure. |
| `skill_bundle_reflector` | Prompt evidence, latest base skill, the 20-record default, `SKILL.md` payload, artifact lineage, redaction, and missing-dataset failure. |
| `agent_system_gepa_reflector` | Default and configured mutation strategies, candidate-count bounds, candidate prompts and payloads, report archive, unpromoted candidates, and missing-dataset failure. |

These contracts are exercised in
`tests/evolution/test_worker_methods.py`. Stable IDs and algorithm-facing input,
output, execution-mode, and default contracts for these three methods remain
covered by `tests/evolution/test_algorithm_preservation_contract.py`. Presentation,
visibility, schema, and unrelated registry entries are not frozen as algorithm
behavior.

GEPA evaluation and transition are algorithm behavior even while their code is
co-located with Terminal Bench automation. Tests in
`tests/evolution/test_terminal_bench_per_task.py` protect:

- objective value before generation and candidate-index tie-breaking;
- `None` below every numeric reward or group score;
- later generation before lower candidate index when objectives tie;
- all per-task and group candidate trials feeding the next generation;
- only the selected per-task or group result feeding the next round;
- dataset history accumulating across generations and rounds;
- `auto` choosing the plain reflector without history and the history reflector
  with all prior dataset IDs.

Productization may relocate this code but must not alter or duplicate its
selection policy. A1 adds no algorithm behavior or artifact lifecycle behavior.

## Protected Stage Boundaries

Focused probes cross the real typed boundaries instead of recreating each stage
with unrelated fixtures:

| Probe | Boundary |
| --- | --- |
| `tests/rollout/test_pipeline.py` | `TaskRequest` through scheduler reservation/dispatch, accepted-dispatch heartbeat reconciliation, typed gateway result, and cleanup. |
| `tests/gateway/test_session_lifecycle.py` | Gateway dispatch through runtime factory/start/prepare/run/stop and transcript trajectory construction. |
| `tests/gateway/test_capture_trajectory_contract.py` | Completion persistence through the token-level trajectory builder, including IDs, loss mask, and logprobs. |
| `tests/evolution/test_architecture_flow.py` | Transcript text through session event, dataset, store-backed worker, typed artifact, context resolve, and runtime memory injection. |
| `tests/gateway/test_evolution_integration.py` | Real resolved memory, skill, and agent-system context through runtime staging and `OPENEVO_*` files/environment. |

Existing trajectory, dataset/job, context resolver, runtime injection, and
harness tests continue to protect stage-local error and compatibility behavior.
Mechanical module moves may update test imports or patch points, but the typed
payloads, stage order, files, lineage, and selection results must remain equal.

## Verification

Run the A1-focused set with the repository Python 3.11 environment:

```bash
pytest -q \
  tests/evolution/test_algorithm_preservation_contract.py \
  tests/evolution/test_worker_methods.py \
  tests/evolution/test_terminal_bench_per_task.py \
  tests/evolution/test_architecture_flow.py \
  tests/gateway/test_evolution_integration.py \
  tests/gateway/test_session_lifecycle.py \
  tests/gateway/test_capture_trajectory_contract.py \
  tests/rollout/test_pipeline.py
```

These tests detect productization drift; they do not replace the three final
Terminal Bench pass@1 rescue gates in the canonical productization spec.
