# OpenEvo: Agent System Evolution on Polar

OpenEvo builds task-facing agent-system evolution on top of Polar's rollout and
evolution backend. The current focus is to turn completed trajectories or
transcripts into safer, auditable updates to `AGENTS.md`, then feed those updates
back into later rollouts without leaking held-out answers.

The previous root README described the original Polar framework. It is preserved
as [README.polar.md](README.polar.md). Lower-level evolution backend usage lives
in [src/polar_evolution/README.md](src/polar_evolution/README.md).

## Architecture

```text
external harness or Polar rollout
        |
        v
trajectory / transcript capture
        |
        v
Polar events -> dataset artifact -> evolution job
        |
        v
method backend
        |
        v
typed artifacts: agent_system, skill_bundle, text_memory, parametric_memory
        |
        v
context resolver / harness injection -> next rollout
        |
        v
task evaluator -> sanitized feedback -> next evolution job
```

Main components:

- **Capture layer**: Polar proxy traces support token-level data. Subscription or
  external harness runs use pure-text transcript capture with
  `token_level_metrics_available=false`.
- **Offline bridges**: Terminal Bench trial/job directories can be converted into
  Polar events and datasets while excluding oracle solutions, reference patches,
  secrets, and protected literals.
- **Evolution backend**: datasets, jobs, leases, artifacts, lineage, compatibility
  filters, and context resolution are handled by the Polar EvolutionStore/API.
- **Algorithm backends**: methods in `src/polar_evolution/methods.py` consume
  dataset artifacts and produce typed artifacts.
- **Evaluators**: task-level evaluators live outside specific methods. For
  ground-truth tasks, they produce sanitized method feedback and leakage guards
  instead of exposing raw answers to the reflector.
- **Runtime injection**: promoted artifacts are resolved by compatibility and
  staged into the next agent session, for example as `AGENTS.md` for Codex or as
  Terminal Bench Harbor agent instructions.

## Implemented Algorithms

| Method | Artifact | Status | Purpose |
|---|---|---:|---|
| `agent_system` | `agent_system` | implemented | Baseline/manual registration of an existing agent-system file. |
| `agent_system_reflector` | `agent_system` | implemented | LLM-based reflector over one dataset artifact with audit and repair. |
| `agent_system_history_reflector` | `agent_system` | implemented | Reflects over multiple rounds, preserves round metrics, and marks regressions. |
| `agent_system_pareto_reflector` | `agent_system` | implemented | Generates multiple strategy candidates, records a candidate archive, and applies promotion gates. |
| `agent_system_gepa_reflector` | `agent_system` | implemented | GEPA-style closed-loop candidate generation for per-task evolution. |
| `text_memory` | `text_memory` | implemented | Distills successful records into Markdown memory. |
| `skill_bundle` | `skill_bundle` | implemented | Registers harness-loadable skill directories. |
| `parametric_memory_register` | `parametric_memory` | implemented | Registers external adapter artifacts for later trainer/inference use. |

Shared infrastructure that is already implemented:

- Golden-standard evaluator for sequence/component extraction: article-scoped
  TP/FP/FN, precision, recall, F1, duplicate counting, and leakage checks.
- Terminal Bench transcript bridge and per-task evolution runner.
- LLM reflector providers for OpenAI-compatible HTTP APIs and sandboxed Codex CLI
  subscription-mode reflection.
- Agent-system audit/repair pass to catch held-out literals and over-specific
  updates before artifact registration.

## Current Dataset Performance

These numbers are local experiment results from the currently available run
artifacts. They are useful for tracking direction, not a frozen benchmark suite.

| Dataset / setting | Method | Scope | Result | Source artifact |
|---|---|---:|---|---|
| Biology component extraction, 5 training bad cases | fixed agentic workflow baseline | 5 articles | Precision 0.715, recall 0.916, F1 0.803 | `/tmp/evolab-5-badcase-selfevolve-3rounds-fixed-20260525T160602Z/comparison_report.md` |
| Biology component extraction, 28 articles | canonical/source-gated evaluator | 28 articles | Precision 0.852, recall 0.463, F1 0.600 | `/tmp/evolab-28-biology-dynamic-openrouter-before-20260522T011453Z/evaluation_28_processed_source_gated_after_debug/article_aligned_evaluator/evaluation_summary.json` |
| Biology component extraction, round-3 self-evolved retry | posthoc deterministic evaluator | 28 articles | Precision 0.298, recall 0.717, F1 0.421 | `/tmp/evolab-28-selfevolve-round3-retry-20260526T060535Z/posthoc_deterministic_evaluation_28/promoter_eval_summary.md` |
| Terminal Bench 2.1, Codex subscription baseline | no evolution, `gpt-5.5` | 89 tasks | Mean reward 0.719; 8 errored trials | `/tmp/tb21-full-codex-gpt55-subscription-cache-20260624-085451/jobs/tb21-full-codex-gpt55-subscription-cache/result.json` |
| Terminal Bench 2.1, old EvoLab baseline | no evolution, `gpt-5.4-mini` | 89 tasks | Mean reward 0.146 | `/tmp/evolab-tb21-baseline-noevo-20260620-172605/jobs/tb21-full89-evolab-baseline-noevo-gpt54mini-20260620-172605/result.json` |
| Terminal Bench 2.1, matched official vs wrapper smoke | no evolution, `gpt-5.5` | 10 tasks | Both official Codex and wrapper subscription runs scored 0.900 | `/tmp/tb21-compare-codex-vs-wrapper-20260622-082940/jobs/` |
| Terminal Bench 2.1, GEPA per-task loop | `agent_system_gepa_reflector` | 10-task smoke | 9/9 already-passing tasks stayed passing; `filter-js-from-html` improved from 0 to 1 in generation 1 | `/tmp/tb21-gepa-loop-5task-20260624-060926/summary.json`, `/tmp/tb21-gepa-loop-remaining5-20260624-071032/summary.json` |

Key interpretation:

- The biology runs exposed a real regression pattern: broad recall rules can
  inflate recall while collapsing precision. The method-level fix is to use
  evaluator feedback, promotion gates, and history-aware reflection rather than
  blindly accepting the latest reflector output.
- The Terminal Bench wrapper can reproduce the official Codex subscription smoke
  result on a matched 10-task subset. The full 89-task no-evolution run reaches
  the expected 70%+ range, but still has verifier and agent timeout errors that
  should be tracked separately from model correctness.
- Per-task GEPA-style evolution is promising on targeted failures, but the current
  evidence is still a smoke test, not a statistically meaningful benchmark.

## Roadmap

- Make benchmark results first-class repo artifacts instead of ad hoc `/tmp`
  summaries.
- Add a stable biology 5-train/23-test split runner with canonical article-id
  mapping in the task description rather than hidden pipeline state.
- Extend per-task Terminal Bench evolution beyond `agent_system` to `skill_bundle`
  and `text_memory`.
- Add promotion policies that combine paired evaluator scores, leakage audit,
  regression limits, and candidate diversity.
- Improve transcript capture fidelity for external harnesses while preserving the
  no-oracle/no-secret boundary.
- Support multi-round evolution from all historical trajectories, not only the
  most recent round.
- Add dashboards for coverage collapse, repeated failure modes, and over-specific
  reflector updates.
- Integrate parametric-memory training and adapter promotion as a backend, not
  just artifact registration.

## Entry Points

- Backend CLI and worker code: `src/polar_evolution/`
- Evolution method implementations: `src/polar_evolution/methods.py`
- Golden-standard evaluator: `src/polar_evolution/golden_standard.py`
- Terminal Bench bridge: `src/polar_evolution/terminal_bench_bridge.py`
- Per-task Terminal Bench loop: `src/polar_evolution/terminal_bench_per_task.py`
- Architecture docs: `docs/architecture/evolution-api-and-method-integration.md`
- Original Polar README: [README.polar.md](README.polar.md)
