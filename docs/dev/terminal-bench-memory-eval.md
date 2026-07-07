# Terminal Bench 2.1 Memory-Only Evaluation

This note records the control-variable setup for evaluating memory evolution on
Terminal Bench 2.1.

## Method Choices

The first textual-memory backend is `text_memory_expel_reflector`. It follows
the ExpeL/Reflexion family because those methods learn from agent experience by
writing reusable natural-language feedback instead of updating model weights:

- ExpeL: LLM Agents Are Experiential Learners, arXiv:2308.10144,
  https://arxiv.org/abs/2308.10144
- Reflexion: Language Agents with Verbal Reinforcement Learning,
  arXiv:2303.11366, https://arxiv.org/abs/2303.11366

The first parametric-memory backend is `parametric_memory_lora_sft`. It uses
successful trajectory traces as supervised examples for a local LoRA trainer,
then registers the produced adapter. LoRA is the representative adapter method
because it keeps the base model frozen and stores task adaptation in a compact
serving-time adapter:

- LoRA: Low-Rank Adaptation of Large Language Models, arXiv:2106.09685,
  https://arxiv.org/abs/2106.09685

## Textual Memory

Textual memory is a Markdown artifact injected through the agent instruction.
It works for both transcript-only subscription runs and proxy/local inference
runs. To isolate memory evolution, enable only `text_memory`:

```sh
uv run polar-evolution terminal-bench-per-task-evolution \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id <task-id> \
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

The output summary includes `memory_benchmark`:

- `enabled_artifacts`: `["text_memory"]`
- `disabled_artifacts`: `["skill_bundle", "agent_system", "parametric_memory"]`
- `pass_at_1`: fraction of tasks whose first candidate trial succeeds
- `pass_at_5`: fraction of tasks with any success in the first five memory-enabled attempts
- `task_transitions`: per-task baseline reward and best memory-evolved reward

Errored Harbor attempts that produce no transcript or verifier reward are kept
in the attempt list with `null` reward. They count as non-passing attempts, but
do not prevent the memory benchmark summary from recording other successful
attempts in the same `--n-attempts` run.

Text-memory mining includes `COMPLETED` and `ERROR` Terminal Bench events by
default. This is intentional: failed/error transcripts are often the primary
source for `Avoid` and `Validate` memory, while successful traces provide
positive `Do` examples.

Live injection depends on the Harbor/EvoLab Terminal Bench package accepting
`memory_path`. The runner checks `EvoLabHarborAgent.__init__` before launching a
text-memory live rollout and fails fast if the installed package only supports
`agent_system_path`.

For subset or smoke runs, baseline pass@1 is computed from the selected baseline
trials. Baseline pass@5 is left unavailable unless a matching multi-attempt
baseline source is supplied by the experiment harness; the summary does not
copy global 89-task constants into task-local runs.

## Live Evidence So Far

The following runs are real Terminal Bench 2.1 Codex subscription runs with
`gpt-5.5`, `text_memory_expel_reflector`, `--artifact-type text_memory`, and
`--n-attempts 5`. They are subset evidence, not a full 89-task benchmark.
The reference full-89 Codex subscription baseline at
`/tmp/tb21-full-codex-gpt55-subscription-cache-20260624-085451/jobs/tb21-full-codex-gpt55-subscription-cache`
has pass@1 = 64/89. The follow-up pass@5 baseline on those 25 pass@1-failed
tasks at `/tmp/tb21-pass5-20260628-070012` recovered 16/25 tasks, giving the
expected baseline pass@5 = 80/89. Across the 21 counted task-local runs below,
task-local memory-enabled pass@1 is 12/21, pass@5 is 17/21, and the transitions
are 17 `fail_to_pass` and 4
`fail_to_fail`.

- `gcode-to-text`: baseline reward 0.0; memory attempt rewards
  `[0.0, 0.0, 0.0, 0.0, 1.0]`; task-local evolved pass@1 = 0/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-current-gcode-20260630-160111`.
- `password-recovery`: baseline reward 0.0; memory attempt rewards
  `[1.0, 1.0, 1.0, 1.0, 1.0]`; task-local evolved pass@1 = 1/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-rich-password-20260630-171746`.
- `large-scale-text-editing`: baseline reward 0.0; memory attempt rewards
  `[1.0, 1.0, 1.0, 1.0, 1.0]`; task-local evolved pass@1 = 1/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-large-edit-20260630-182446`.
- `filter-js-from-html`: baseline reward 0.0; memory attempt rewards
  `[0.0, 0.0, 0.0, 0.0, 0.0]`; task-local evolved pass@1 = 0/1 and pass@5 =
  0/1; transition `fail_to_fail`; run root
  `/tmp/tb21-text-memory-filter-20260630-174035`.
- `chess-best-move`: baseline reward 0.0; memory attempt rewards
  `[1.0, 0.0, 1.0, 0.0, 1.0]`; task-local evolved pass@1 = 1/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-failed-batch-20260630-190312/runs/chess-best-move`.
- `dna-insert`: baseline reward 0.0; memory attempt rewards
  `[0.0, 0.0, 0.0, 0.0, 0.0]`; task-local evolved pass@1 = 0/1 and pass@5 =
  0/1; transition `fail_to_fail`; run root
  `/tmp/tb21-text-memory-failed-batch-20260630-190312/runs/dna-insert`.
- `overfull-hbox`: baseline reward 0.0; memory attempt rewards
  `[1.0, 1.0, 1.0, 1.0, 1.0]`; task-local evolved pass@1 = 1/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-failed-batch-20260630-190312/runs/overfull-hbox`.
- `configure-git-webserver`: baseline reward 0.0; memory attempt rewards
  `[1.0, 1.0, 1.0, 1.0, 1.0]`; task-local evolved pass@1 = 1/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-targeted-batch-20260630-203032/runs/configure-git-webserver`.
- `pypi-server`: baseline reward 0.0; memory attempt rewards
  `[1.0, 0.0, 0.0, 0.0, 0.0]`; task-local evolved pass@1 = 1/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-targeted-batch-20260630-203032/runs/pypi-server`.
- `pytorch-model-recovery`: baseline reward 0.0; memory attempt rewards
  `[0.0, 0.0, 0.0, 1.0, 0.0]`; task-local evolved pass@1 = 0/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-pytorch-recovery-20260630-212915/run`.
- `raman-fitting`: baseline reward 0.0; memory attempt rewards
  `[0.0, 0.0, 0.0, 0.0, 0.0]`; task-local evolved pass@1 = 0/1 and pass@5 =
  0/1; transition `fail_to_fail`; run root
  `/tmp/tb21-text-memory-raman-20260630-215638/run`.
- `qemu-alpine-ssh`: baseline reward 0.0; memory attempt rewards
  `[1.0, 0.0, 1.0, 0.0, 1.0]`; task-local evolved pass@1 = 1/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-qemu-20260630-224006/run`.
- `video-processing`: baseline reward 0.0; memory attempt rewards
  `[0.0, 0.0, 0.0, 0.0, 0.0]`; task-local evolved pass@1 = 0/1 and pass@5 =
  0/1; transition `fail_to_fail`; run root
  `/tmp/tb21-text-memory-video-20260630-233151/run`.
- `protein-assembly`: baseline reward 0.0; memory attempt rewards
  `[0.0, 1.0, 1.0, 0.0, 1.0]`; task-local evolved pass@1 = 0/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-protein-20260701-001337/run`.
- `regex-chess`: baseline reward 0.0; memory attempt rewards
  `[0.0, 1.0, null, null, null]`; task-local evolved pass@1 = 0/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-regex-chess-20260701-005405/run`.
- `pytorch-model-cli`: baseline reward null; memory attempt rewards
  `[1.0, 1.0, 1.0, 0.0, 1.0]`; task-local evolved pass@1 = 1/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-pytorch-cli-20260701-013050/run`.
- `query-optimize`: baseline reward 0.0; memory attempt rewards
  `[1.0, 1.0, 1.0, 1.0, 1.0]`; task-local evolved pass@1 = 1/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-query-optimize-20260701-021002/run`.
- `make-mips-interpreter`: baseline reward 0.0; memory attempt rewards
  `[0.0, 1.0, 0.0, 0.0, 1.0]`; task-local evolved pass@1 = 0/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-mips-interpreter-20260701-040524/run`.
- `vulnerable-secret`: baseline reward 0.0; memory attempt rewards
  `[1.0, 1.0, 1.0, 1.0, 1.0]`; task-local evolved pass@1 = 1/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-vulnerable-secret-20260701-051205/run`.
- `mteb-retrieve`: baseline reward null; memory attempt rewards
  `[1.0, 1.0, null, 1.0, null]`; task-local evolved pass@1 = 1/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-mteb-retrieve-20260701-052938/run`.
- `train-fasttext`: baseline reward 0.0; memory attempt rewards
  `[1.0, 0.0, 0.0, null, 1.0]`; task-local evolved pass@1 = 1/1 and pass@5 =
  1/1; transition `fail_to_pass`; run root
  `/tmp/tb21-text-memory-train-fasttext-20260701-073824/run`.

The `password-recovery` run used the current default text-memory dataset policy:
the baseline `ERROR` transcript was included, the memory artifact manifest
recorded `record_count=1`, `reflected_record_count=1`, `success_count=0`, and
`failure_count=1`, and live Harbor logs show the memory was prepended via
`Reusable task memory:`.

The batch rooted at `/tmp/tb21-text-memory-failed-batch-20260630-190312`
continued into `query-optimize`, but that first attempt was interrupted after
more than 14 minutes without agent output. That interrupted `query-optimize`
trial is not counted; the completed `query-optimize` run above is counted.

## Parametric Memory

Parametric memory is an adapter artifact selected by the local/proxy inference
serving backend. It is not applicable to Codex subscription Terminal Bench runs,
because subscription harnesses call the external model service directly and
cannot select OpenEvo-trained adapters.

Use proxy/local inference for parametric-memory evaluation. The reference
backend method is `parametric_memory_lora_sft`: it exports successful trajectory
traces to `training.jsonl`, invokes an external trainer with
`{training_dataset}` and `{adapter_dir}`, and registers a `parametric_memory`
artifact only after the trainer writes a valid LoRA adapter directory.
Long successful transcripts can be projected before SFT export with
`job.config.training_projection`. The current built-in projection is
`{"type": "response_tail", "response_tail_chars": N}`, which keeps the final
assistant action window while preserving the original prompt messages.
For Codex-style Terminal Bench JSONL transcripts, use
`{"type": "terminal_bench_final_actions", "max_events": N, "max_output_chars": M}`
to parse completed command/message events and train only on the last bounded
terminal actions. This keeps durable solution actions, checksums, and final
file writes without letting large intermediate tool outputs dominate SFT.
For local Qwen/vLLM tool-use experiments, prefer
`{"type": "terminal_bench_tool_call_policy", "max_commands": N}`. It exports
Qwen chat-template `assistant.tool_calls` records plus a top-level `tools` list
so SFT can train the same `<tool_call>` XML shape that vLLM's `qwen3_xml`
parser expects. The trainer must pass each record's `tools` value into
`tokenizer.apply_chat_template`; otherwise the SFT prompt will not match the
runtime tool prompt.
If this does not affect the live policy, use
`{"type": "terminal_bench_corrective_tool_call_policy", ...}`. This opt-in path
stores compact real `llm_calls.jsonl` prefixes in the dataset and exports a
supervised next-tool-call from those exact prefixes. It can train from failed or
zero-reward records, because the point is to correct a known bad local rollout
state rather than imitate only successful transcripts. The projection requires a
`target_tool_call` and can filter prefixes with `input_contains`; exported
records again include top-level `tools` and require trainer-side
`apply_chat_template(..., tools=record["tools"])` support. Real failed prefixes
can exceed 13k tokens, so use `max_input_tool_messages` or the CLI
`--training-corrective-max-input-tool-messages` to keep the system/user prompt
and only the most recent tool-result messages when the trainer cannot fit the
full prefix.
For stage-aware corrective SFT, set
`{"type": "terminal_bench_corrective_tool_call_policy", "stages": [...]}`.
Each stage has `name`, exactly one of `target_tool_call` or
`target_assistant_message`, optional `input_contains`, `max_examples` (default
64), `repeat` (default 1), optional `max_input_tool_messages`, and optional
`synthetic_tool_results`. Synthetic tool results are appended only to the
exported SFT prefix before the target assistant action; they are useful for
finish-boundary records such as "tests passed -> collect result -> stop" when a
real rollout never reached that later prefix. The exporter scans the saved
`llm_calls` separately for each stage, emits repeated weighted samples when
`repeat > 1`, and records `projection_stage`,
`projection_stage_index`, and `projection_repeat_index` in each JSONL line's
metadata. Use `target_assistant_message` to teach finish behavior after a
terminal tool such as `tb_collect_result`; it emits a normal assistant message
with no `tool_calls`. The CLI accepts repeated `--training-corrective-stage-json`
objects for this form.
For the `password-recovery` local Qwen smoke, the higher-level
`terminal_bench_password_recovery_shorttarget_recipe` projection expands to
the same staged corrective contract. It emits a `read_task` target from the
initial Harbor prompt, repeated `short_exec_after_read` targets after the task
read output, and an optional `correct_back_to_short_exec` target when
`correction_input_contains` is supplied. The recipe defaults to matching
`static-terminal-bench-harbor` for the read stage and `recovered_passwords.txt`
for the after-read stage; the target command still must be provided explicitly
and should derive `/app/recovered_passwords.txt` from task files rather than
embed a protected answer.
For Qwen chat-template SFT, the trainer must not derive the loss mask by
tokenizing the full conversation and a generation prefix independently and then
masking by prefix token count. BPE can merge the prompt-ending newline with the
response-starting newline, masking out the first generated token. Instead,
render the full text, verify it starts with the generation prefix, tokenize the
prefix and suffix separately with `add_special_tokens=False`, concatenate those
ids, and mask only the prefix ids. This keeps the first response token under
loss and is required for the adapter to affect free generation.
This is the path expected for OpenEvo deployments backed by a local inference
server: the proxy can select a request-level LoRA adapter, while subscription
mode cannot.

### Local Qwen/vLLM Evaluation Path

Parametric-memory evaluation uses Harbor `mode=evolab` with an
OpenAI-compatible local/proxy endpoint. It does not use Codex subscription.
Keep `text_memory`, `skill_bundle`, and `agent_system` evolution disabled for a
controlled parametric-memory comparison.

The local parametric-memory runner sets `EVOLAB_TB_MODE=direct_solver` for the
Harbor agent. This disables EvoLab's task-local memory, prompt overlay, skill
graph, dynamic replanning, and insight-memory paths inside the Terminal Bench
harness so the treatment variable is the serving-time adapter. The Harbor CLI
still uses `mode=evolab` because the agent talks to an OpenAI-compatible local
endpoint. The runner records the requested solver output cap and the effective
cap passed as `EVOLAB_TB_MAX_OUTPUT_TOKENS`. By default the requested cap is
`4096`, but it is clamped by `--context-reserve-tokens` default `1536` to avoid
vLLM 400 errors when long direct-solver prompts approach the Qwen3.6 16k-token
serving window during later tool-heavy turns.

Use repeated `--agent-env KEY=VALUE` entries to pass Terminal-Bench/EvoLab
package behavior knobs into the direct-solver agent process. OpenEvo only
accepts `EVOLAB_TB_*` keys here and rejects fields that it controls directly
such as `EVOLAB_TB_MODEL`, `EVOLAB_TB_MODE`, and token-budget settings. This is
the intended bridge for package-level guards such as
`EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT=1` +
`EVOLAB_TB_DIRECT_SOLVER_COMPLETION_GUARD=successful_collect` stop-after-success
mode. For solve-focused adapters that create the task artifact but do not
reliably call the finish tools, use package-level
`EVOLAB_TB_DIRECT_SOLVER_COMPLETION_GUARD=successful_auto_tested_exec`; that
mode runs fixed tests after successful `tb_exec` calls and only lets `tb_exec`
count as successful when those tests pass. OpenEvo records the redacted
`agent_env` block in dry-run and live summaries, but the guard semantics must be
implemented by the installed Terminal-Bench/EvoLab package.

Use `--auth-mode local` or `--auth-mode proxy` for local/proxy inference modes;
neither is subscription mode. The selected auth mode is recorded in the
summary. `--server-url` selects the OpenAI-compatible endpoint. With
`--manage-server`, the command starts and stops vLLM locally; without it, the
command targets an already-running endpoint. When `--manage-server` receives an
absolute vLLM executable path, the runner prepends that executable's directory
to the server `PATH`, so vLLM subprocesses can find venv-local helpers such as
`ninja` during FlashInfer JIT startup.

Repeated `--trainer-arg` values are preserved as trainer arguments, including
values that begin with `--`. For example, `--trainer-arg --train-file` passes
`--train-file` through to the trainer command.

Preflight the local model, vLLM install, and GPU state:

```sh
du -sh /root/.cache/huggingface/hub/models--Qwen--Qwen3.6-35B-A3B
/root/evolab-vllm/bin/vllm --version
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits
```

The first controlled subset is:

```text
train-fasttext
query-optimize
make-mips-interpreter
```

For task-local parametric-memory experiments, prefer building the SFT dataset
from a trajectory pool that contains both failed and successful attempts for the
same Terminal Bench task. This path does not ingest events into EvolutionStore;
it writes a standalone dataset manifest, `records.jsonl`, and
`WorkerClaimedJob` payload under `--output-root`. It extracts a successful
Codex command from `agent/codex.txt`, pairs it with a failed trajectory summary,
and exports a Qwen tool-call SFT record using the default `full_trace`
projection. Use `--run-worker` only when the trainer is ready to run locally.

```sh
uv run polar-evolution terminal-bench-task-local-parametric-memory-job \
  --trajectory-pool /home/jyw/openevo-skill-rl-experiments/pools/tb21-current-20260702/trajectory_pool.jsonl \
  --task-id train-fasttext \
  --output-root /tmp/tb21-task-local-parametric/train-fasttext \
  --dataset-name tb21-task-local-train-fasttext \
  --base-model Qwen/Qwen3.6-35B-A3B \
  --adapter-id tb-parametric-memory-train-fasttext \
  --trainer-command /root/evolab-vllm/bin/python \
  --trainer-arg /path/to/train_lora.py \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --command-contains /app/model.bin \
  --output /tmp/tb21-task-local-parametric/train-fasttext/job.json
```

The CLI normalizes repeated `--trainer-arg` values, so trainer flags such as
`--train-file` and `--output-dir` are passed through as literal trainer
arguments. The generated job still uses `parametric_memory_lora_sft`, so the
trainer must render each JSONL row with the row-level `tools` value when tool
schemas are present. `full_trace` now preserves assistant messages that contain
`tool_calls` even when `content` is empty, and carries trace-level `tools` into
`training.jsonl`. When several successful commands match `--command-contains`,
the builder prefers write-like commands such as `save_model`, `cp`, `mv`, or
file writes over later verification commands such as `Path.exists()` checks.

Smoke evidence on 2026-07-07:

- Dry-run job generation at
  `/tmp/tb21-task-local-parametric-smoke-20260707/train-fasttext` selected
  `train-fasttext`, wrote 11 SFT records, and chose the successful
  `model.save_model('/app/model.bin')` command rather than the later file-size
  verification command.
- Trainer smoke at
  `/tmp/tb21-task-local-parametric-train-smoke-20260707/train-fasttext-qwen35-9b`
  used `Qwen/Qwen3.5-9B`, `CUDA_VISIBLE_DEVICES=6`, one SFT record, one trainer
  step, and produced a `parametric_memory` artifact with
  `adapter_config.json`, `adapter_model.safetensors`, and
  `trainer_diagnostics.json`. The diagnostic loss was `1.2177761793136597`.
- Local eval smoke at
  `/tmp/tb21-task-local-parametric-eval-smoke-20260707/train-fasttext-qwen35-9b-live`
  used managed vLLM on GPU 6 with `Qwen/Qwen3.5-9B`, `n_attempts=1`, and the
  one-step adapter above. Baseline pass@1 was `0/1`; parametric-memory pass@1
  was `0/1`; delta was `0`. This verifies the baseline/treatment local eval
  plumbing and LoRA serving path, but it is not performance evidence for the
  method because the adapter was trained for only one step on one record.

Create a parametric-memory job from successful Terminal Bench trajectories and
run the local worker once:

```sh
uv run polar-evolution terminal-bench-parametric-memory-job \
  --input /tmp/tb21-text-memory-train-fasttext-20260701-073824/run/tasks/train-fasttext/r1/harbor_jobs/train-fasttext-r1/train-fasttext__attempt1 \
  --input /tmp/tb21-text-memory-query-optimize-20260701-021002/run/tasks/query-optimize/r1/harbor_jobs/query-optimize-r1/query-optimize__attempt1 \
  --input /tmp/tb21-text-memory-mips-interpreter-20260701-040524/run/tasks/make-mips-interpreter/r1/harbor_jobs/make-mips-interpreter-r1/make-mips-interpreter__attempt2 \
  --db /tmp/tb21-parametric-memory/evolution.db \
  --artifact-root /tmp/tb21-parametric-memory/artifacts \
  --dataset-name tb21-parametric-memory-subset \
  --policy-version tb21-qwen36-local-parametric-memory \
  --base-model Qwen/Qwen3.6-35B-A3B \
  --adapter-id tb-parametric-memory \
  --trainer-command python \
  --trainer-arg /path/to/train_lora.py \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --training-projection terminal_bench_tool_call_policy \
  --training-tool-call-max-commands 1 \
  --training-tool-call-command-contains recovered_passwords.txt \
  --training-tool-call-derive-password-recovery-command \
  --run-worker \
  --output /tmp/tb21-parametric-memory/job.json
```

To train a corrective adapter from failed local trajectory prefixes, point the
job at the failed Harbor trial, choose the corrective projection, and provide
one or more staged next-tool-call targets. Keep target commands bounded and
avoid embedding protected task answers in docs or reusable configs:

```sh
uv run polar-evolution terminal-bench-parametric-memory-job \
  --input /tmp/tb21-parametric-memory-password-toolpolicy-20260702-110343/local-eval-password-toolpolicy-2048/baseline/harbor_jobs/baseline-password-recovery/password-recovery__AzMbthq \
  --db /tmp/tb21-parametric-memory-corrective/evolution.db \
  --artifact-root /tmp/tb21-parametric-memory-corrective/artifacts \
  --dataset-name tb21-parametric-memory-password-corrective \
  --policy-version tb21-qwen36-local-password-corrective \
  --status COMPLETED \
  --base-model Qwen/Qwen3.6-35B-A3B \
  --adapter-id tb-parametric-memory-password-corrective \
  --trainer-command /root/evolab-vllm/bin/python \
  --trainer-arg /tmp/qwen36_lora_sft.py \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --training-projection terminal_bench_corrective_tool_call_policy \
  --training-corrective-stage-json '{"name":"read_task","input_contains":["static-terminal-bench-harbor"],"target_tool_call":{"name":"tb_read_task","arguments":{"task_id":"terminal-bench-task"}}}' \
  --training-corrective-stage-json '{"name":"short_exec_after_read","input_contains":["recovered_passwords.txt"],"max_examples":64,"repeat":6,"max_input_tool_messages":5,"target_tool_call":{"name":"tb_exec","arguments":{"task_id":"terminal-bench-task","command":"<bounded command that derives /app/recovered_passwords.txt from task files>"}}}' \
  --run-worker \
  --output /tmp/tb21-parametric-memory-corrective/job.json
```

The equivalent first-class recipe form is less error-prone for the
`password-recovery` short-target smoke:

```sh
uv run polar-evolution terminal-bench-parametric-memory-job \
  --input /tmp/tb21-parametric-memory-password-toolpolicy-20260702-110343/local-eval-password-toolpolicy-2048/baseline/harbor_jobs/baseline-password-recovery/password-recovery__AzMbthq \
  --db /tmp/tb21-parametric-memory-corrective/evolution.db \
  --artifact-root /tmp/tb21-parametric-memory-corrective/artifacts \
  --dataset-name tb21-parametric-memory-password-recipe \
  --policy-version tb21-qwen36-local-password-recipe \
  --status COMPLETED \
  --base-model Qwen/Qwen3.6-35B-A3B \
  --adapter-id tb-parametric-memory-password-recipe \
  --trainer-command /root/evolab-vllm/bin/python \
  --trainer-arg /tmp/qwen36_lora_sft.py \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --training-projection terminal_bench_password_recovery_shorttarget_recipe \
  --training-recipe-target-command '<bounded command that derives /app/recovered_passwords.txt from task files>' \
  --training-recipe-after-read-repeat 6 \
  --training-recipe-correction-input-contains 'Dummy entry' \
  --training-recipe-max-input-tool-messages 5 \
  --run-worker \
  --output /tmp/tb21-parametric-memory-corrective/job.json
```

The first successful real local parametric-memory smoke used this stage-aware
short-target setup on `password-recovery`. A command-only Harbor smoke confirmed
the bounded derivation strategy at
`/tmp/tb21-parametric-memory-password-shorttarget-smoke-20260702-180114`
(`reward=1.0`). Training then used 28 corrective records
(`read_task`, repeated `short_exec_after_read`, and correction-back examples)
with `Qwen/Qwen3.6-35B-A3B`, LoRA rank 8, and 84 SFT steps. The controlled
one-task local eval at
`/tmp/tb21-parametric-memory-password-shorttarget-eval-20260702-181502`
had baseline `0/1`, parametric memory `1/1`, and delta `+1.0` pass@1/pass@k.
The vLLM log confirmed the adapter was loaded. Treat this as a one-task
hand-built projection validation, not a full Terminal-Bench 2.1 result.

Two follow-up runs drove the same idea through the committed framework path,
using `terminal-bench-parametric-memory-job --run-worker` to ingest the failed
trial, export staged corrective JSONL, train the LoRA adapter, register a
`parametric_memory` artifact, rewrite the adapter for Qwen3.6 MoE vLLM serving,
and run `terminal-bench-local-parametric-memory-eval`:

- `/tmp/tb21-parametric-memory-staged-framework-20260702-184506`: 25 records
  (`read_task` once, four `short_exec_after_read` prefixes repeated six times),
  75 SFT steps, artifact `art_ff63382e8930464a`. Eval result was baseline
  `0/1`, parametric memory `0/1`, delta `0`. The adapter served successfully,
  but the first `tb_exec` drifted to unrelated UUID/discovery commands and did
  not write `/app/recovered_passwords.txt`.
- `/tmp/tb21-parametric-memory-staged-afterread-20260702-191729`: 25 records
  (`read_task` once, only the immediate after-read prefix repeated 24 times),
  75 SFT steps, artifact `art_1a53d92856904d0e`. Eval result was baseline
  `0/1`, parametric memory `0/1`, delta `0`. This narrower dataset overfit
  badly: the first `tb_read_task` call produced malformed repeated task-id
  arguments.

The current evidence is therefore: the framework can now run the staged
parametric-memory training and serving loop end-to-end, but the reliable
one-task performance gain still comes from the hand-built mixed short-target
record set. The first-class short-target recipe now captures that mixed
read/after-read/correction shape in config. A follow-up recipe-backed framework
run at `/tmp/tb21-parametric-memory-recipe-framework-20260702-195641` ingested
three local failed/error trajectories, exported 23 records (`read_task=3`,
`short_exec_after_read=18`, `correct_back_to_short_exec=2`), trained 84 LoRA
steps, and registered artifact `art_731ae1e1ca4d4e57`. The controlled
one-task local eval at
`/tmp/tb21-parametric-memory-recipe-framework-20260702-195641/local-eval-retry`
had baseline `0/1`, parametric memory `1/1`, and delta `+1.0` pass@1/pass@k.
The treatment trial recorded verifier reward `1.0` but also an
`AgentTimeoutError` after a later slow command, so this is positive smoke
evidence for the adapter effect, not a polished policy behavior.

Two later finish-behavior experiments added `target_assistant_message` stages
to train a normal assistant response after `tb_collect_result`. The exporter
worked as intended and the generated JSONL was leak-free, but the policy effect
was negative in the current recipe. `/tmp/tb21-parametric-memory-finish-real-20260702-222144`
trained 82 records, including 16 `finish_after_collect` and 24
`collect_result_after_report` samples; eval produced baseline `0/1`,
parametric memory `0/1`, and the treatment called `tb_collect_result` at step 0.
`/tmp/tb21-parametric-memory-solvefinish-real-20260702-230201` restored the
solve-heavy mix to 104 records (`read_task=24`, `short_exec_after_read=48`,
`run_tests_after_count=20`, `correct_back_to_short_exec=4`,
`finish_after_collect=8`) and included `ERROR` status trajectories so reward-1
timeout runs were not filtered out. It still evaluated at baseline `0/1`,
parametric memory `0/1`; the treatment did `tb_read_task`, then prematurely
called `tb_collect_result`, then stopped after one `tb_exec`. Treat
`target_assistant_message` as a framework mechanism for future stop/finish
methods, not as the current best `password-recovery` recipe.

A follow-up local smoke at
`/tmp/tb21-parametric-memory-recipe-framework-20260702-195641/local-eval-successguard`
used the recipe adapter `art_731ae1e1ca4d4e57` with the OpenEvo
`--agent-env` bridge and a locally patched Terminal-Bench/EvoLab package guard:
`EVOLAB_TB_REQUIRE_SUCCESSFUL_COLLECT=1` and
`EVOLAB_TB_DIRECT_SOLVER_COMPLETION_GUARD=successful_collect`. The summary
recorded baseline `0/1`, parametric memory `0/1`, and delta `0`. Baseline
failed after 15 tool calls without tests. The treatment loaded the adapter and
entered the direct solver, but did not reach `tb_run_tests` or
`tb_collect_result`; it generated slow `tb_exec` commands and ended with reward
`0.0` plus an `AgentTimeoutError`. This run shows the environment bridge and
package guard are not enough to recover performance when the adapter's command
policy drifts to slow enumeration before the validation/collect phase.

The next local eval made the local parametric path deterministic by passing
solver temperature `0.0` to the Terminal-Bench EvoLab package and starting
managed vLLM with `--generation-config vllm`, so Qwen's model
`generation_config.json` no longer overrides serving defaults with sampled
`top_k/top_p` behavior. Using the stronger fast-target v2 adapter
`art_476b4c85b1c44a2b`, the run at
`/tmp/tb21-parametric-memory-fasttarget-v2-deterministic-20260703-013446/local-eval-successguard`
recorded baseline `0/1`, parametric memory `1/1`, and delta `+1.0`
pass@1/pass@k. The treatment server metadata confirmed
`--generation-config vllm`, LoRA key rewrite
`qwen3_5_moe_vllm_language_model`, and 80 rewritten adapter keys. This is the
current best controlled local evidence that parametric memory can improve the
`password-recovery` task under a local Qwen3.6 MoE backend. It is still not a
polished policy: the treatment wrote a verifier-passing artifact but later
ended with `AgentTimeoutError` instead of cleanly calling
`tb_run_tests`/`tb_collect_result` and stopping. Future parametric-memory
methods should train that validation/finish boundary explicitly.

The first synthetic finish-boundary attempt added
`synthetic_tool_results` stages to train `tb_run_tests -> tb_collect_result ->
Done` from real post-`tb_exec` prefixes. The v3 run at
`/tmp/tb21-parametric-memory-finishboundary-v3-20260703-023546` exported 624
records (`read_task=16`, `fast_exec_after_read=168`,
`correct_drift_to_fast_exec=44`, `run_tests_after_recovered_file=176`,
`collect_after_synthetic_tests=132`, `finish_after_synthetic_collect=88`),
trained 260 LoRA steps, and registered artifact `art_e4b6e377d25c4d20`. The
deterministic success-guard eval recorded baseline `0/1`, parametric memory
`0/1`, and delta `0`; the treatment still produced `tb_read_task` followed by
repeated `tb_exec` calls, never `tb_run_tests` or `tb_collect_result`, and ended
with `AgentTimeoutError`. Treat this as a negative method result: synthetic
finish prefixes are available for future methods, but this heavy
finish-boundary mix weakened the fast-command behavior instead of producing a
clean validation/collect/stop policy.

A continued-LoRA v4 attempt tested whether starting from the best fast-target
v2 adapter could preserve the fast command while adding only a small finish
boundary update. The temporary resume trainer loaded all 80 tensors from v2
artifact `art_476b4c85b1c44a2b`, then trained 80 additional steps at lower
learning rate on 318 replay-heavy records at
`/tmp/tb21-parametric-memory-continued-v4-20260703-034807`
(`read_task=18`, `fast_exec_after_read=192`,
`correct_drift_to_fast_exec=44`, `run_tests_after_grep_context=32`,
`collect_after_synthetic_tests=16`, `finish_after_synthetic_collect=16`).
The deterministic success-guard eval again recorded baseline `0/1`,
parametric memory `0/1`, and delta `0`. The treatment did not keep the v2 fast
trajectory; it repeatedly called `tb_read_task` and never reached `tb_exec`,
`tb_run_tests`, or `tb_collect_result`. This negative result suggests that
small continued-LoRA updates can still destabilize the tool-call policy. Future
finish-boundary work should either use a much more explicit action-state
curriculum, separate adapters/routing for solve vs finish states, or a
harness-side deterministic validation/collect guard while keeping the v2
parametric adapter focused on the fast solve action.

A harness-side auto-tested exec guard was then tested with the v2 solve-focused
adapter rather than mixing finish behavior into the same LoRA. Because
`password-recovery` has no visible `/tests` or `/app` test entrypoint, the
useful controlled guard must provide a task-visible test command:
`EVOLAB_TB_TEST_COMMAND='test -s /app/recovered_passwords.txt'`. The best run
at
`/tmp/tb21-parametric-memory-v2-autotested-visible-20260703-050759/local-eval-auto-tested-exec`
recorded baseline `0/1`, parametric memory `1/1`, and delta `+1.0`
pass@1/pass@k with no Harbor exception stats in either condition. A follow-up
runtime-stop attempt at
`/tmp/tb21-parametric-memory-v2-autostop-20260703-054159/local-eval-auto-tested-exec`
kept the same baseline `0/1` and parametric memory `1/1` reward result, but the
treatment still ended with `AgentTimeoutError`; the auto-tested `tb_exec`
signals passed, while dynamic runtime stopping did not yet terminate the agent
cleanly. That run was later traced to the Harbor process importing the default
`/root/EvoLabCore-terminal-bench-task-package` EvoLab package instead of the
selected `--terminal-bench-package-root` worktree, so the static completion guard
opt-in fix was not active in the subprocess. After prepending the selected
package root to Harbor `PYTHONPATH` and using its compose files, the fixed
launcher run at
`/tmp/tb21-parametric-memory-v2-autostop-fixed-launcher-20260703-064752/local-eval-auto-tested-exec`
recorded baseline `0/1`, parametric memory `1/1`, and delta `+1.0`
pass@1/pass@k with no Harbor exceptions. The treatment used only two tool calls
(`tb_read_task`, then one successful auto-tested `tb_exec`) and the dynamic
runtime stopped cleanly in the same second as the successful `tb_exec`
(`73.2s` EvoLab round, `1m49s` Harbor job). This is the current best controlled
evidence that the v2 parametric memory adapter improves `password-recovery`
under the local Qwen3.6 MoE backend when paired with the harness-side
auto-tested exec guard.

Evaluate baseline local Qwen and adapter local Qwen against the same subset:

```sh
uv run polar-evolution terminal-bench-local-parametric-memory-eval \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id train-fasttext \
  --task-id query-optimize \
  --task-id make-mips-interpreter \
  --run-root /tmp/tb21-parametric-memory/local-eval \
  --model Qwen/Qwen3.6-35B-A3B \
  --adapter-path /tmp/tb21-parametric-memory/artifacts/workers/<job-id>/parametric_memory_lora_sft/adapter \
  --adapter-id tb-parametric-memory \
  --adapter-artifact-id <artifact-id> \
  --adapter-key-rewrite qwen3_5_moe_vllm_language_model \
  --gpu 1 \
  --gpu 2 \
  --gpu 3 \
  --gpu 4 \
  --server-port 8000 \
  --manage-server \
  --n-attempts 5 \
  --max-output-tokens 4096 \
  --context-window-tokens 16384 \
  --context-reserve-tokens 1536 \
  --tool-result-prompt-max-chars 2048 \
  --solver-temperature 0.0 \
  --vllm-generation-config vllm \
  --agent-env EVOLAB_TB_DIRECT_SOLVER_COMPLETION_GUARD=successful_auto_tested_exec \
  --agent-env 'EVOLAB_TB_TEST_COMMAND=test -s /app/recovered_passwords.txt' \
  --verifier-python-install-mirror http://172.17.0.8:8765/python-build-standalone/releases/download \
  --output /tmp/tb21-parametric-memory/local-eval/summary.json
```

The summary reports `baseline`, `parametric_memory`, `delta`, and the
`requested_max_output_tokens`, effective `max_output_tokens`,
`context_window_tokens`, `context_reserve_tokens`, and
`tool_result_prompt_max_chars` caps used for both conditions, plus
`solver_temperature`, `vllm_generation_config`, and redacted `agent_env`
package knobs when supplied. The default
requested output-token cap is `4096`, but the effective cap is clamped to the
default context reserve `1536`; increase `--context-reserve-tokens` only when
the serving context window and prompt growth leave enough room. For smoke tests
on slower local Qwen/vLLM servers, lower `--max-output-tokens` explicitly, for
example `--max-output-tokens 1024`. `--context-window-tokens` also drives
managed vLLM `--max-model-len`. `--tool-result-prompt-max-chars` sets
`EVOLAB_TB_TOOL_RESULT_PROMPT_MAX_CHARS` for the Harbor agent. It only affects
runtime behavior when the installed Terminal Bench/EvoLab package honors that
environment variable; otherwise the value is still recorded in the summary as a
preflight signal that the package needs the matching support. The same package
must honor `EVOLAB_TB_LLM_TEMPERATURE` for `--solver-temperature` to affect
OpenAI-compatible chat-completion requests. `--vllm-generation-config vllm` is
the managed-server default so model-local generation configs do not silently
turn a controlled adapter eval into sampled decoding. Many Terminal
Bench verifiers run `uvx -p ...`, which can download managed Python even when
wheel dependencies are local. When a local Python-build mirror is available,
pass `--verifier-python-install-mirror` with the uv-compatible
`.../python-build-standalone/releases/download` base; if the local mirror root
ends at `.../python-build-standalone`, the runner normalizes it to that download
base. The runner records the resulting verifier environment in the summary for
reproducibility. Treat controlled-subset results as subset evidence until the
same path is run over full Terminal Bench 2.1.

Use `--adapter-key-rewrite qwen3_5_moe_vllm_language_model` when the adapter is
a PEFT LoRA trained against Hugging Face `Qwen3_5MoeForConditionalGeneration`
or the Qwen3.6 MoE alias but served through vLLM's language-model-only
`Qwen3_5MoeForConditionalGeneration` wrapper. vLLM 0.21.0 maps LoRA keys under
`language_model.model.layers.*`, while the PEFT trainer writes
`base_model.model.model.layers.*`. The runner copies the adapter under
`run_root/prepared_adapters/<adapter-id>/<rewrite>/adapter`, rewrites only the
safetensors key prefixes, and records both the source and serving adapter paths
plus the rewritten key count in the summary. Treat `rewritten_key_count=0` as a
configuration error; without this rewrite, vLLM can expose the adapter model id
while the generated logits remain unchanged.
