# Current Dataset Performance Notes

These numbers are local development evidence from currently available run
artifacts. They are useful for tracking direction, not a frozen benchmark suite
or release claim.

| Dataset / setting | Method | Scope | Result | Source artifact |
|---|---|---:|---|---|
| Biology component extraction, 5 training bad cases | fixed agentic workflow baseline | 5 articles | Precision 0.715, recall 0.916, F1 0.803 | `/tmp/evolab-5-badcase-selfevolve-3rounds-fixed-20260525T160602Z/comparison_report.md` |
| Biology component extraction, 28 articles | canonical/source-gated evaluator | 28 articles | Precision 0.852, recall 0.463, F1 0.600 | `/tmp/evolab-28-biology-dynamic-openrouter-before-20260522T011453Z/evaluation_28_processed_source_gated_after_debug/article_aligned_evaluator/evaluation_summary.json` |
| Biology component extraction, round-3 self-evolved retry | posthoc deterministic evaluator | 28 articles | Precision 0.298, recall 0.717, F1 0.421 | `/tmp/evolab-28-selfevolve-round3-retry-20260526T060535Z/posthoc_deterministic_evaluation_28/promoter_eval_summary.md` |
| Terminal Bench 2.1, Codex subscription baseline | no evolution, `gpt-5.5` | 89 tasks | Mean reward 0.719; 8 errored trials | `/tmp/tb21-full-codex-gpt55-subscription-cache-20260624-085451/jobs/tb21-full-codex-gpt55-subscription-cache/result.json` |
| Terminal Bench 2.1, old EvoLab baseline | no evolution, `gpt-5.4-mini` | 89 tasks | Mean reward 0.146 | `/tmp/evolab-tb21-baseline-noevo-20260620-172605/jobs/tb21-full89-evolab-baseline-noevo-gpt54mini-20260620-172605/result.json` |
| Terminal Bench 2.1, matched official vs wrapper smoke | no evolution, `gpt-5.5` | 10 tasks | Both official Codex and wrapper subscription runs scored 0.900 | `/tmp/tb21-compare-codex-vs-wrapper-20260622-082940/jobs/` |
| Terminal Bench 2.1, GEPA per-task loop | `agent_system_gepa_reflector` | 10-task smoke | 9/9 already-passing tasks stayed passing; `filter-js-from-html` improved from 0 to 1 in generation 1 | `/tmp/tb21-gepa-loop-5task-20260624-060926/summary.json`, `/tmp/tb21-gepa-loop-remaining5-20260624-071032/summary.json` |
| Terminal Bench 2.1, frozen baseline-failure conditional subset | OpenEvo declarative `text_memory_memevolve`, `gpt-5.5` subscription | 21 tasks, one attempt per condition | Corrected baseline 2/21; adaptation 9/21; delta +7 tasks / +33.33 percentage points; no pass-to-fail transitions | [canonical memory experiment record](memory-method-experiment-results.md#task-level-results) |
| Terminal Bench 2.1, continual-memory lifecycle smoke | base vs ordinary sequential LoRA vs `parametric_memory_sd_lora`, Qwen3-4B | 2-task stream, one attempt and one training step | All base and adapter rewards were 0; lifecycle completed, but no performance gain was observed | [canonical memory experiment record](memory-method-experiment-results.md#upstream-and-lifecycle-checks) |
| Terminal Bench 2.1, conditional continual-memory gain smoke | base vs replay-assisted `parametric_memory_sd_lora`, Qwen3-4B | 2 selected tasks, one attempt per task/generation, 100/200 optimizer steps | Base `[0, 0]`; generation matrix `[[1, 0], [1, 1]]`; final average 1.000, BWT 0.000, forgetting 0.000 | [canonical memory experiment record](memory-method-experiment-results.md#corrected-positive-smoke-results) |

Key interpretation:

- The biology runs exposed a real regression pattern: broad recall rules can
  inflate recall while collapsing precision. The method-level fix is to use
  evaluator feedback, promotion gates, and history-aware reflection rather than
  blindly accepting the latest reflector output.
- The Terminal Bench wrapper can reproduce the official Codex subscription smoke
  result on a matched 10-task subset. The full 89-task no-evolution run reaches
  the expected 70%+ range, but still has verifier and agent timeout errors that
  should be tracked separately from model correctness.
- Per-task GEPA-style evolution is promising on targeted failures, but the
  current evidence is still a smoke test, not a statistically meaningful
  benchmark.
- The MemEvolve result is conditional on a previously selected baseline-failure
  subset and is not an unbiased full-suite score. It is evidence for the
  OpenEvo declarative adaptation, not the upstream executable-provider method.
- The first SD-LoRA run proved two-generation lifecycle continuity but was
  negative performance evidence. The corrected replay-assisted adaptation then
  improved two selected base failures while retaining the first learned task
  after generation 2. Replay means this is not the paper's rehearsal-free
  SD-LoRA method. The selected two-task, one-attempt protocol is positive
  functional evidence, not a statistically meaningful or release-level
  Terminal Bench result.
