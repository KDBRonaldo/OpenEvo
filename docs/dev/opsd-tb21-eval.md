# OPSD Terminal-Bench 2.1 实验记录

日期：2026-07-03

本文记录用 GPT-5.5 的 Terminal-Bench 2.1 transcript 蒸馏
Qwen3.5-9B 的 OPSD 实验。实验边界是：GPT transcript 只作为 OPSD
训练时的 privileged teacher context；heldout 执行阶段只让 Qwen 看到可见任务输入，
不提供 GPT transcript。

## 输入

- GPT-5.5 Terminal-Bench 2.1 源运行：
  `/tmp/tb21-full-codex-gpt55-subscription-cache-20260624-085451/jobs/tb21-full-codex-gpt55-subscription-cache`
- 转换后的 Polar dataset：
  `/tmp/openevo-opsd-tb21-gpt55/artifacts/datasets/ds_b43616aeaef54bec/records.jsonl`
- Split manifest：
  `/tmp/openevo-opsd-tb21-gpt55/splits/opsd_tb21_split_v1.json`
- OPSD smoke adapter：
  `/tmp/openevo-opsd-tb21-gpt55/qwen35-9b-opsd-4step/adapter`
- 已打包训练数据：
  `/home/liaoc/OpenEvo/opsd_tb21_gpt55_training_data_20260705.zip`

Split 摘要：

- 转换后 records：81 个完成任务，task id 唯一。
- 转换后 reward：63 pass，18 fail。
- Split seed：`opsd-tb21-gpt55-v1`。
- Split 计数：train 53，dev 12，test 12，excluded 4。
- 转换数据中存在并被 hard exclude 的任务：
  `code-from-image`, `count-dataset-tokens`, `gcode-to-text`,
  `git-leak-recovery`.
- 因 raw trial errored 而不在转换数据中的 hard exclude 任务：
  `password-recovery`, `vulnerable-secret`.

## 最近实验总结

最近的 OPSD 实验验证的是：能否把 GPT-5.5 的 Terminal-Bench transcript 当作
privileged teacher context，让 Qwen/Qwen3.5-9B 学到更好的行为。执行时 student
只看到 task prompt；GPT-5.5 transcript 只进入 OPSD loss。这保住了
privileged-context 边界，但目前实验还没有把两个目标能力拆开：

1. 更好执行任务：训练后的模型在 direct solver harness 下更稳定地解决
   Terminal-Bench 任务。
2. 更好自进化/优化：训练后的模型能产出更有用的 `text_memory`、
   `agent_system` 或 `skill_bundle`，从而提升后续 session。

当前证据：

| 实验 | 训练边界 | 评估边界 | 结果 | 解读 |
|---|---|---|---|---|
| 4-step OPSD smoke | 转换后的 GPT-success records 中很小一部分 | 3 个 direct A/C pilot tasks | A/base 1/3，C/OPSD 1/3 | 验证 serving 和 LoRA 路径可跑，但没有 reward delta。 |
| 32k/64 follow-up | 同一个 4-step smoke adapter | `build-pov-ray`, `dna-assembly`, `write-compressor` 的 process-health rerun | 无 reward gain | 32k context 和 force-collect 改善了 harness 健康度，但没有改善任务 reward。 |
| Full-success 3-round OPSD | 61 个可用 `reward=1.0` GPT-success tasks，183 steps | 4 个 train-set smoke tasks | A/base 0/4，C/OPSD 0/4 | C 改善了一个 process-health case，但官方 reward 没提升。 |
| Full-success 10-epoch OPSD | 同样 61 个任务，fresh 610-step LoRA run | 同样 4 个 smoke tasks | A/base 0/4，C/OPSD 0/3，另有一个 no-reward verifier error | 更多 epoch 没有提升 reward，还引入了一个 budget/verifier failure。 |
| 10-epoch additional task eval | 同一个 all-success 10-epoch adapter | 7 个额外 scored split-test tasks | A/base 0/7，C/OPSD 0/7 | 没有 fail-to-pass transition；process health 有好有坏。 |

最强的负面信号是：单纯增加 epoch 没有推动官方 Terminal-Bench reward。
它确实改变了一些过程行为，例如在额外任务评估里改善了 `prove-plus-comm`
和 `sanitize-git-repo` 的 process health，但也让 `mailman` 和 `build-pov-ray`
退化。这更像是优化目标和评估目标不匹配，而不是简单 undertraining。

## TB2.1 过程指标

最终 verifier reward 仍然应该是主指标，因为它代表任务是否真的解决。但对
Qwen3.5-9B 这种小模型，reward 全 0 时信息量太低；需要加过程指标来判断模型到底是在
工具使用、计划执行、测试反馈利用，还是最终修复质量上失败。

过程指标分成四层，不直接替代 verifier reward：

| 指标层级 | 指标 | 含义 | 注意事项 |
|---|---|---|---|
| 格式有效性 | valid tool-call rate、JSON/XML parse error count、unknown tool count | 模型是否能稳定产生 harness 可执行的工具调用 | 只说明接口对齐，不说明任务正确。 |
| 执行成功率 | tool exit success count、failed command count、timeout count | 工具调用是否能成功执行 | exit 0 不等于语义正确。 |
| 任务进展 | successful test count、test pass signal count、modified expected files count、collect-after-test count | 模型是否在朝 verifier 相关目标推进 | 需要从 stdout/stderr 和文件 diff 中抽取弱监督信号。 |
| 效率与稳定性 | calls-to-first-test、calls-to-first-pass-like-output、calls-to-collect、total tool calls、budget exhaustion rate | 小模型是否在有限预算内有效行动 | 可解释 reward 为 0 时的 process-health 变化。 |

“工具调用正确次数”需要谨慎定义。没有任务级 oracle 时，不应把所有 exit 0
都称为正确调用。更稳妥的定义是：

- `valid_tool_calls`：格式可解析且 tool name 合法的调用次数。
- `successful_tool_calls`：工具实际执行成功、没有 timeout/exception 的次数。
- `progress_tool_calls`：产生可见任务进展的调用次数，例如修改目标文件、运行相关测试、
  读取必要输入、触发 collect。
- `productive_test_calls`：测试命令执行成功，且输出中出现 pass-like signal 或失败数减少。
- `premature_collect_count`：未见测试或明显进展就 collect 的次数。
- `redundant_tool_calls`：重复读同一文件、重复运行无新增信息命令、重复失败命令的次数。

这些指标的用途是做能力分解：

- 如果 C/OPSD 的 valid tool-call rate 更高但 reward 不变，说明 adapter 可能改善了
  harness 对齐，但没有改善解题。
- 如果 C/OPSD 的 calls-to-collect 更少且 productive test calls 更多，说明它可能学到了
  更好的执行流程，即使最终 reward 仍为 0。
- 如果 C/OPSD 的 budget exhaustion rate 更高，说明 OPSD 可能让模型更啰嗦或更容易陷入
  工具循环。
- 如果 process metrics 改善但 verifier reward 不改善，下一步应训练 task-solution
  signal；如果 process metrics 也不改善，下一步应先做工具调用/流程 curriculum。

## 本地 Serving

Pilot serving 使用 GPU 4 上的本地 vLLM：

```bash
CUDA_VISIBLE_DEVICES=4 \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_USE_FLASHINFER_SAMPLER=0 \
/root/evolab-vllm/bin/python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3.5-9B \
  --served-model-name qwen35-9b \
  --host 0.0.0.0 \
  --port 8000 \
  --trust-remote-code \
  --dtype bfloat16 \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.88 \
  --max-num-seqs 1 \
  --enable-lora \
  --lora-modules qwen35-9b-opsd=/tmp/openevo-opsd-tb21-gpt55/qwen35-9b-opsd-4step/adapter \
  --max-loras 1 \
  --max-lora-rank 8 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml \
  --api-key dummy-local-key \
  --disable-uvicorn-access-log
```

Served model ids：

- `qwen35-9b`
- `qwen35-9b-opsd`

## Harness 说明

direct solver wrapper：

`/tmp/openevo-opsd-tb21-gpt55/run_direct_pilot_ac.sh`

关键设置：

- `EVOLAB_TB_MODE=direct_solver`
- `EVOLAB_TB_DIRECT_SOLVER_COMPLETION_GUARD=successful_collect`
- `EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT=0`
- `EVOLAB_TB_MAX_OUTPUT_TOKENS=1024`
- `EVOLAB_TB_TOOL_RESULT_PROMPT_MAX_CHARS=1000`
- `EVOLAB_TB_MAX_SUBAGENT_LLM_CALLS=32`
- `EVOLAB_TB_FORCE_TOOL_CHOICE_REQUIRED=0`

本地临时 harness patch 位于仓库外的
`/root/EvoLabCore-terminal-bench-task-package`，用于把 static completion guard
传到 direct solver subagents，并让 subagent LLM-call budget 可通过环境变量配置。

## Direct A/C Pilot 结果

Groups：

- A: base `qwen35-9b`
- C: OPSD LoRA `qwen35-9b-opsd`

Run roots：

- `cancel-async-tasks`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-16k-budget32-20260703-092510`
- `build-pov-ray`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-16k-3task-20260703-093732`
- `dna-assembly`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-16k-dna-20260703-095455`

| Task | A reward | C reward | Process-health 解读 |
|---|---:|---:|---|
| `cancel-async-tasks` | 1.0 | 1.0 | 最干净的 A/C reward 比较；两组官方 verifier 都 pass。 |
| `build-pov-ray` | 0.0 | 0.0 | Process-limited。A 达到 32-call budget；C 遇到 context/tool-call failure。 |
| `dna-assembly` | 0.0 | 0.0 | Process-limited。两组都因为 LLM/tool JSON 或 collect error 失败。 |

三个 pilot tasks 汇总：

- A/base: 1/3.
- C/OPSD: 1/3.

这个汇总只是 harness smoke 结果，不是 OPSD 有效性 claim，因为 3 个任务中有 2 个
主要被 process-health failure 主导。

## 32k/64 Budget Follow-Up

第二轮把 vLLM context 提到 32k，在标注处把 subagent tool/LLM budgets 设为 64，
把 tool-result prompt text 降到 500 字符，并在 `/tmp/evolab_tb_patch`
引入临时 generic process-health shim：

- `EVOLAB_TB_FORCE_TOOL_CHOICE_REQUIRED=1`
- `EVOLAB_TB_FORCE_COLLECT_AFTER_TESTS=3`

该 shim 只会在重复可见 test calls 后强制 `tb_collect_result`。它是 process-health
control，不是最终 evaluation policy。

额外 run roots：

- `build-pov-ray`，32k，64 LLM-call budget，但 tool-call budget 仍为默认 32：
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-32k-budget64-build-20260703-112654`
- `dna-assembly`，32k，64 tool/LLM budgets：
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-32k-budget64both-dna-20260703-114309`
- `dna-assembly`，32k，64 tool/LLM budgets，force-collect-after-3-tests：
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-32k-forcecollect3-dna-20260703-115615`
- `write-compressor`，32k，64 tool/LLM budgets，force-collect-after-3-tests：
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-32k-forcecollect3-write-compressor-20260703-120244`
- `write-compressor` A rerun，同样 force-collect policy：
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-32k-forcecollect3-write-compressor-A-rerun-20260703-120749`

| Task | Config | A reward | C reward | Process-health 解读 |
|---|---|---:|---:|---|
| `build-pov-ray` | 32k, 64 LLM calls | 0.0 | 0.0 | A 干净完成 test/collect；C 完成 test/collect 但触发 `guard_failed`。无 reward gain。 |
| `dna-assembly` | 32k, 64 tool/LLM calls | 0.0 | 0.0 | JSON parse errors 消失，但两组仍在 collect 前达到 64-tool budget。 |
| `dna-assembly` | 32k, force collect after repeated tests | 0.0 | 0.0 | 两组都干净完成；reward 仍为 0，说明早期失败既有 process-health 问题，也有解题质量问题。 |
| `write-compressor` | 32k, force collect after repeated tests | invalid/0.0 | 0.0 | 第一次 A attempt 在 `tb_read_task` 立即失败；A rerun 干净完成且 reward 0。C 干净完成且 reward 0。 |

## Full-Success 3-Round 训练

2026-07-04，我们在转换后的 Terminal-Bench 2.1 dataset 中所有可用 GPT-success
records 上训练了一个更大的 OPSD adapter。这不是前面定义的 train/dev/test split
protocol；它故意使用完整成功 transcript 集合作为 train-set overfit/process smoke
test。

训练输入边界：

- 转换后 completed records：81。
- Reward 计数：63 success，18 failure。
- 当前 trainer 只使用 `reward=1.0` examples。
- 当前 trainer 的 safety skip 从这个 dataset 中移除 `git-leak-recovery` 和
  `crack-7z-hash`。`password-recovery` 和 `vulnerable-secret` 在 safety skip list
  中，但本数据里不存在。
- 实际训练 examples：61 个唯一任务。
- Rounds：3，相当于按顺序在同样 61 个 examples 上跑 183 steps。
- Base model：`Qwen/Qwen3.5-9B`。
- Student model 只看 task prompt；GPT-5.5 transcript 只作为 OPSD loss 的
  privileged teacher context。

训练命令：

```bash
CUDA_VISIBLE_DEVICES=4 \
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
/root/evolab-vllm/bin/python /tmp/openevo-opsd-tb21-gpt55/train_opsd_qwen35_9b.py \
  --records-jsonl /tmp/openevo-opsd-tb21-gpt55/artifacts/datasets/ds_b43616aeaef54bec/records.jsonl \
  --output-dir /tmp/openevo-opsd-tb21-gpt55/qwen35-9b-opsd-all-success-3rounds-20260704-075324 \
  --model Qwen/Qwen3.5-9B \
  --limit-examples 1000 \
  --max-steps 183 \
  --max-new-tokens 64 \
  --max-student-prompt-tokens 1024 \
  --max-teacher-prompt-tokens 3072 \
  --max-completion-tokens 64 \
  --learning-rate 1e-4 \
  --temperature 1.0 \
  --beta 0.5 \
  --top-k 128 \
  --seed 7 \
  --save-adapter
```

输出：

- Adapter：
  `/tmp/openevo-opsd-tb21-gpt55/qwen35-9b-opsd-all-success-3rounds-20260704-075324/adapter`
- Losses：
  `/tmp/openevo-opsd-tb21-gpt55/qwen35-9b-opsd-all-success-3rounds-20260704-075324/losses.jsonl`
- Token stats：
  `/tmp/openevo-opsd-tb21-gpt55/qwen35-9b-opsd-all-success-3rounds-20260704-075324/token_stats.json`
- Examples preview：
  `/tmp/openevo-opsd-tb21-gpt55/qwen35-9b-opsd-all-success-3rounds-20260704-075324/examples.preview.jsonl`

Loss 摘要：

- Rows：183。
- Mean loss：0.042500813802083336。
- Median loss：0.03466796875。
- Min loss：0.006317138671875。
- Max loss：0.16015625。
- Round means：first 61 = 0.04645475794057377, second 61 =
  0.0387693311347336, third 61 = 0.042278352330942626.
- Last 10 mean：0.05579833984375。

训练后 smoke eval 的 serving 使用 GPU 4 上 32k context 的 vLLM，adapter id 为
`qwen35-9b-opsd`：

```bash
CUDA_VISIBLE_DEVICES=4 \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_USE_FLASHINFER_SAMPLER=0 \
/root/evolab-vllm/bin/python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3.5-9B \
  --served-model-name qwen35-9b \
  --host 0.0.0.0 \
  --port 8000 \
  --trust-remote-code \
  --dtype bfloat16 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.95 \
  --max-num-seqs 1 \
  --enable-lora \
  --lora-modules qwen35-9b-opsd=/tmp/openevo-opsd-tb21-gpt55/qwen35-9b-opsd-all-success-3rounds-20260704-075324/adapter \
  --max-loras 1 \
  --max-lora-rank 8 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml \
  --api-key dummy-local-key \
  --disable-uvicorn-access-log
```

## Full-Success 3-Round Smoke Eval

这些 tasks 位于 61-example training set 中，因此本节是 train-set smoke/overfit
sanity check，不是 heldout generalization evidence。

通用 eval 设置：

- vLLM context：32k。
- Groups：A = `qwen35-9b`，C = `qwen35-9b-opsd`。
- `EVOLAB_TB_MAX_SUBAGENT_TOOL_CALLS=64`.
- `EVOLAB_TB_MAX_SUBAGENT_LLM_CALLS=64`.
- `EVOLAB_TB_TOOL_RESULT_PROMPT_MAX_CHARS=500`.
- `EVOLAB_TB_MAX_OUTPUT_TOKENS=1024`.
- `dna-assembly`、`write-compressor` 和 `build-pov-ray` 使用
  `EVOLAB_TB_FORCE_TOOL_CHOICE_REQUIRED=1` 和
  `EVOLAB_TB_FORCE_COLLECT_AFTER_TESTS=3`.

Run roots：

- `cancel-async-tasks`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess3r-32k-cancel-20260704-082616`
- `dna-assembly`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess3r-32k-dna-20260704-084014`
- `write-compressor`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess3r-32k-write-20260704-084449`
- `build-pov-ray`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess3r-32k-pov-20260704-085630`

| Task | A reward | C reward | A agent status | C agent status | Process-health 解读 |
|---|---:|---:|---|---|---|
| `cancel-async-tasks` | 0.0 | 0.0 | Failed: 64 LLM-call budget exceeded | Completed | C 改善 process health，但官方 verifier reward 仍为 0。 |
| `dna-assembly` | 0.0 | 0.0 | Completed | Completed | 两组都干净完成；无 reward delta。 |
| `write-compressor` | 0.0 | 0.0 | Failed: 64 LLM-call budget exceeded | Failed: 64 LLM-call budget exceeded | 无 process 或 reward improvement。 |
| `build-pov-ray` | 0.0 | 0.0 | Completed | Completed | 两组都干净完成；无 reward delta。 |

四个 train-set smoke tasks 汇总：

- A/base 官方 reward：0/4。
- C/full-success OPSD 官方 reward：0/4。
- Clean agent completion：A = 2/4，C = 3/4。

## Full-Success 10-Epoch 训练和 Smoke Eval

2026-07-04，我们用更多 epochs 重复了 full-success train-set smoke。当前实验
trainer 没有 resume/init-adapter 选项，所以这是从 `Qwen/Qwen3.5-9B` 开始的
fresh 10-epoch LoRA run，不是从 3-round adapter 继续训练。

训练输入边界：

- 实际训练 examples：与 3-round run 相同的 61 个唯一 `reward=1.0` tasks。
- Epochs：10，相当于按顺序在同样 61 个 examples 上跑 610 steps。
- Student model 只看 task prompt；GPT-5.5 transcript 仍然只作为 OPSD loss 的
  privileged teacher context。

输出 adapter：

`/tmp/openevo-opsd-tb21-gpt55/qwen35-9b-opsd-all-success-10epochs-20260704-121939/adapter`

Loss 摘要：

- Rows：610。
- Mean loss：0.0419496630058914。
- Median loss：0.034912109375。
- Min loss：0.0020904541015625。
- Max loss：0.267578125。
- Epoch means：
  1 = 0.04927863449346824,
  2 = 0.042435442815061473,
  3 = 0.043730188588627046,
  4 = 0.04046505787333504,
  5 = 0.043907290599385244,
  6 = 0.0390352342949539,
  7 = 0.036418226898693645,
  8 = 0.0404998279008709,
  9 = 0.037934850473872954,
  10 = 0.04579187612064549.
- Last 10 mean：0.0444854736328125。

10-epoch 的 mean loss 只比 3-round run 略低
（`0.0419496630058914` vs `0.042500813802083336`），并且最后一个 epoch
相对 epochs 7-9 变差。这不是“单纯加 epoch 能改善 OPSD objective”的强证据。

10-epoch smoke eval 复用了 32k vLLM serving 配置和 3-round smoke 相同的 direct
A/C harness policy。served LoRA id 仍是 `qwen35-9b-opsd`，但指向上面的
10-epoch adapter。

Run roots：

- `cancel-async-tasks`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess10e-32k-cancel-20260704-135537`
- `dna-assembly`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess10e-32k-dna-20260704-141732`
- `write-compressor`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess10e-32k-write-20260704-142153`
- `build-pov-ray`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess10e-32k-pov-20260704-142859`

| Task | A reward | C reward | A agent status | C agent status | Process-health 解读 |
|---|---:|---:|---|---|---|
| `cancel-async-tasks` | 0.0 | 0.0 | Completed | Completed | 两组都干净完成；无 reward delta。 |
| `dna-assembly` | 0.0 | 0.0 | Completed | Completed | 两组都干净完成；C 使用更少 tool steps，但 reward 仍为 0。 |
| `write-compressor` | 0.0 | 0.0 | Completed | Completed | 10-epoch run 相比 3-round smoke 改善了 process health，但 reward 仍为 0。 |
| `build-pov-ray` | 0.0 | error | Completed | Failed: 64 LLM-call budget exceeded | C 的 process health 退化。随后 verifier 卡在 `uvx pytest`，38 分钟后被终止，得到 `RewardFileNotFoundError`。 |

四个 train-set smoke tasks 汇总：

- A/base 官方 reward：0/4。
- C/10-epoch OPSD 官方 reward：0/3 scored trials，另有一个 verifier
  error/no-reward trial。
- Clean agent completion：A = 4/4，C = 3/4。

10-epoch adapter 没有在这个 smoke set 上产生官方 reward gain。它相对 3-round
smoke 改善了 `write-compressor` 的 process health，但也引入了
`build-pov-ray` budget failure。因此不应把“继续加 epoch”作为下一步主要 scaling
lever。

## Full-Success 10-Epoch 更多任务 Eval

随后我们在不属于四任务 smoke set 的剩余 split-test tasks 上评估 10-epoch adapter。
因为这个 adapter 是在所有可用 GPT-success records 上训练的，而不是只用 split train
set，所以对 GPT record 为 `reward=1.0` 的任务来说，这不是干净的 heldout
generalization test。它仍然是一个有用的 A/C execution comparison，因为 base model
和 OPSD model 使用相同 direct harness。

通用设置：

- vLLM context：32k。
- Groups：A = `qwen35-9b`，C = `qwen35-9b-opsd`。
- Adapter：
  `/tmp/openevo-opsd-tb21-gpt55/qwen35-9b-opsd-all-success-10epochs-20260704-121939/adapter`
- `EVOLAB_TB_MAX_SUBAGENT_TOOL_CALLS=64`.
- `EVOLAB_TB_MAX_SUBAGENT_LLM_CALLS=64`.
- `EVOLAB_TB_TOOL_RESULT_PROMPT_MAX_CHARS=500`.
- `EVOLAB_TB_MAX_OUTPUT_TOKENS=1024`.
- `EVOLAB_TB_FORCE_TOOL_CHOICE_REQUIRED=1`.
- `EVOLAB_TB_FORCE_COLLECT_AFTER_TESTS=3`.
- 每个 task/group 的 outer timeout：15 分钟。

Run roots：

- 主 remaining-task run：
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess10e-32k-more-test-remaining7-20260704-153900`
- `large-scale-text-editing` 单独尝试：
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess10e-32k-more-test-20260704-152354`

单独的 `large-scale-text-editing` attempt 不进入官方 reward aggregate，因为两组都
干净到达 `tb_collect_result`，但 verifier 卡在 Debian apt index download。A/base
约 9 分钟后被终止；随后 C/OPSD 复现同样 verifier stall 后也被终止。

| Task | A reward | C reward | A agent status | C agent status | Process-health 解读 |
|---|---:|---:|---|---|---|
| `mailman` | 0.0 | 0.0 | Completed, 61 tools | Failed: 64-call budget | C 的 process health 退化；无 reward delta。 |
| `modernize-scientific-stack` | 0.0 | 0.0 | Completed, 32 tools | Completed, 45 tools | 两组都 completed；C 使用更多 tools，且无 reward gain。 |
| `overfull-hbox` | 0.0 | 0.0 | Failed: 64-call budget | Failed: 64-call budget | 无 process 或 reward improvement。 |
| `prove-plus-comm` | 0.0 | 0.0 | Completed, 9 tools | Completed, 6 tools | C 改善 process health，但官方 reward 仍为 0。 |
| `raman-fitting` | 0.0 | 0.0 | Failed: subagent error | Failed: subagent error | C 更早失败；无 reward gain。 |
| `sanitize-git-repo` | 0.0 | 0.0 | Failed: 64-call budget | Completed, 14 tools | C 改善 process health，但官方 reward 仍为 0。 |
| `winning-avg-corewars` | 0.0 | 0.0 | Completed, 44 tools | Completed, 60 tools | 两组都 completed；C 使用更多 tools，且无 reward gain。 |

七个额外 scored tasks 汇总：

- A/base 官方 reward：0/7。
- C/10-epoch OPSD 官方 reward：0/7。
- Clean agent completion：A = 4/7，C = 4/7。
- Process-health transitions：C 改善 `prove-plus-comm` 和 `sanitize-git-repo`，
  退化 `mailman`，其余任务中性或更差。

合并四个 10-epoch smoke tasks 和这些额外 scored tasks：

- A/base 官方 reward：0/11 scored tasks。
- C/10-epoch OPSD 官方 reward：0/10 scored tasks，加上前面的 `build-pov-ray`
  no-reward verifier error 和未计分的 `large-scale-text-editing` verifier stall。
- 10-epoch OPSD adapter 没有观察到 fail-to-pass transition。

## 当前结论和解耦假设

direct execution 路径已经能跑通官方 Terminal-Bench verifier，用来比较 base
和 OPSD Qwen3.5-9B 是可行的。但最近实验没有显示正向 OPSD reward delta。
把 context 提到 32k、提高 subagent tool/LLM budget、加保守 collect policy
确实改善了一部分 process health；不过 3-round 和 10-epoch adapter 都没有带来
fail-to-pass task transition。

已经观察到的执行信号：

- 初始 3-task A/C pilot：A/base 1/3，C/4-step OPSD 1/3。
- 32k/64 follow-up：`build-pov-ray`、`dna-assembly`、`write-compressor`
  都没有 reward gain。
- Full-success 3-round train-set smoke：A/base 0/4，C/OPSD 0/4。
- Full-success 10-epoch train-set smoke：A/base 0/4，C/OPSD 0/3 scored
  trials，另有 `build-pov-ray` 的 no-reward verifier error。
- Full-success 10-epoch additional-task eval：A/base 0/7，C/OPSD 0/7。
- 当前合并 10-epoch scored eval：没有观察到 fail-to-pass transition。

关键解读不是“OPSD 被证伪了”。当前 protocol 至少耦合了四个因素：

- OPSD objective 是否真的教会了更好的 terminal task execution。
- 同一个 adapter 是否也教会了更好的自进化行为，例如挖掘 memory 或写
  agent-system instructions。
- direct solver 的 process health 可以独立于官方 reward 改变。
- 在所有 GPT-success records 上训练会产生 train-task exposure，削弱干净 heldout
  claim。

因为项目目标同时包含“更好执行”和“更好自进化”，下一轮实验应该先解耦这两个能力，
再考虑继续扩大训练。单纯增加 epoch 目前是弱 lever：10-epoch run 相比 3-round run
只略微降低 mean OPSD loss，最后一个 epoch 还比 epochs 7-9 更差，并且没有提升官方
reward。

## 下一轮 Curriculum 和 Ablation 计划

下一轮目标是把当前混合信号拆成可解释的独立 claim。分析单位用 task，harness policy
必须在各 group 间保持对称。

### Stage 0：评分卫生和过程指标

- 固定 direct solver policy：32k context、明确 tool/LLM budget、固定 collect
  policy、固定 outer timeout。
- 把官方 verifier reward 和 harness failure、verifier stall、LLM/tool JSON
  failure、budget exhaustion 分开记录。
- 增加过程指标：valid tool-call rate、successful tool-call count、
  productive test call count、calls-to-first-test、calls-to-collect、
  premature collect count、redundant tool-call count。
- 对每个 task/group 产出一行 machine-readable summary，至少包含：
  `reward`、`agent_status`、`failure_reason`、`verifier_error`、
  `tool_calls_total`、`tool_calls_valid`、`tool_calls_successful`、
  `productive_test_calls`、`collect_called`、`calls_to_collect`。
- 除非 A 和 C 都达到可比 verifier 状态，否则 process-limited pair 不进入 primary
  reward-effect claim；但它们保留在 process-health table 中。
- 使用已打包训练数据
  `/home/liaoc/OpenEvo/opsd_tb21_gpt55_training_data_20260705.zip`
  作为可复现数据输入。

### Stage 1：只评估执行能力

目标：回答 OPSD 是否在不使用任何 self-evolution artifact 的情况下改善
Terminal-Bench 执行。

必要 group：

| Group | Execution model | Evolution artifact | 目的 |
|---|---|---|---|
| A | Base Qwen3.5-9B | none | 本地执行 baseline。 |
| B | Plain SFT LoRA | none | 区分普通 GPT transcript imitation 和 OPSD。 |
| C | OPSD LoRA | none | 衡量 OPSD 的直接执行效果。 |

必要 ablation：

- 只用 split-train records 训练，不再用所有 GPT-success records。
- OPSD 必须和 plain SFT baseline 对比；两者使用相同 prompts、completions、
  LoRA rank、steps 和 optimizer 设置。
- 加 shuffled-privileged-context OPSD run。如果它接近真实 OPSD，说明 privileged
  transcript 没有被 task-specific 地利用。
- 加 reward-only privileged-context run。如果它接近 transcript OPSD，说明 transcript
  内容没有提供额外有效信号。
- 加 epoch sweep checkpoint，例如 1、3、5、10 epochs；先在同一批 dev tasks 上评估，
  再碰 test tasks。
- 对每个 checkpoint 同时报告 verifier reward 和过程指标。小模型可能先体现为
  tool-call validity、测试调用时机或 budget efficiency 的改善，而不是直接 reward
  改善。

成功标准：

- OPSD 在 heldout dev/test execution 上必须超过 base 和 SFT，主指标是 paired
  per-task reward delta 以及 fail-to-pass vs pass-to-fail count。
- 如果 reward 未提升但过程指标显著改善，只能 claim “执行流程或工具使用改善”，不能
  claim “任务解决能力改善”。

### Stage 2：只评估自进化/优化能力

目标：固定执行模型，只看 OPSD 是否让模型成为更好的 optimizer/reflection worker。

必要 group：

| Group | Execution model | Evolution model | Artifact type | 目的 |
|---|---|---|---|---|
| D | Base Qwen3.5-9B | Base Qwen3.5-9B | `text_memory` first | 自进化 baseline。 |
| E | Base Qwen3.5-9B | OPSD LoRA | `text_memory` first | OPSD optimizer effect。 |
| E-shuffle | Base Qwen3.5-9B | Shuffled OPSD control | `text_memory` first | leakage/control baseline。 |

评估规则：

- 只从 train trajectories 生成 artifacts。
- 把 artifacts 注入 heldout dev/test execution；D 和 E 的执行模型都固定为 base。
- artifact 评分看 downstream reward delta、regression rate、artifact audit
  failures、artifact size，以及注入后的过程指标变化；不要只靠主观文本质量。
- 先做 `text_memory`，因为 runtime injection 风险低于 `agent_system` 或
  `skill_bundle`。

成功标准：

- 执行模型固定为 base Qwen3.5-9B 时，OPSD-generated artifacts 相比
  base-generated artifacts 必须提升 heldout reward 或降低 regression。
- 如果 reward 不动但过程指标改善，只能 claim “artifact 改善了执行流程”，还不能 claim
  “artifact 改善了最终任务成功率”。

### Stage 3：耦合闭环

目标：衡量组合系统是否超过任一单独组件。

必要 group：

| Group | Execution model | Evolution model | 目的 |
|---|---|---|---|
| C | OPSD LoRA | none | 直接执行效果。 |
| E | Base Qwen3.5-9B | OPSD LoRA | 自进化效果。 |
| F | OPSD LoRA | OPSD LoRA | 执行和自进化耦合效果。 |

主要 contrasts：

- Direct effect：C vs A，C vs B。
- Artifact effect：E vs D。
- Coupled effect：F vs C，F vs E。

成功标准：

- F 应该同时超过 C 和 E。如果 F 只接近 C，说明闭环没有贡献有用 self-evolution；
  如果 F 只接近 E，说明 execution adapter 没有贡献有用执行能力。
- 闭环 claim 必须同时报告 verifier reward 和 process metrics，否则无法区分“真的解决
  任务”和“只是更早 collect / 更少失败 / 更会用工具”。

### 下一轮运行前的开放决策

- 下一次 OPSD 是否只用 split-train 训练，还是做 curriculum：先用 train-success
  records，再加入 train-failure reflection examples。
- curriculum 是否应该先训练 process-health skills，再训练 task-solving transcripts。
  目前 reward flat，但 process behavior 会变。
- force-collect-after-repeated-tests 是最终 harness policy，还是只作为诊断 control。
- `agent_system` 和 `skill_bundle` 是否应等到 `text_memory` 有可测量
  self-evolution signal 后再做。

## 生成的 Summary 文件

- `cancel-async-tasks`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-16k-budget32-20260703-092510/summary.json`
- `build-pov-ray`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-16k-3task-20260703-093732/summary.json`
- `dna-assembly`:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-16k-dna-20260703-095455/summary.json`
- `build-pov-ray`，32k follow-up:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-32k-budget64-build-20260703-112654/summary.json`
- `dna-assembly`，32k force-collect:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-32k-forcecollect3-dna-20260703-115615/summary.json`
- `write-compressor`，32k force-collect:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-32k-forcecollect3-write-compressor-20260703-120244/summary.json`
- `write-compressor` A rerun:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-pilot-natural-32k-forcecollect3-write-compressor-A-rerun-20260703-120749/summary.json`
- `cancel-async-tasks`，full-success 3-round adapter:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess3r-32k-cancel-20260704-082616/summary.json`
- `dna-assembly`，full-success 3-round adapter:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess3r-32k-dna-20260704-084014/summary.json`
- `write-compressor`，full-success 3-round adapter:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess3r-32k-write-20260704-084449/summary.json`
- `build-pov-ray`，full-success 3-round adapter:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess3r-32k-pov-20260704-085630/summary.json`
- `cancel-async-tasks`，full-success 10-epoch adapter:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess10e-32k-cancel-20260704-135537/summary.json`
- `dna-assembly`，full-success 10-epoch adapter:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess10e-32k-dna-20260704-141732/summary.json`
- `write-compressor`，full-success 10-epoch adapter:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess10e-32k-write-20260704-142153/summary.json`
- `build-pov-ray`，full-success 10-epoch adapter:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess10e-32k-pov-20260704-142859/summary.json`
- 10-epoch additional split-test A/C run:
  `/tmp/openevo-opsd-tb21-gpt55/eval-runs/direct-fullsuccess10e-32k-more-test-remaining7-20260704-153900/combined_ac_summary.json`
