# Terminal Bench 2.1 Continual Parametric-Memory Evaluation

This note records the first controlled evaluation of OpenEvo's internal
`parametric_memory_sd_lora` method. It is development evidence, not a release
gate or an official Terminal Bench submission.

The consolidated textual- and parametric-memory results are in
[Terminal Bench 2.1 Memory Method Results](memory-method-experiment-results.md).

## Scope

The experiment isolates parametric memory:

- enabled evolution target: `parametric_memory`;
- disabled evolution targets: `text_memory`, `skill_bundle`, and
  `agent_system`;
- inference path: OpenEvo Core `CodexHarness` -> OpenEvo Gateway -> local vLLM;
- compared conditions: fixed base model, ordinary sequential LoRA, and the
  OpenEvo replay-assisted SD-LoRA adaptation;
- one ordered training generation per task, with every learned generation
  evaluated on every task in the fixed stream.

The benchmark does not expose another inference API. It installs a pinned host
Codex payload into each Harbor container and verifies its version and hashes
before the task starts. Training is a fixed Daemon-owned subprocess and uses
successful prior trajectories as user-task adapter data.

## Algorithm Fidelity

The original SD-LoRA method is rehearsal-free. Its plain formulation adds a new
low-rank direction for each task, so rank growth is part of the upstream method;
the upstream rank-reduction and knowledge-distillation variants control that
growth without replay.

OpenEvo retains the direction/magnitude decomposition and one cumulative
adapter, but adds bounded successful-trajectory replay as its retention signal.
The artifact therefore records `paper_equivalent=false`,
`rehearsal_free=false`, and
`retention_strategy=bounded_trajectory_replay`. Historical directions remain
frozen while their magnitudes and the new direction are trained. This document
uses "replay-assisted SD-LoRA adaptation" for OpenEvo results; it does not claim
performance for the original rehearsal-free algorithm. Rank reduction and
knowledge distillation are not implemented in the current method.

## Algorithm Sanity Check

Before adapting SD-LoRA to language-model adapters, the upstream repository at
commit `8bacded6eb44786db071f66fb90a87dd660d94ea` was run on its CIFAR-100
10-task configuration with seed 1993. The only source adjustment made the data
loader worker count environment-selectable; the algorithm and experiment
configuration were unchanged.

Our run reached average incremental accuracy 91.536, final accuracy 86.76, and
forgetting 5.60. The author's checked-in log reports 92.051, 87.26, and 6.411,
respectively. This is evidence that the upstream implementation runs in the
local environment. It is not OpenEvo or Terminal Bench performance evidence:
the upstream method operates on a vision transformer and its task classifier,
whereas OpenEvo adapts the decomposition to cumulative PEFT language-model
adapters.

Retained local evidence:

- run log:
  `/tmp/sdlora-upstream-run-20260729/logs/sdlora/cifar224/0/10/reproduce_1993_vit_base_patch16_224.log`;
- author log:
  `/tmp/SD-Lora-CL-review-20260729/logs/C100.log`.

## Daemon Two-Generation Smoke

A direct GPU 3 smoke trained two consecutive generations with
`Qwen/Qwen3-4B-Instruct-2507`. Generation 1 consumed and validated generation
0's decomposition state rather than starting a separate task-routed adapter.

| Generation | Components | Effective rank | Loss | Train time | Peak allocated GPU memory |
|---:|---:|---:|---:|---:|---:|
| 0 | 1 | 2 | 6.896610 | 10.016 s | 8,204,034,560 bytes |
| 1 | 2 | 4 | 5.132625 | 13.736 s | 8,209,158,144 bytes |

Evidence:
`/tmp/openevo-sd-lora-gpu3-two-generation-20260730-v2/result.json`.

## Terminal Bench Reproduction

The first end-to-end run used this command:

```bash
openevo-terminal-bench terminal-bench-continual-memory-eval \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id openssl-selfsigned-cert \
  --training-trial openssl-selfsigned-cert=/root/EvoLabCore-terminal-bench-task-package/jobs/tb21-evolab-full-gpt54mini-noproxy-20260609-184645/openssl-selfsigned-cert__LSCv4mb \
  --task-id prove-plus-comm \
  --training-trial prove-plus-comm=/root/EvoLabCore-terminal-bench-task-package/jobs/tb21-evolab-full-gpt54mini-noproxy-20260609-184645/prove-plus-comm__Fr3g3fF \
  --run-root /tmp/tb21-continual-memory-qwen3-4b-20260730-v1 \
  --terminal-bench-package-root /root/EvoLabCore-terminal-bench-task-package \
  --model Qwen/Qwen3-4B-Instruct-2507 \
  --model-revision cdbee75f17c01a7cc42f958dc650907174af0554 \
  --codex-version 0.144.1 \
  --gpu 3 \
  --vllm-executable /root/evolab-vllm/bin/vllm \
  --vllm-port 8000 \
  --gateway-port 8100 \
  --gateway-advertise-host 172.17.0.8 \
  --max-model-length 16384 \
  --agent-timeout-seconds 3600 \
  --rank 8 \
  --target-module q_proj \
  --target-module k_proj \
  --target-module v_proj \
  --target-module o_proj \
  --learning-rate 0.0002 \
  --epochs 1 \
  --max-steps 1 \
  --max-length 2048 \
  --max-records 1 \
  --gradient-accumulation-steps 1 \
  --no-gradient-checkpointing \
  --seed 1993 \
  --trainer-timeout-seconds 900 \
  --output /tmp/tb21-continual-memory-qwen3-4b-20260730-v1/report.json
```

The command produced 10 Harbor attempts: two base attempts plus two generations
times two tasks for each of ordinary LoRA and SD-LoRA. Every attempt completed
without an infrastructure exception.

## Results

| Condition | Reward matrix | Final average | Anytime average | BWT | Forgetting |
|---|---|---:|---:|---:|---:|
| Base | `[0, 0]` | 0.000 | 0.000 | n/a | n/a |
| Ordinary sequential LoRA | `[[0, 0], [0, 0]]` | 0.000 | 0.000 | 0.000 | 0.000 |
| Replay-assisted SD-LoRA adaptation | `[[0, 0], [0, 0]]` | 0.000 | 0.000 | 0.000 | 0.000 |

Neither adapter method improved reward in this smoke. The resource measurements
were:

| Method | Generation | Effective rank | Loss | Adapter bytes | Train time | Peak allocated GPU memory |
|---|---:|---:|---:|---:|---:|---:|
| Ordinary LoRA | 0 | 8 | 2.589730 | 23,637,556 | 8.942 s | 12,276,156,416 bytes |
| Ordinary LoRA | 1 | 8 | 3.185173 | 23,637,556 | 7.270 s | 11,033,436,672 bytes |
| SD-LoRA | 0 | 8 | 2.589730 | 47,277,124 | 12.652 s | 12,284,680,704 bytes |
| SD-LoRA | 1 | 16 | 2.603484 | 94,497,453 | 13.362 s | 11,232,557,568 bytes |

The SD-LoRA artifact grows with the cumulative components and also carries the
decomposition state needed by the next generation. Training loss and resource
measurements are diagnostics, not held-out reward. Harbor attempt latency is
not reported as serving latency because it includes container setup, task
execution, and verification.

The canonical report is
`/tmp/tb21-continual-memory-qwen3-4b-20260730-v1/report.json`, with SHA-256
`ee9f72d8f0b936b880a3ca93d5d46aef3ee4a845e3b17b3b3d1114c000acf9bd`.
The same command with `--dry-run` was revalidated against the final CLI and
wrote a plan with the complete normalized training configuration to
`/tmp/tb21-continual-memory-qwen3-4b-20260730-v1/reproduction-plan.json`; its
SHA-256 is
`599e1a5860c94a583c98df865043c594697010d71c2ba16b4d11e3dc98927057`.

## Interpretation And Next Gate

This run establishes the lifecycle boundary: user trajectories become a
Daemon-trained cumulative adapter, the next generation validates and extends
the prior continual state, vLLM loads each cumulative PEFT adapter, and ordinary
task inference remains on the existing Core harness path. It does not establish
a Terminal Bench performance gain.

The next performance gate must predeclare a larger frozen task stream, use more
than one optimization step and one trajectory per generation, and repeat task
attempts or specify another uncertainty protocol. It must retain all task-level
rewards and distinguish infrastructure failures from verifier failures and
model failures. Serving latency, if evaluated, needs an isolated request-level
measurement rather than Harbor wall time.

## Corrected Replay-Assisted Gain Smoke

On 2026-08-01, a second controlled run addressed the two main defects exposed
by the first smoke:

- training examples now project successful Harbor ATIF turns into the exact
  messages and tools contract captured from the live base-model
  `CodexHarness -> Gateway` requests, and loss is restricted to the projected
  assistant targets;
- each historical component is stored as a frozen global unit-Frobenius
  direction, while its learned magnitude is restored and jointly optimized
  with the new direction and magnitude. A bounded successful-trajectory replay
  buffer supplies retention data without adding task routing.

The fixed base was `Qwen/Qwen3-4B-Instruct-2507` at immutable revision
`cdbee75f17c01a7cc42f958dc650907174af0554`, served through vLLM on GPU 3.
Codex `0.146.0` performed all task inference through the existing OpenEvo
Gateway. Only `parametric_memory` evolution was enabled; ordinary sequential
LoRA was deliberately skipped in this run. The ordered tasks were
`prove-plus-comm` and `regex-log`, with one successful GPT-5.5 teacher trial
projected for each task. Rank 16 components targeted `q_proj`, `k_proj`,
`v_proj`, `o_proj`, `gate_proj`, `up_proj`, and `down_proj`; direction and
magnitude learning rates were `1e-4` and `1e-2`, respectively.

| Condition / generation | `prove-plus-comm` | `regex-log` | Mean |
|---|---:|---:|---:|
| Fixed base | 0 | 0 | 0.000 |
| Replay-assisted generation 0 | 1 | 0 | 0.500 |
| Replay-assisted generation 1 | 1 | 1 | 1.000 |

The resulting reward matrix was `[[1, 0], [1, 1]]`. Final average and anytime
average were both 1.000, backward transfer was 0.000, and forgetting was
0.000. All six Harbor attempts returned finite rewards without infrastructure
exceptions. The first task's reward remained 1 after learning the second task.

| Generation | Components | Effective rank | Current / replay records | Steps | Loss | Train time | Peak allocated GPU memory |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 1 | 16 | 5 / 0 | 100 | 0.355367 | 238.333 s | 23,794,438,656 bytes |
| 1 | 2 | 32 | 12 / 12 | 200 | 0.171579 | 603.307 s | 30,517,412,352 bytes |

Generation 1 restored the first component magnitude and updated it from
`4.083327` to `3.995929`; the new component magnitude was `5.577014`. Both
serving commands used `--max-loras 1`. Generation 1 loaded one rank-32
cumulative adapter, not two task adapters, and every condition reproduced the
same per-task Gateway contract digest.

The source report has SHA-256
`0e69820410ba56a707d2269ede5a22cdd7cbe3f36d373db216f9f4aad0627ff8`.
Its claim-relevant fields are retained in the
[canonical result record](memory-method-experiment-results.md#corrected-positive-smoke-results).

This is positive end-to-end evidence that the replay-assisted OpenEvo path can
learn two selected base failures without forgetting the first one. It is not a
result for rehearsal-free SD-LoRA. It remains a conditional two-task,
one-attempt smoke: the tasks and successful teacher trajectories are not an
unbiased sample, ordinary LoRA was not rerun under this larger budget, and no
confidence interval is available. It therefore does not replace the larger
frozen, repeated Terminal Bench performance gate described above.
