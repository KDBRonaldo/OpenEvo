# Trajectory Builders

Builders convert a captured `CompletionSession` into a `Trajectory` of trainable
`Trace`s — the first reconstruction step, before evaluation.

## Main files

- `base.py`: the builder contract (`async build(session) -> Trajectory`).
- `agent_transcript.py`: one tokenless trace from an agent stdout transcript.
- `per_request.py`: one trace per completion.
- `prefix_merging.py`: stitch an append-only agent chain into one token-level trace.
- `record_utils.py`: helpers to pull messages, token ids, logprobs, and metadata
  out of a completion record.

## `agent_transcript`

`agent_transcript` 是不经过 OpenEvo proxy 的 harness run 的 fallback builder。Gateway
只有在 `agent.settings.capture_mode="transcript"` 或等价 transcript capture mode
开启、且没有 proxy completions 时才会选择它。它读取
`agent_result.metadata.log_dir` 和 `last_step`，加载 `step.xx.stdout.log`，并生成一个
包含 transcript message 的 `Trace`。

它不会创建 token-level 训练数据。生成的 trace 中 `response_ids` 为空，
`loss_mask` 为空，`response_logprobs=None`。trajectory 和 trace metadata 会设置
`capture_mode="transcript"` 和 `token_level_metrics_available=false`，因此下游
RL 代码可以过滤它，而 skill/memory evolution 仍然可以使用 transcript。

## `per_request`

The simplest strategy: each completion becomes its own trace. Use it when you
want every request preserved independently rather than merged into longer
multi-turn examples.

## `prefix_merging`

A multi-turn agent resends the growing conversation on every step, so
consecutive requests share a common prefix. `prefix_merging` detects this and
merges the chain into a single trace with one prompt and the concatenated turns.

The join test is a **strict token prefix**: a new request joins the chain only
when its `prompt_ids` start with the previous completion's prompt (append-only).
A message-level key gates candidates first, and it deliberately ignores
tool-result and empty assistant messages so ordinary tool loops still merge. When
the prefix relationship breaks — e.g. after context compaction rewrites earlier
turns — a new trace is started (and a partially merged chain can be truncated at
the break).

## Loss mask and logprobs

Builders set `loss_mask = 1` for the sampled assistant tokens that should train
the policy and `loss_mask = 0` for interstitial/copied tokens. Sampled tokens
keep their real logprobs; `prefix_merging` fills interstitial positions with
`0.0` placeholders so the arrays stay aligned. The turn boundary is found via an
end-of-turn token (auto-detected, or set explicitly with the builder's
`end_of_turn_token_id` config). Training bridges expect trainable tokens to have
matching logprob data.
