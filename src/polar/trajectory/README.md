# Trajectories

`polar.trajectory` defines the data shapes and the strategy registry used to turn
the model calls captured during a run into **trainable, reward-bearing traces**.
In the pipeline: the gateway captures a `CompletionSession` → a **builder** turns
it into a `Trajectory` → an **evaluator** scores it → the gateway merges the
reward onto the traces and sends the result back.

## Mental model

- Two plugin families — **builders** and **evaluators** — each chosen by a string
  name in the task (`builder.strategy`, `evaluator.strategy`).
- A `StrategyRegistry` maps names to classes and constructs one per request from
  `spec.config`. A name you didn't register also works if you pass a
  `"module:ClassName"` import path.
- A `Trajectory` is a terminal status plus a list of `Trace`s. Each `Trace`
  carries parallel token arrays (`response_ids` / `loss_mask` /
  `response_logprobs`) and the messages. Reward lands on each `Trace.reward`,
  attached by the gateway from the evaluator's result — builders don't set it.

## Main files

- `models.py`: the schemas — `CompletionRecord`, `CompletionSession`, `Trace`,
  `Trajectory`, plus `StrategySpec`, `EvaluatorSpec`, `EvalResult`.
- `registry.py`: the generic `StrategyRegistry` + the default builder/evaluator
  registries.
- `builder/`: trajectory construction strategies (see [builder](builder/README.md)).
- `evaluator/`: reward / validation strategies (see [evaluator](evaluator/README.md)).

## Data model

- `CompletionRecord`: one captured model call — `completion_id`, the original and
  served `request`s, and the `response`.
- `CompletionSession`: every record from one run; it auto-sorts records by
  timestamp so builders see them in order.
- `Trace`: `prompt_ids`, `response_ids`, `loss_mask`, `prompt_messages`,
  `response_messages`, `tools`, `finish_reason`, `response_logprobs`, `reward`,
  `metadata`. (`loss_mask`, when present, matches `response_ids` length and holds
  only 0/1.)
- `Trajectory`: `status` (one of `COMPLETED` / `TIMEOUT` / `ERROR`), `traces`
  (zero or more), `metadata`, `error`.

## Transcript-only trajectories

Codex subscription 模式可以不经过 Polar proxy 运行，因此不会产生
`CompletionRecord` 或 token-level metric。当该模式结束时没有捕获到 completion，
gateway 会 fallback 到 `agent_transcript` builder，并从
`logs/agent/step.xx.stdout.log` 重建一个不带 token metric 的 trace。

这个 trace 保留 `prompt_messages` / `response_messages`，但 `response_ids`、
`loss_mask` 和 `response_logprobs` 为空。metadata 会设置
`capture_mode="transcript"` 和 `token_level_metrics_available=false`。这类
trajectory 适合 skill、memory、agent-system evolution 或 transcript-level
evaluation；token-level RL 应只消费 proxy 捕获的 traces。

## Reward attachment

An evaluator returns an `EvalResult` with an `outcome_reward` and/or per-trace
`trace_rewards`. The gateway merges it: `trace_rewards` must have exactly one
entry per trace (otherwise the session is marked `ERROR`); a single
`outcome_reward` is broadcast to every trace.

## Extension points

Register new builders/evaluators in `registry.py`, or reference any
`BaseTrajectoryBuilder` / `BaseTrajectoryEvaluator` subclass directly by
`"module:ClassName"`. Keep strategy names stable — task files and Slime configs
refer to them by string. Registered out of the box: builders `agent_transcript`,
`per_request`, `prefix_merging`; evaluators `session_completed`,
`test_on_output`, `swebench_harness`.
