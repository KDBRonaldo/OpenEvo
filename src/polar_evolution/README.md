# Polar Evolution Backend（演化后端）

Polar Evolution Backend 是一个面向 skill、memory、agent system 文本和 parametric
memory evolution 的异步控制面。它接收 Polar session 和 task events，从 events
构建 datasets，把 jobs 租约给外部 workers，注册 workers 产出的 artifacts，并为
后续 Polar sessions 解析 runtime context。

架构文档和图：

- [Evolution Backend](../../docs/architecture/evolution-backend.md)
- [Evolution Runtime Context](../../docs/architecture/evolution-runtime-context.md)
- [Evolution API 与新算法接入](../../docs/architecture/evolution-api-and-method-integration.md)
- [Reference Evolution Worker](../../docs/architecture/reference-evolution-worker.md)

本地启动 backend：

```sh
uv run polar-evolution serve --host 127.0.0.1 --port 8200
```

默认情况下，backend 状态保存在 `.polar_evolution/` 下。

核心 APIs：

- `/v1/events`
- `/v1/datasets`
- `/v1/jobs`
- `/v1/jobs/claim`
- `/v1/jobs/{job_id}/heartbeat`
- `/v1/jobs/{job_id}/complete`
- `/v1/jobs/{job_id}/fail`
- `/v1/contexts/resolve`

这个 backend 不负责训练 LoRA adapters，也不负责 serving inference。
Parametric memory artifacts 会被注册到 backend，并在 context resolve 时以
adapter merge specs 的形式返回给 trainer 和 inference infrastructure。

## OPSD privileged distillation helpers

`polar_evolution.opsd` 提供官方 OPSD 风格的轻量 helper，用于外部 trainer 或 vLLM
runner 组装 privileged-context distillation 数据流。它不注册 evolution artifact，也不
改变 context resolver 行为。

核心约定：

- student prompt 只包含测试时可见的 problem / schema / state。
- teacher prompt 包含同一个 problem，再额外包含 delimited privileged information。
- completion 必须来自 student on-policy generation。
- teacher 和 student 都 score 同一段 student completion。
- loss mask 只覆盖 completion tokens；prompt 和 privileged block 不参与训练。

使用 vLLM 做 full-logit OPSD 时，外部 runner 应启动允许 all-logits 的 vLLM server
并用同一 tokenizer 构造 student/teacher token sequences。若 teacher/student tokenizer
或 vocab 不一致，只能做 target-token 或 sequence-level distillation，不能做 full-vocab
KL/JSD。

`polar_evolution.opsd_vllm.VllmOpsdClient` 是一个最小 vLLM/OpenAI-compatible
runner。它会：

1. 用 student model 从 student prompt 生成 on-policy completion。
2. 用 teacher model tokenize teacher privileged prompt。
3. 把同一段 completion token ids 拼到 student/teacher prompt ids 后。
4. 对两段 pre-tokenized input ids 发 `/v1/completions` scoring 请求：
   `max_tokens=0`、`prompt_logprobs=-1`、`return_token_ids=true`、
   `add_special_tokens=false`。
5. 从 vLLM `prompt_logprobs` 中切出 completion token positions，计算 JSD/KL
   smoke loss，或把 logits 交给外部 torch trainer。

vLLM server 需要以允许 full prompt logits 的方式启动，例如设置
`--max-logprobs -1`，并在需要 raw logits 而不是 logprobs 时设置对应的
`--logprobs-mode`。

运行内置 reference worker：

```sh
uv run polar-evolution worker --base-url http://127.0.0.1:8200 --once
```

## Terminal Bench 离线 transcript bridge

Terminal Bench 可以继续由 Harbor/EvoLab 和官方 verifier 执行。若只想让 Polar
负责后续 skill、memory 或 agent-system evolution，可先把 Harbor/EvoLab 的 trial
或 job 目录转换成 Polar event JSONL：

### Per-task Terminal Bench evolution

Use this mode when each Terminal Bench task should evolve independently. The
live Harbor runner supports one evolved artifact type per invocation:
`agent_system` or `text_memory`. `skill_bundle` remains represented in the
runner interface for future support. `parametric_memory` is rejected on this
Codex subscription path because adapters can only be selected by proxy/local
inference serving.

```bash
uv run polar-evolution terminal-bench-per-task-evolution \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id fix-git \
  --task-id filter-js-from-html \
  --baseline-root /tmp/tb21-compare-codex-vs-wrapper-20260622-082940/jobs/tb21-wrapper-codex-gpt55-subscription-10 \
  --run-root /tmp/tb21-per-task-evolution \
  --model gpt-5.5 \
  --reflector-model gpt-5.5 \
  --rounds 1 \
  --env-json '{"HTTP_PROXY":"http://172.17.0.8:7890","HTTPS_PROXY":"http://172.17.0.8:7890","ALL_PROXY":"http://172.17.0.8:7890","NO_PROXY":"localhost,127.0.0.1,::1"}' \
  --verifier-env UV_NO_INDEX=1 \
  --verifier-env UV_FIND_LINKS=http://172.17.0.8:8765/wheels \
  --verifier-env UV_DOWNLOAD_URL=http://172.17.0.8:8765 \
  --output /tmp/tb21-per-task-evolution/summary.json
```

To run the memory-only ablation for Terminal Bench, keep skill, agent-system,
and parametric-memory evolution disabled by selecting only `text_memory`.
The default memory method is `text_memory_expel_reflector`:

```bash
uv run polar-evolution terminal-bench-per-task-evolution \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id fix-git \
  --baseline-root /tmp/tb21-baseline/jobs \
  --run-root /tmp/tb21-text-memory \
  --model gpt-5.5 \
  --reflector-model gpt-5.5 \
  --artifact-type text_memory \
  --memory-method text_memory_expel_reflector \
  --n-attempts 5 \
  --rounds 1 \
  --output /tmp/tb21-text-memory/summary.json
```

The summary includes pass@1/pass@5 and task-level reward transitions under
`memory_benchmark`, with `enabled_artifacts=["text_memory"]` and the other
evolution artifact types listed as disabled. `--n-attempts 5` runs five Harbor
trials with the selected memory artifact and records each attempt. The runner
passes the selected memory artifact as `memory_path` to Harbor; the installed
Harbor/EvoLab task package must support that argument for live injection. Text
memory runs check this before launching Harbor and fail fast when the installed
agent package cannot consume `memory_path`. Baseline pass@1 is computed from the
selected baseline trials; baseline pass@5 is unavailable for task-local runs
unless the experiment provides a matching multi-attempt baseline source.
Text-memory dataset construction includes both `COMPLETED` and `ERROR` Terminal
Bench events by default so failed transcripts can become `Avoid` and `Validate`
memory. Agent-system dataset construction remains completed-only by default.

To run closed-loop GEPA-style agent-system evolution for each task, select the
GEPA reflector and set the per-round generation count:

```bash
uv run polar-evolution terminal-bench-per-task-evolution \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id filter-js-from-html \
  --baseline-root /tmp/tb21-baseline/jobs \
  --run-root /tmp/tb21-gepa-loop \
  --model gpt-5.5 \
  --reflector-model gpt-5.5 \
  --agent-system-method agent_system_gepa_reflector \
  --gepa-candidate-count 2 \
  --gepa-generations 3 \
  --rounds 1 \
  --output /tmp/tb21-gepa-loop/summary.json
```

Within one evolution round, generation 1 reflects on the baseline trial.
After its candidates run through Harbor and the official verifier, generation
2 ingests all generation-1 candidate trials as feedback while keeping the
baseline dataset artifact in history. Later generations repeat that loop and
the runner selects the best-scoring candidate trial across the whole round.

### Group-level Terminal Bench evolution

Use group mode when a configurable set of Terminal Bench tasks should evolve
one shared `agent_system` artifact. This is different from per-task mode:
multiple `--task-id` values in per-task mode still create independent
per-task loops, while group mode creates one group loop and evaluates every
candidate artifact across every task in the group.

```bash
uv run polar-evolution terminal-bench-group-evolution \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --group-id tb21-failed-hard \
  --task-id train-fasttext \
  --task-id vulnerable-secret \
  --task-id sam-cell-seg \
  --baseline-root /tmp/tb21-baseline/jobs \
  --run-root /tmp/tb21-group-evolution \
  --model gpt-5.5 \
  --reflector-model gpt-5.5 \
  --agent-system-method agent_system_gepa_reflector \
  --gepa-candidate-count 2 \
  --gepa-generations 2 \
  --objective macro_mean_reward \
  --rounds 1 \
  --output /tmp/tb21-group-evolution/summary.json
```

The first group-level objective is `macro_mean_reward`. For each generation,
the runner creates one agent-system job from all current group trial inputs.
Each candidate `AGENTS.md` is then injected into every task in the group, and
the candidate score is the unweighted mean of those task rewards. The selected
candidate's per-task trial outputs become the next round's inputs. The summary
contains `groups[].rounds[].candidate_trials[]` with per-task trial paths,
per-task rewards, aggregate score, and the selected artifact.

```sh
uv run polar-evolution terminal-bench-events \
  --input /tmp/evolab-tb21-run/<job-or-trial-dir> \
  --output /tmp/tb21-events.jsonl
```

也可以直接写入本地 EvolutionStore 并创建 dataset artifact：

```sh
uv run polar-evolution terminal-bench-dataset \
  --input /tmp/evolab-tb21-run/<job-or-trial-dir> \
  --db /tmp/polar-tb21/evolution.db \
  --artifact-root /tmp/polar-tb21/artifacts \
  --name tb21_round0 \
  --purpose agent_system_reflection \
  --policy-version tb21-round0 \
  --output /tmp/polar-tb21/dataset.json
```

如果目标是 agent-system-only evolution，可以直接创建 audited reflector job：

```sh
uv run polar-evolution terminal-bench-agent-system-job \
  --input /tmp/evolab-tb21-run/<job-or-trial-dir> \
  --db /tmp/polar-tb21/evolution.db \
  --artifact-root /tmp/polar-tb21/artifacts \
  --dataset-name tb21_round0 \
  --policy-version tb21-round0 \
  --reflector-provider codex_cli \
  --reflector-model gpt-5.4 \
  --codex-home /path/to/codex-home \
  --output /tmp/polar-tb21/agent-system-job.json
```

该命令会 ingest Terminal Bench events、创建 dataset artifact，并创建一个
`agent_system_reflector` job。为了避免本地 DB 中不同轮次的 completed events 被混入，
`--input` 模式必须显式传 `--policy-version`。若改用已有多轮 dataset artifact，可重复传
`--dataset-artifact-id`；当输入 artifact 超过一个时，默认 method 会切换成
`agent_system_history_reflector`：

```sh
uv run polar-evolution terminal-bench-agent-system-job \
  --db /tmp/polar-tb21/evolution.db \
  --artifact-root /tmp/polar-tb21/artifacts \
  --dataset-artifact-id art_round1 \
  --dataset-artifact-id art_round2 \
  --reflector-model gpt-5.4
```

如果目标是 text-memory-only evolution，可以创建对应的 memory reflector job：

```sh
uv run polar-evolution terminal-bench-text-memory-job \
  --input /tmp/evolab-tb21-run/<job-or-trial-dir> \
  --db /tmp/polar-tb21/evolution.db \
  --artifact-root /tmp/polar-tb21/artifacts \
  --dataset-name tb21_memory_round0 \
  --policy-version tb21-memory-round0 \
  --method text_memory_expel_reflector \
  --reflector-provider codex_cli \
  --reflector-model gpt-5.5 \
  --codex-home /path/to/codex-home \
  --output /tmp/polar-tb21/text-memory-job.json
```

该命令同样只消费 transcript dataset，不要求 token-level logprobs，并且默认纳入
`COMPLETED` 和 `ERROR` Terminal Bench events。这样失败轨迹也能贡献 textual memory。
`text_memory` artifact 可用于 subscription 和 proxy/local inference；`parametric_memory`
artifact 仍只适用于 proxy/local inference。

若要使用多候选、带 promotion gate 和 candidate archive 的 agent-system evolution
算法，可显式指定：

```sh
uv run polar-evolution terminal-bench-agent-system-job \
  --db /tmp/polar-tb21/evolution.db \
  --artifact-root /tmp/polar-tb21/artifacts \
  --dataset-artifact-id art_round1 \
  --dataset-artifact-id art_round2 \
  --dataset-artifact-id art_round3 \
  --method agent_system_pareto_reflector \
  --reflector-model gpt-5.4
```

Terminal Bench job config 默认设置 `compatibility.agent_harness=["terminal-bench-harbor"]`
和 `task_tags=["terminal-bench"]`。默认 audit 只会从结构化 protected metadata
（例如 `leakage_basis` / `forbidden_literals`、article title/id、source file/sheet/row、
sequence）自动派生 `agent_system_audit.forbidden_literals`。公开 task id、task
instruction、trial name 和 verifier failure feedback 会保留为可学习的任务上下文，不会被
自动加入 forbidden list；如果需要额外保护 held-out 字面量，可重复传
`--audit-forbidden-literal`。

该 bridge 读取非 oracle 产物：

- `result.json`
- `agent/instruction.txt`
- `agent/stdout.txt`
- `agent/stderr.txt`
- `agent/evolab_lab/terminal_bench_report.md`
- `verifier/reward.txt`
- `verifier/ctrf.json`
- `verifier/test-stdout.txt`

输出 event 使用 `event_type="polar.session_completed"`，trajectory metadata 显式设置
`capture_mode="transcript"` 和 `token_level_metrics_available=false`。它不会把
`config.agent.env`、API key、oracle solution 或 reference patch 写入 event。
