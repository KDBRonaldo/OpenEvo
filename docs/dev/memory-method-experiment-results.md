# Terminal Bench 2.1 Memory Method Results

This document is the canonical development record for the MemEvolve and
SD-LoRA experiments completed through 2026-08-01. These are controlled local
experiments, not official Terminal Bench submissions, release gates, or
full-suite performance claims.

## Result Summary

| Target | OpenEvo method | Inference mode | Evaluation scope | Observed result |
|---|---|---|---|---|
| Textual memory | `text_memory_memevolve` | Codex subscription, `gpt-5.5` | 21 tasks selected from an earlier baseline-failure pool, one attempt per condition | Corrected baseline 2/21; evolved 9/21; +7 successes and +33.33 percentage points |
| Parametric memory | `parametric_memory_sd_lora` | Local Qwen3-4B through Core and Gateway | Two selected tasks, one attempt per task and generation | Base `[0, 0]`; replay-assisted generation matrix `[[1, 0], [1, 1]]`; final average 1.000 |

The rows are not comparable with each other. They use different models,
inference modes, task selections, training signals, and evaluation protocols.

## MemEvolve Declarative Textual Memory

### Method identity

The upstream [MemEvolve paper](https://icml.cc/virtual/2026/poster/61379) and
[repository](https://github.com/bingreeky/MemEvolve) evolve executable memory
providers that can retrieve, ingest, manage, validate, and evaluate memory.
OpenEvo does not execute model-generated Python inside the Daemon. Its
`text_memory_memevolve` method instead generates three independent static
Markdown candidates from trajectory evidence, selects one with a Codex evidence
judge, and publishes one `text_memory` artifact for the next session.

The artifact contract records:

- `algorithm_family=MemEvolve`;
- `adaptation_scope=declarative_text_memory_v1`;
- `provider_runtime=static_markdown`;
- `paper_equivalent=false`.

This result is therefore evidence for the OpenEvo declarative textual
adaptation. It is not a reproduction of the upstream executable-provider
algorithm or its reported benchmark performance.

### Protocol

- Benchmark: Terminal Bench 2.1.
- Model and auth: `gpt-5.5` through Codex subscription.
- Enabled evolution target: `text_memory` only.
- Disabled targets: `skill_bundle`, `agent_system`, and `parametric_memory`.
- Method: `text_memory_memevolve`.
- Scope: a frozen 21-task set selected from an earlier baseline-failure pool.
- Attempts: one baseline observation and one evolved attempt per task.
- Evolution: one memory generation per task; three method candidates per
  generated artifact.

Two source baseline trials, `pytorch-model-cli` and `mteb-retrieve`, had null
rewards after verifier timeouts. Later baseline reruns passed, so the paired
analysis corrects those two baseline rewards from null to 1. The uncorrected
runner summary reported baseline 0/21; all aggregate claims below use the
corrected paired summary.

### Task-level results

| Task | Corrected baseline | MemEvolve adaptation | Transition |
|---|---:|---:|---|
| `gcode-to-text` | 0 | 0 | fail to fail |
| `password-recovery` | 0 | 1 | fail to pass |
| `large-scale-text-editing` | 0 | 0 | fail to fail |
| `filter-js-from-html` | 0 | 0 | fail to fail |
| `chess-best-move` | 0 | 1 | fail to pass |
| `dna-insert` | 0 | 0 | fail to fail |
| `overfull-hbox` | 0 | 1 | fail to pass |
| `configure-git-webserver` | 0 | 0 | fail to fail |
| `pypi-server` | 0 | 1 | fail to pass |
| `pytorch-model-recovery` | 0 | 0 | fail to fail |
| `raman-fitting` | 0 | 0 | fail to fail |
| `qemu-alpine-ssh` | 0 | 1 | fail to pass |
| `video-processing` | 0 | 0 | fail to fail |
| `protein-assembly` | 0 | 0 | fail to fail |
| `regex-chess` | 0 | 1 | fail to pass |
| `pytorch-model-cli` | 1 | 1 | pass to pass |
| `query-optimize` | 0 | 0 | fail to fail |
| `make-mips-interpreter` | 0 | 0 | fail to fail |
| `vulnerable-secret` | 0 | 1 | fail to pass |
| `mteb-retrieve` | 1 | 1 | pass to pass |
| `train-fasttext` | 0 | 0 | fail to fail |

Aggregate results:

- corrected baseline pass@1: 2/21 = 0.0952;
- evolved pass@1: 9/21 = 0.4286;
- paired change: +7 tasks and +0.3333 absolute success rate;
- transitions: 7 fail-to-pass, 2 pass-to-pass, 12 fail-to-fail, and no
  pass-to-fail transitions.

The source `summary.json` has SHA-256
`6e272638fdce9d328a9f4c954bf58d88646ccbb4b27319df1140afbbbbb09885`.
The corrected paired summary has SHA-256
`1ba21c752d91d184fdd0aac8f5050e6597b212679ae06791293a18b390916b8e`.

### Interpretation

This is positive conditional evidence: the declarative memory adaptation
rescued seven tasks without losing either corrected baseline success. It is not
an unbiased 89-task score. Task selection used prior baseline outcomes, each
condition has one stochastic attempt, and the run did not record a matched
ExpeL condition, confidence interval, model cost, total wall time, or isolated
retrieval overhead. Issue
[#238](https://github.com/CompLifeLab-ZJU/OpenEvo/issues/238) remains open for
the upstream smoke, a closed provider runtime, and a larger repeated gate.

## Replay-Assisted SD-LoRA Parametric Memory

### Method identity

The upstream [SD-LoRA paper](https://arxiv.org/abs/2501.13198) and
[repository](https://github.com/WuYichen-97/SD-Lora-CL) describe a
rehearsal-free class-incremental vision method. Plain SD-LoRA adds a new
low-rank direction as tasks arrive, so rank growth is part of that method
family. Its rank-reduction and knowledge-distillation variants address that
growth without trajectory replay.

OpenEvo adapts the direction/magnitude decomposition to language-model PEFT
adapters, but adds bounded successful-trajectory replay for retention. It
freezes historical A/B directions, restores and jointly optimizes their
magnitudes with one new direction, and folds every generation into one
cumulative serving adapter. It does not route by task or serve an independent
adapter bank.

The artifact contract records:

- `algorithm_family=SD-LoRA`;
- `paper_equivalent=false`;
- `rehearsal_free=false`;
- `retention_strategy=bounded_trajectory_replay`;
- `routing_mode=single_cumulative_adapter`.

Accordingly, the OpenEvo result must be called the replay-assisted SD-LoRA
adaptation, not a result for the original rehearsal-free algorithm. Rank
reduction and knowledge distillation are not implemented in this method.

### Upstream and lifecycle checks

The pinned upstream commit
`8bacded6eb44786db071f66fb90a87dd660d94ea` reproduced CIFAR-100 average
incremental accuracy 91.536 and final accuracy 86.76 with seed 1993. The
author's checked-in log reports 92.051 and 87.26. This only verifies that the
upstream vision code ran in the local environment.

The first matched OpenEvo lifecycle smoke used two tasks and one optimizer step
per generation. Base, ordinary sequential LoRA, and the OpenEvo SD-LoRA
adaptation all produced zero reward:

| Condition | Reward matrix | Final average |
|---|---|---:|
| Fixed base | `[0, 0]` | 0.000 |
| Ordinary sequential LoRA | `[[0, 0], [0, 0]]` | 0.000 |
| Replay-assisted SD-LoRA adaptation | `[[0, 0], [0, 0]]` | 0.000 |

This negative run established the two-generation artifact and serving
lifecycle, but not a performance gain. Its report has SHA-256
`ee9f72d8f0b936b880a3ca93d5d46aef3ee4a845e3b17b3b3d1114c000acf9bd`.

### Corrected positive smoke protocol

- Benchmark: Terminal Bench 2.1.
- Base model: `Qwen/Qwen3-4B-Instruct-2507` at immutable revision
  `cdbee75f17c01a7cc42f958dc650907174af0554`.
- Inference: Codex `0.146.0` through `CodexHarness -> Gateway -> local vLLM`.
- Hardware: GPU 3, BF16, one visible CUDA device.
- Enabled evolution target: `parametric_memory` only.
- Disabled targets: `text_memory`, `skill_bundle`, and `agent_system`.
- Ordered tasks: `prove-plus-comm`, then `regex-log`.
- Attempts: one per task for the base and each learned generation.
- Training data: successful teacher trajectories projected to the exact live
  messages/tools contract, with loss only on assistant target tokens.
- LoRA rank per task: 16 across `q_proj`, `k_proj`, `v_proj`, `o_proj`,
  `gate_proj`, `up_proj`, and `down_proj`.
- Optimizer budget: 20 epochs, at most 200 steps, batch size 1, sequence length
  16,384, direction learning rate `1e-4`, magnitude learning rate `1e-2`, and
  replay capacity 64.

### Corrected positive smoke results

| Condition / generation | `prove-plus-comm` | `regex-log` | Mean |
|---|---:|---:|---:|
| Fixed base | 0 | 0 | 0.000 |
| Replay-assisted generation 0 | 1 | 0 | 0.500 |
| Replay-assisted generation 1 | 1 | 1 | 1.000 |

The reward matrix was `[[1, 0], [1, 1]]`. Final average and anytime average
were 1.000, backward transfer and forgetting were 0.000, and the first task
retained reward 1 after the second task was learned.

| Generation | Effective rank | Current / replay records | Steps | Loss | Train time | Adapter bytes | Peak GPU memory |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 16 | 5 / 0 | 100 | 0.355367 | 238.333 s | 264,563,074 | 23,794,438,656 bytes |
| 1 | 32 | 12 / 12 | 200 | 0.171579 | 603.307 s | 529,308,990 | 30,517,412,352 bytes |

Generation 1 updated the first component magnitude from `4.083327` to
`3.995929` and learned a new magnitude of `5.577014`. Both generations served
exactly one cumulative adapter with `--max-loras 1`; effective rank grew from
16 to 32 because one rank-16 direction was added per task.

The report has SHA-256
`0e69820410ba56a707d2269ede5a22cdd7cbe3f36d373db216f9f4aad0627ff8`.

### Interpretation

This is positive end-to-end evidence that the current OpenEvo adaptation can
fit two selected base failures and retain the first success after one more
generation. It is not evidence that replay is required, that the original
rehearsal-free SD-LoRA works on language-agent tasks, or that this method beats
ordinary LoRA. The positive run deliberately skipped the matched ordinary-LoRA
condition, used selected successful teacher trajectories, and has one attempt
per cell with no uncertainty estimate. Issue
[#239](https://github.com/CompLifeLab-ZJU/OpenEvo/issues/239) remains open for
the larger frozen stream, matched ordinary-LoRA control, uncertainty protocol,
serving-latency measurement, rank-growth controls, and product-level successor
activation.

## Implementation And Evidence Retention

No retained source code depends on a `/tmp` worktree:

- Core-owned Codex method inference merged through
  [PR #241](https://github.com/CompLifeLab-ZJU/OpenEvo/pull/241).
- The declarative MemEvolve method merged through
  [PR #244](https://github.com/CompLifeLab-ZJU/OpenEvo/pull/244).
- The Daemon-trained continual parametric-memory method and its corrected
  training path merged through
  [PR #256](https://github.com/CompLifeLab-ZJU/OpenEvo/pull/256).

The exact Terminal Bench plan-bound textual-memory dispatch used by the
MemEvolve run is preserved in remote draft
[PR #248](https://github.com/CompLifeLab-ZJU/OpenEvo/pull/248), but is not part
of `stable`; issue
[#245](https://github.com/CompLifeLab-ZJU/OpenEvo/issues/245) tracks that
remaining integration. The raw runtime trees and adapter weights are not
repository source and must not be committed. The task-level outcomes,
configuration, limitations, report identities, and source-code provenance
needed to audit the claims are retained in this document.
