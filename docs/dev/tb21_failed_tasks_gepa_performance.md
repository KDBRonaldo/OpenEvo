# Terminal Bench 2.1 GEPA On Failed Tasks

Last updated: 2026-06-29.

This file records how per-task `agent_system` GEPA performed on the Terminal
Bench 2.1 failed task set from
`docs/dev/tb21_codex_gpt55_failed_tasks.md`.

Treat this as an experiment reference, not a leaderboard claim.

## Source Runs

- Baseline failed-task reference:
  `docs/dev/tb21_codex_gpt55_failed_tasks.md`
- Qualitative reflection examples:
  `docs/dev/tb21_gepa_reflection_examples.md`
- Misevolution/rescue analysis:
  `docs/dev/tb21_gepa_misevolution_rescue_analysis.md`
- Main continue-on-failure GEPA run:
  `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706`
- Main run mode: `per-task-continue-on-failure`
- Main run started: `2026-06-26T16:07:06Z`
- Main run finished: `2026-06-27T22:30:27Z`
- Evolution method: `agent_system_gepa_reflector`
- Agent and reflector model: `gpt-5.5`
- GEPA settings: 2 candidates per generation, 3 generations.

Earlier retry runs provide evidence for tasks that did not receive a clean
summary in the main run:

- `/tmp/tb21-failed-agent-system-gepa-20260626-124347`
- `/tmp/tb21-failed-agent-system-gepa-remaining-setsid-20260626-133053`
- `/tmp/tb21-failed-agent-system-gepa-remaining-after-sam-20260626-155002`

Early-stop GEPA pass@5 follow-up runs:

- `/tmp/tb21-pass5-20260628-070012/summary.json`
- `/tmp/tb21-gepa-pass5-missing-20260629-030928/summary.json`

## Status Meanings

- `rescued_clean`: clean per-task summary exists and selected/observed reward is
  `1.0`.
- `still_failed_clean`: clean per-task summary exists and all observed candidate
  rewards were `0.0`.
- `rescued_observed_no_summary`: at least one candidate trial reached reward
  `1.0`, but the run did not produce a clean per-task summary.
- `no_clean_result`: run failed before a usable summary or successful candidate.
- `not_run_infra_stuck`: no candidate result because the task was skipped for
  verifier infrastructure issues.

## Rollup

- Baseline failed/non-pass tasks: 25.
- Clean per-task GEPA summaries for baseline failed tasks: 19.
- Extra clean per-task summary outside the failed set: `tune-mjcf`.
- Tasks with observed GEPA candidate reward `1.0`: 17.
- Tasks still failed in clean summaries: 6.
- Tasks without any successful candidate or clean result: 2 (`sam-cell-seg`,
  `vulnerable-secret`).
- Tasks with observed reward `1.0` but no clean summary: 4
  (`chess-best-move`, `pypi-server`, `protein-assembly`, `train-fasttext`).

The strongest caveat is that "observed reward `1.0`" is a single candidate
trial result. For tasks without a clean summary, it is not yet a fully resumed
promotion/selection result.

## GEPA Pass@5 Follow-Up

The first GEPA pass@5 run covered the six tasks with clean historical GEPA
pass@1 reward `0.0`. The supplemental run covered the remaining two tasks that
had no usable historical GEPA pass@1 result. Already-run tasks were skipped in
the supplemental run.

| Historical GEPA pass@1 failed/no-result task | GEPA pass@5 result | Attempts | Evidence |
| --- | --- | ---: | --- |
| `dna-insert` | failed | 5 | `/tmp/tb21-pass5-20260628-070012/summary.json` |
| `filter-js-from-html` | failed | 5 | `/tmp/tb21-pass5-20260628-070012/summary.json` |
| `make-doom-for-mips` | failed | 5 | `/tmp/tb21-pass5-20260628-070012/summary.json` |
| `pytorch-model-recovery` | passed | 3 | `/tmp/tb21-pass5-20260628-070012/summary.json` |
| `raman-fitting` | failed | 5 | `/tmp/tb21-pass5-20260628-070012/summary.json` |
| `sam-cell-seg` | passed | 1 | `/tmp/tb21-gepa-pass5-missing-20260629-030928/summary.json` |
| `video-processing` | passed | 4 | `/tmp/tb21-pass5-20260628-070012/summary.json` |
| `vulnerable-secret` | passed | 1 | `/tmp/tb21-gepa-pass5-missing-20260629-030928/summary.json` |

Rollup for this subset:

```text
GEPA pass@1 failed/no-result tasks: 8
GEPA pass@5 passed: 4
GEPA pass@5 failed: 4
```

## Per-Task GEPA Performance

| Task | Baseline reward | Baseline exception | GEPA status | Observed best reward | Evidence |
| --- | ---: | --- | --- | ---: | --- |
| `chess-best-move` | 0.0 |  | `rescued_observed_no_summary` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-remaining-setsid-20260626-133053/tasks/chess-best-move` |
| `compile-compcert` | 0.0 | `AgentTimeoutError` | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/compile-compcert.json` |
| `configure-git-webserver` | 0.0 |  | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/configure-git-webserver.json` |
| `dna-insert` | 0.0 |  | `still_failed_clean` | 0.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/dna-insert.json` |
| `filter-js-from-html` | 0.0 |  | `still_failed_clean` | 0.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/filter-js-from-html.json` |
| `gcode-to-text` | 0.0 |  | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/gcode-to-text.json` |
| `large-scale-text-editing` | 0.0 |  | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/large-scale-text-editing.json` |
| `make-doom-for-mips` | 0.0 | `AgentTimeoutError` | `still_failed_clean` | 0.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/make-doom-for-mips.json` |
| `make-mips-interpreter` | 0.0 |  | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/make-mips-interpreter.json` |
| `mteb-retrieve` | null | `VerifierTimeoutError` | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/mteb-retrieve.json` |
| `overfull-hbox` | 0.0 |  | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/overfull-hbox.json` |
| `password-recovery` | 0.0 | `NonZeroAgentExitCodeError` | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/password-recovery.json` |
| `protein-assembly` | 0.0 |  | `rescued_observed_no_summary` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-remaining-after-sam-20260626-155002/tasks/protein-assembly` |
| `pypi-server` | 0.0 |  | `rescued_observed_no_summary` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-20260626-124347/tasks/pypi-server` |
| `pytorch-model-cli` | null | `VerifierTimeoutError` | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/pytorch-model-cli.json` |
| `pytorch-model-recovery` | 0.0 |  | `still_failed_clean` | 0.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/pytorch-model-recovery.json` |
| `qemu-alpine-ssh` | 0.0 |  | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/qemu-alpine-ssh.json` |
| `query-optimize` | 0.0 |  | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/query-optimize.json` |
| `raman-fitting` | 0.0 |  | `still_failed_clean` | 0.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/raman-fitting.json` |
| `regex-chess` | 0.0 |  | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/regex-chess.json` |
| `sam-cell-seg` | 0.0 |  | `not_run_infra_stuck` | null | `/tmp/tb21-failed-agent-system-gepa-remaining-after-sam-20260626-155002/run-info.txt` |
| `torch-pipeline-parallelism` | null | `VerifierTimeoutError` | `rescued_clean` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/torch-pipeline-parallelism.json` |
| `train-fasttext` | 0.0 |  | `rescued_observed_no_summary` | 1.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/tasks/train-fasttext` |
| `video-processing` | 0.0 |  | `still_failed_clean` | 0.0 | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/summaries/video-processing.json` |
| `vulnerable-secret` | 0.0 | `NonZeroAgentExitCodeError` | `no_clean_result` | null | `/tmp/tb21-failed-agent-system-gepa-per-task-20260626-160706/tasks/vulnerable-secret` |
