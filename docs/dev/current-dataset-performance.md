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
