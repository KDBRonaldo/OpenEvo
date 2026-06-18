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

运行内置 reference worker：

```sh
uv run polar-evolution worker --base-url http://127.0.0.1:8200 --once
```

## Terminal Bench 离线 transcript bridge

Terminal Bench 可以继续由 Harbor/EvoLab 和官方 verifier 执行。若只想让 Polar
负责后续 skill、memory 或 agent-system evolution，可先把 Harbor/EvoLab 的 trial
或 job 目录转换成 Polar event JSONL：

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
