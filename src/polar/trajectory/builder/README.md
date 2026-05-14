# Trajectory Builders

Builders convert a `CompletionSession` into a `Trajectory`.

## Main Files

- `base.py`: builder contract.
- `per_request.py`: one trace per completion.
- `prefix_merging.py`: merge strict append-only prompt/response chains.
- `record_utils.py`: helpers for extracting messages, token ids, logprobs, and
  response metadata from completion records.

## `per_request`

`per_request` is the simplest strategy. Each completion becomes its own trace.
Use it when preserving every request independently is more important than
building longer multi-turn training examples.

## `prefix_merging`

`prefix_merging` joins consecutive completions when each prompt is exactly the
previous prompt plus response. It starts a separate trace when that prefix
relationship breaks, such as after context compaction.

## Loss Mask And Logprobs

Builders should set `loss_mask = 1` for sampled assistant response tokens that
can train the policy. Interstitial or copied tokens should use `loss_mask = 0`.
Training bridges expect trainable tokens to have matching logprob data.
