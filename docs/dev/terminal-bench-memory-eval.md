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
full prefix. If the saved training prefix contains bridge-only appended
`Tool result payload` sections that are absent from live runtime `llm_calls`,
also set `strip_input_tool_result_payload=true` or pass
`--training-corrective-strip-input-tool-result-payload`. Use
`max_input_tool_content_chars` or
`--training-corrective-max-input-tool-content-chars` to cap each tool-result
input message after optional payload stripping. `input_contains` is evaluated
after these shaping steps, so filters should match the final exported prefix.
For stage-aware corrective SFT, set
`{"type": "terminal_bench_corrective_tool_call_policy", "stages": [...]}`.
Each stage has `name`, exactly one of `target_tool_call` or
`target_assistant_message`, optional `input_contains`, `max_examples` (default
64), `repeat` (default 1), optional `max_input_tool_messages`, optional
`strip_input_tool_result_payload`, optional `max_input_tool_content_chars`, and
optional `synthetic_tool_results`. Synthetic tool results are appended only to the
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
embed a protected answer. Recipe configs and CLI accept the same tool-prefix
shaping knobs via `strip_input_tool_result_payload`,
`max_input_tool_content_chars`,
`--training-recipe-strip-input-tool-result-payload`, and
`--training-recipe-max-input-tool-content-chars`; the recipe copies them into
every generated corrective stage.
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
summary. `--server-url` selects the OpenAI-compatible endpoint when supplied.
If it is omitted, the CLI derives `http://127.0.0.1:<server-port>/v1` from
`--server-port`, so managed vLLM runs on non-default ports do not wait on the
default 8000 endpoint. With `--manage-server`, the command starts and stops
vLLM locally; without it, the command targets an already-running endpoint. When
`--manage-server` receives an absolute vLLM executable path, the runner
prepends that executable's directory to the server `PATH`, so vLLM subprocesses
can find venv-local helpers such as `ninja` during FlashInfer JIT startup.

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
Codex command from `agent/codex.txt`, pairs it with a failed trajectory prefix,
and exports a Qwen tool-call SFT record using the default `full_trace`
projection. Prefer `--prompt-style live_replay` when failed local Harbor/Qwen
trajectories include `agent/evolab_lab/.evolab/registries/trajectory/llm_calls.jsonl`;
it reuses the real model `input_messages` immediately after `tb_read_task` and
supervises the successful `tb_exec` target. The default `--prompt-style
direct_solver` remains a synthetic direct-solver approximation for pools that
only contain Codex transcripts. Use `--prompt-style synthetic_correction` only
for explicit ablations of the old compact correction prompt. The default
`--target-mode final` exports one selected successful command. Use
`--target-mode sequence` for multi-step recipes such as `train-fasttext`, where
the final `/app/model.bin` write depends on earlier package installation or data
preparation commands; sequence mode exports progressive next-command examples
through the selected final target. Use `--target-exec-timeout-seconds` to pin a
runtime-compatible optional `timeout_seconds` argument on each supervised
`tb_exec` target when local tool-call models drift into malformed optional
arguments. Use `--run-worker` only when the trainer is ready to run locally.

```sh
OPEN_EVO_REPO=/path/to/OpenEvo
uv run polar-evolution terminal-bench-task-local-parametric-memory-job \
  --trajectory-pool /home/jyw/openevo-skill-rl-experiments/pools/tb21-current-20260702/trajectory_pool.jsonl \
  --task-id train-fasttext \
  --output-root /tmp/tb21-task-local-parametric/train-fasttext \
  --dataset-name tb21-task-local-train-fasttext \
  --base-model Qwen/Qwen3.6-35B-A3B \
  --adapter-id tb-parametric-memory-train-fasttext \
  --trainer-command /root/evolab-vllm/bin/python \
  --trainer-arg "${OPEN_EVO_REPO}/scripts/qwen_lora_sft.py" \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --command-contains /app/model.bin \
  --prompt-style live_replay \
  --target-mode sequence \
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
With `--target-mode sequence`, it first selects that final target and then emits
one SFT row per successful command up to and including it, preserving the task
state through synthetic `tb_exec` tool-result messages between targets.
With `live_replay`, `records.jsonl` contains the exact failed local harness
prefix selected from `llm_calls.jsonl`, including runtime Memory/Skills/Skill
Context blocks and the real `tb_read_task` tool result. With the synthetic
direct-solver prompt style, `records.jsonl` contains the direct-solver
system/user prompt, a masked `tb_read_task` assistant/tool prefix, and the
supervised target `tb_exec` as the final assistant tool call.
The repo helper `scripts/qwen_lora_sft.py` is an experiment script, not a main
package dependency; run it with a trainer Python environment that already has
`torch`, `transformers`, and `peft`. Pass this helper as an absolute path, or
expand a repo-root variable in the shell as shown above, because the worker
invokes the trainer from the artifact output directory rather than from the
OpenEvo repository root.

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

Single-task non-smoke evidence on 2026-07-07:

- Task-local training at
  `/tmp/tb21-task-local-parametric-train-full-20260707/train-fasttext-qwen35-9b-r8-s44`
  used `Qwen/Qwen3.5-9B`, `CUDA_VISIBLE_DEVICES=6`, 11 SFT records from the
  `train-fasttext` trajectory pool, LoRA rank 8, alpha 16, `max_length=1024`,
  and 44 trainer steps. The trainer diagnostics recorded loss moving from
  `1.1809505224227905` to `0.0003651840379461646`, and the worker registered a
  `parametric_memory` adapter under
  `/tmp/tb21-task-local-parametric-train-full-20260707/train-fasttext-qwen35-9b-r8-s44/artifacts/workers/job-tb-parametric-memory-train-fasttext-qwen35-9b-r8-s44/parametric_memory_lora_sft/adapter`.
- The paired local eval at
  `/tmp/tb21-task-local-parametric-eval-full-20260707/train-fasttext-qwen35-9b-r8-s44`
  used managed vLLM on GPU 6 with `Qwen/Qwen3.5-9B`, `n_attempts=1`,
  `--solver-temperature 0.0`, `--max-output-tokens 1024`,
  `--context-window-tokens 8192`, and `--tool-result-prompt-max-chars 2048`.
  Baseline pass@1/pass@k was `0/1`; parametric-memory pass@1/pass@k was `0/1`;
  delta was `0`. The treatment server command recorded `--enable-lora` and
  served model
  `tb-parametric-memory-train-fasttext-qwen35-9b-r8-s44`, so the adapter
  loading path was exercised. The task still failed because the treatment did
  not produce `/app/model.bin` for the verifier. Treat this as a negative
  single-task method result, not as full Terminal Bench performance evidence.
- A follow-up direct-solver-aligned run at
  `/tmp/tb21-task-local-parametric-train-direct-20260707/train-fasttext-qwen35-9b-direct-r8-s66`
  used `Qwen/Qwen3.5-9B`, `CUDA_VISIBLE_DEVICES=6`, 11 records,
  `--prompt-style direct_solver`, LoRA rank 8, alpha 16, `max_length=2048`,
  and 66 trainer steps. Loss moved from `1.0200927257537842` to
  `7.931108848424628e-05`. The paired eval at
  `/tmp/tb21-task-local-parametric-eval-direct-20260707/train-fasttext-qwen35-9b-direct-r8-s66`
  also produced baseline pass@1/pass@k `0/1` and parametric-memory
  pass@1/pass@k `0/1`, delta `0`.
- Diagnosis from the direct-solver-aligned treatment:
  the adapter was loaded by vLLM, but the treatment trajectory still began with
  exploratory commands (`ls -la`, `ls -la data/`, parquet inspection, fastText
  import checks) and never wrote `/app/model.bin`. The training records used a
  synthetic direct-solver prefix, while the live Qwen/Harbor request included
  runtime Memory/Skills/Skill Context blocks and a richer `tb_read_task` tool
  result. Additionally, the selected successful target command was the final
  `model.save_model('/app/model.bin')` command from a longer successful Codex
  sequence, so it depended on intermediate files such as `train_full.ft.txt`.
  These observations motivated the `live_replay` prompt style and one-shot
  target experiments.

Create a parametric-memory job from successful Terminal Bench trajectories and
run the local worker once:

```sh
OPEN_EVO_REPO=/path/to/OpenEvo
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
  --trainer-arg "${OPEN_EVO_REPO}/scripts/qwen_lora_sft.py" \
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
OPEN_EVO_REPO=/path/to/OpenEvo
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
  --trainer-arg "${OPEN_EVO_REPO}/scripts/qwen_lora_sft.py" \
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
OPEN_EVO_REPO=/path/to/OpenEvo
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
  --trainer-arg "${OPEN_EVO_REPO}/scripts/qwen_lora_sft.py" \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --training-projection terminal_bench_password_recovery_shorttarget_recipe \
  --training-recipe-target-command '<bounded command that derives /app/recovered_passwords.txt from task files>' \
  --training-recipe-after-read-repeat 6 \
  --training-recipe-correction-input-contains 'Dummy entry' \
  --training-recipe-max-input-tool-messages 5 \
  --training-recipe-strip-input-tool-result-payload \
  --training-recipe-max-input-tool-content-chars 512 \
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

Task-local `train-fasttext` parametric-memory attempts on Qwen3.5-9B did not
yet show a Terminal-Bench reward gain. A direct-solver-aligned SFT run at
`/tmp/tb21-task-local-parametric-train-direct-20260707/train-fasttext-qwen35-9b-direct-r8-s66`
trained 11 records for 66 steps (`loss: 1.02009 -> 0.000079`) and evaluated at
`/tmp/tb21-task-local-parametric-eval-direct-20260707/train-fasttext-qwen35-9b-direct-r8-s66`
with baseline `0/1`, parametric memory `0/1`, delta `0`. The treatment loaded
the LoRA adapter but still followed the base model's exploratory path (`ls`,
parquet inspection, fastText availability checks) and never wrote
`/app/model.bin`.

The next `train-fasttext` attempt added `--prompt-style live_replay` and a
self-contained one-shot target command. Training used one live-replay record at
`/tmp/tb21-task-local-parametric-train-live-replay-20260707/train-fasttext-qwen35-9b-live-replay-oneshot-r8-s80`
for 80 steps (`loss: 0.79877 -> 0.000034`). The first paired eval attempt was
diagnostically useful but not a valid benchmark result: the runner served the
base model and LoRA module under the same model id, so `/v1/models` exposed two
entries named `tb-parametric-memory-train-fasttext-qwen35-live-replay-oneshot-r8-s80`.
That made `model=adapter_id` ambiguous. The same run also hit repeated vLLM
context-length 400s at exact `prompt_tokens + output_tokens = max_model_len + 1`
boundaries when the agent kept exploring.

After the serving fix, a focused treatment-only rerun used base
`--served-model-name Qwen/Qwen3.5-9B` and LoRA module
`tb-parametric-memory-train-fasttext-qwen35-live-replay-oneshot-r8-s80=...`.
`/v1/models` then exposed distinct base and adapter ids, with the adapter's
`parent` set to the base model. This proved request routing could select the
adapter. The treatment trajectory at
`/tmp/tb21-task-local-parametric-eval-live-replay-fixed-20260707/treatment-only/harbor_jobs/parametric-memory-train-fasttext-fixed-routing/train-fasttext__viNqa4t`
did shift behavior compared with baseline (first `tb_exec` became
`ls -la /app && ls -la /app/data/`), but it still did not emit the trained
fastText one-shot command. It attempted `pip install fasttext`, installed
`scikit-learn`, and drifted into sklearn logistic-regression training rather
than producing a fastText binary. The run was stopped after this failure mode
was captured, so it should be treated as method debugging evidence, not a
formal pass@1 result. For completed `train-fasttext` paired runs so far, the
measured parametric-memory delta remains `0`.

The next framework change added `--target-mode sequence` for task-local
parametric-memory jobs. On the same real `train-fasttext` trajectory pool, the
sequence builder at
`/tmp/tb21-task-local-parametric-sequence-20260707/train-fasttext-qwen35-9b-sequence-smoke-v2`
exported 16 capped records from a 23-command successful recipe. Because the cap
is suffix-anchored in sequence mode, the exported records covered sequence
indices 7 through 22 and still included the final `/app/model.bin` target. A
Qwen3.5-9B 1-step trainer smoke at
`/tmp/tb21-task-local-parametric-sequence-20260707/train-fasttext-qwen35-9b-sequence-smoke-train-v2`
read all 16 records, trained one LoRA step on GPU 6, and registered a
`parametric_memory` artifact with `adapter_config.json`,
`adapter_model.safetensors`, and `trainer_diagnostics.json`
(`losses: [0.5574389696121216]`).

Two local-inference smoke eval attempts used the sequence adapter above with
managed Qwen3.5-9B vLLM on GPU 6. The first used `context_window_tokens=8192`
and reproduced the known boundary failure: vLLM rejected a later baseline
request with `prompt contains at least 8192 input tokens` plus requested output,
after which the baseline verifier path was stopped. The second used
`context_window_tokens=16384`, `max_output_tokens=512`, and
`EVOLAB_TB_MAX_SUBAGENT_TOOL_CALLS=12`; vLLM and the Harbor agent interacted
normally with no context-window rejection, and baseline failed cleanly by
tool-call budget, but the official `train-fasttext` verifier was still slow in
the smoke run and was stopped before the parametric-memory condition. These
smokes validate dataset/trainer/local-server plumbing for sequence mode, but do
not constitute a completed paired performance result.

The formal `train-fasttext` sequence follow-up at
`/tmp/tb21-task-local-parametric-sequence-formal-20260707/train-fasttext-qwen35-9b-sequence-r8-s160`
trained a Qwen3.5-9B LoRA on GPU 6 from 16 sequence records, LoRA rank 8,
alpha 16, `max_length=4096`, and 160 steps. Diagnostics recorded loss moving
from `0.4766453802585602` to `0.0007073074812069535`, and the worker
registered a `parametric_memory` adapter compatible with
`terminal-bench:train-fasttext`. The paired eval attempt at
`/tmp/tb21-task-local-parametric-sequence-formal-20260707/train-fasttext-qwen35-9b-sequence-r8-s160-eval`
used managed Qwen3.5-9B vLLM on GPU 6 and loaded the rewritten LoRA adapter.
Baseline failed by the 31-tool budget and then its official verifier timed out
after 1h7m while installing/building fastText verifier dependencies. The
treatment also failed by tool budget and did not create `/app/model.bin`; the
run was stopped before repeating the same long verifier path. Treat this as a
negative/inconclusive task-local method result and as evidence that
`train-fasttext` is poor for quick iteration unless verifier dependencies are
prebaked or otherwise cached under the benchmark protocol.

The next fast-verifier task-local run used `gcode-to-text`, which has both
failed and successful trajectory-pool records and a lightweight pytest
verifier. The dry projection at
`/tmp/tb21-task-local-parametric-gcode-20260707/dryrun` exported 16 sequence
records ending with the successful target command
`printf '%s' 'flag{gc0d3_iz_ch4LLenGiNg}' > /app/out.txt`. Training at
`/tmp/tb21-task-local-parametric-gcode-20260707/train-gcode-qwen35-sequence-r8-s120`
used Qwen3.5-9B, GPU 6, LoRA rank 8, alpha 16, `max_length=4096`, and 120
steps; diagnostics recorded loss moving from `1.722609043121338` to
`0.014706883579492569`, and the adapter was registered as
`tb-parametric-memory-gcode-qwen35-sequence-r8-s120`. The paired local eval at
`/tmp/tb21-task-local-parametric-gcode-20260707/eval-gcode-qwen35-sequence-r8-s120`
completed cleanly with baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`,
and delta `0`. The treatment server loaded the adapter with
`--enable-lora`, exposed model id
`tb-parametric-memory-gcode-qwen35-sequence-r8-s120`, and rewrote 64 LoRA keys.
The remaining failure is method alignment: the treatment followed the trained
sequence prefix too literally, starting with a command that ran
`rg --files -uu` after `pwd`, but the live task container does not have `rg`;
later generated shell and Python commands also failed before writing
`/app/out.txt`. This is a real negative performance result for the current
task-local sequence backend, not a serving-path failure.

A final-target follow-up on the same `gcode-to-text` pool tested whether the
sequence-prefix failure was the dominant issue. The dry projection at
`/tmp/tb21-task-local-parametric-gcode-final-20260707/dryrun` exported 16
records with `--target-mode final`; all records used the single target command
`/bin/bash -lc "printf '%s' 'flag{gc0d3_iz_ch4LLenGiNg}' > /app/out.txt"` and
none included the earlier `rg` exploration target. Training at
`/tmp/tb21-task-local-parametric-gcode-final-20260707/train-gcode-qwen35-final-r8-s100`
used Qwen3.5-9B, GPU 6, LoRA rank 8, alpha 16, `max_length=4096`, and 100
steps; diagnostics recorded loss moving from `1.5798166990280151` to
`1.3598812074633315e-05`. The paired eval at
`/tmp/tb21-task-local-parametric-gcode-final-20260707/eval-gcode-qwen35-final-r8-s100`
completed with baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`, and
delta `0`. The treatment server loaded the LoRA adapter with
`--enable-lora`, exposed model id
`tb-parametric-memory-gcode-qwen35-final-r8-s100`, and rewrote 64 adapter keys.
The method failure changed shape: after `tb_read_task`, the treatment emitted a
near-target `tb_exec` call, but its captured arguments were malformed
(`timeout_seconds` had no value) and the command drifted to `flag.txt` rather
than `/app/out.txt`. This shows final-target training removes the `rg` prefix
pollution but still needs stronger tool-argument/schema shaping before it can
be treated as a reliable task-local parametric-memory backend.

A timeout-shaped final-target follow-up fixed the runtime tool schema mismatch.
The dry projection at
`/tmp/tb21-task-local-parametric-gcode-timeout-20260707/dryrun` exported 16
records with `--target-mode final --target-exec-timeout-seconds 30`; every
target command was
`/bin/bash -lc "printf '%s' 'flag{gc0d3_iz_ch4LLenGiNg}' > /app/out.txt"`,
every target included `timeout_seconds: 30`, the `tb_exec` tool schema exposed
`timeout_seconds` as an integer with minimum 1, and no target contained `rg`.
Training at
`/tmp/tb21-task-local-parametric-gcode-timeout-20260707/train-gcode-qwen35-final-timeout-r8-s100`
used Qwen3.5-9B, GPU 6, LoRA rank 8, alpha 16, `max_length=4096`, and 100
steps; diagnostics recorded loss moving from `1.3398288488388062` to
`1.3986424164613709e-05`, and the worker registered one `parametric_memory`
artifact. The paired eval at
`/tmp/tb21-task-local-parametric-gcode-timeout-20260707/eval-gcode-qwen35-final-timeout-r8-s100`
completed with baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`, and
delta `0`. The treatment server loaded the LoRA adapter, rewrote 64 adapter
keys, and its first `tb_exec` carried a valid `timeout_seconds: 30`; the failure
therefore moved from malformed tool arguments to semantic target drift. The
treatment wrote the correct flag to `/app/challenge.txt`, then later variants
of `/app/challenge.txt`, while the verifier required `/app/out.txt`.

The next gcode iteration found that the earlier `--prompt-style live_replay`
jobs had silently fallen back to `direct_solver_read_task`, because the original
pool's failed gcode rows were Codex transcripts without Harbor/EvoLab
`llm_calls.jsonl`. A micro pool at
`/tmp/tb21-task-local-parametric-gcode-livereplay-20260707/micro_pool.jsonl`
paired the latest failed Qwen treatment trial with the same successful Codex
trial. The dry-run first showed another framework issue: Harbor's outer tool
message `content` can be truncated, while the full `tb_read_task` result lives
under `metadata.tool_result.content`; using the truncated display text dropped
the `/app/out.txt` constraint from the SFT prefix. The builder now preserves the
full metadata tool result for live replay tool messages. After the fix, the
dry-run at
`/tmp/tb21-task-local-parametric-gcode-livereplay-20260707/dryrun-fulltool`
exported 16 records from `live_replay_llm_call:2`; all 16 prompt prefixes
contained `/app/out.txt`, none contained `[truncated`, and all targets wrote
the correct flag to `/app/out.txt`.

Training at
`/tmp/tb21-task-local-parametric-gcode-livereplay-20260707/train-gcode-qwen35-livereplay-fulltool-r8-s100`
used Qwen3.5-9B, GPU 6, LoRA rank 8, alpha 16, `max_length=4096`, and 100
steps; diagnostics recorded loss moving from `1.2726507186889648` to
`1.6078307453426532e-05`. The paired eval at
`/tmp/tb21-task-local-parametric-gcode-livereplay-20260707/eval-gcode-qwen35-livereplay-fulltool-r8-s100`
still completed with baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`,
and delta `0`. The treatment no longer wrote `challenge.txt`, but it regressed
to `printf '%s' 'flag=gc0d3' > flag`, so preserving full live replay context was
necessary for correct training input but not sufficient for stable generation.

A mixed follow-up at `/tmp/tb21-task-local-parametric-gcode-mixed-20260707`
combined 16 original direct-solver fallback rows with 16 full live-replay rows.
The dry-run exported 32 records: 16 `direct_solver_read_task` and 16
`live_replay_llm_call:2`; every supervised target was the exact
`/app/out.txt` command. Training at
`/tmp/tb21-task-local-parametric-gcode-mixed-20260707/train-gcode-qwen35-mixed-r8-s120`
used the same Qwen3.5-9B LoRA settings with 120 steps and reached final losses
around `2.28e-05`. The paired eval at
`/tmp/tb21-task-local-parametric-gcode-mixed-20260707/eval-gcode-qwen35-mixed-r8-s120`
also scored baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`, delta `0`.
The treatment was more tool-sequence aligned (`tb_exec -> tb_run_tests ->
tb_collect_result`) but its first command rewrote `/app/text.gcode` with a small
dummy G-code file instead of writing `/app/out.txt`. This negative result
suggests the next method variable should not be more repetition of one target;
it needs stronger action-shape separation, such as training explicit
post-verifier correction stages or constraining output-file creation separately
from task input inspection/editing.

The framework now exposes the explicit post-verifier variant as
`--include-run-tests-correction` for task-local `live_replay` + final-target
jobs. When a failed local trajectory contains a failed `tb_run_tests` tool
result, the builder adds a second record whose prompt is the real prefix after
that verifier feedback and whose target is still the selected successful
`tb_exec` write command. This is intended for the next gcode run: the dry-run
should contain both the first-action live replay record and a
`live_replay_run_tests_correction_llm_call:*` record carrying
`candidate_artifacts` feedback that `/app/out.txt` is missing.
The v2 negative result then showed a later failure point: after
`tb_collect_result` had already collected the failed verifier result, the
treatment wrote a report claiming success instead of repairing the missing
artifact. The builder now also exposes `--include-collect-result-correction`,
which adds a supervised record from the real prefix after failed
`tb_collect_result` and targets the selected successful `tb_exec` repair. The
next gcode variant should therefore include first-action, post-run-tests, and
post-collect-result records as separate local-memory stages.

The first correction-stage gcode adapter used
`/tmp/tb21-task-local-parametric-gcode-correction-20260707/correction_pool.jsonl`,
which combined the prior mixed pool with repeated failed local treatment rows.
The dry-run exported 64 records: 16 `direct_solver_read_task`, 24
`live_replay_llm_call:2`, and 24
`live_replay_run_tests_correction_llm_call:4`; every target command wrote the
correct flag to `/app/out.txt`, every target carried `timeout_seconds=30`, and
24 prefixes included `candidate_artifacts` feedback. Training at
`/tmp/tb21-task-local-parametric-gcode-correction-20260707/train-gcode-qwen35-correction-r8-s160`
used Qwen3.5-9B, GPU 6, LoRA rank 8, alpha 16, `max_length=4096`, and 160
steps; diagnostics recorded 64 training records and loss moving from roughly
`1.35` to `1.57e-5`. The paired eval at
`/tmp/tb21-task-local-parametric-gcode-correction-20260707/eval-gcode-qwen35-correction-r8-s160`
completed with baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`, and
delta `0`. This adapter was active: the treatment immediately emitted the
correct flag string and valid `timeout_seconds=30`, while the baseline kept
inspecting `text.gcode`. The remaining error was output-path drift. The
treatment wrote `flag.txt` or `/app/flag.txt`, saw `tb_run_tests` report
`/app/out.txt present=false`, and still did not repair to `/app/out.txt`.

A second iterative correction adapter used the failed v1 treatment trajectory
itself as the negative prefix source:
`/tmp/tb21-task-local-parametric-gcode-correction-v2-20260707/correction_v2_pool.jsonl`.
Its dry-run exported 64 records: 32 first-action `live_replay_llm_call:2`
records and 32 `live_replay_run_tests_correction_llm_call:4` records. All
targets were the exact `/app/out.txt` write command, all carried
`timeout_seconds=30`, all prefixes contained `/app/out.txt`, and all correction
prefixes contained both `flag.txt` and missing-artifact feedback. Training at
`/tmp/tb21-task-local-parametric-gcode-correction-v2-20260707/train-gcode-qwen35-correction-v2-r8-s120`
used the same Qwen3.5-9B LoRA settings for 120 steps and reached final losses
around `7e-6`. The paired eval at
`/tmp/tb21-task-local-parametric-gcode-correction-v2-20260707/eval-gcode-qwen35-correction-v2-r8-s120`
again scored baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`, delta `0`.
The treatment wrote the correct flag to `/app/flag.txt`, called
`tb_run_tests`, wrote a report incorrectly claiming success, and never created
`/app/out.txt`; the official verifier failed `test_hello_file_exists` and
`test_hello_file_content`. Treat these gcode correction runs as active-adapter
method negatives: the LoRA reliably injects the answer string and timeout
schema, but path binding remains unstable under this pure SFT task-local memory
construction.

The collect-result correction follow-up used the failed v2 treatment trajectory
as the negative prefix source:
`/tmp/tb21-task-local-parametric-gcode-collect-correction-20260707/collect_correction_pool.jsonl`.
The dry-run exported 72 records: 24 first-action `live_replay_llm_call:2`
records, 24 `live_replay_run_tests_correction_llm_call:4` records, and 24
`live_replay_collect_result_correction_llm_call:5` records. All targets wrote
the correct flag to `/app/out.txt`, all carried `timeout_seconds=30`, and the
collect-result prefixes included nested failed verifier output. Training at
`/tmp/tb21-task-local-parametric-gcode-collect-correction-20260707/train-gcode-qwen35-collect-correction-r8-s140`
used Qwen3.5-9B, GPU 6, LoRA rank 8, alpha 16, `max_length=4096`, and 140
steps; diagnostics recorded 72 training records and loss moving from roughly
`1.27` to `6.9e-6`. The paired eval at
`/tmp/tb21-task-local-parametric-gcode-collect-correction-20260707/eval-gcode-qwen35-collect-correction-r8-s140`
again scored baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`, delta `0`.
The treatment no longer wrote a premature report, but it wrote the correct flag
to `/app/text.gcode`; the official verifier still failed because `/app/out.txt`
did not exist. This isolates output-path binding, rather than correction-stage
timing, as the remaining failure.

A path-bound target variant used a synthetic successful Codex transcript whose
target command repeated `/app/out.txt` three times and verified the file in
shell:
`/tmp/tb21-task-local-parametric-gcode-pathbound-20260707/pathbound_pool.jsonl`.
The dry-run again exported 72 records across the same three stage types, with
no target references to `/app/text.gcode`. Training at
`/tmp/tb21-task-local-parametric-gcode-pathbound-20260707/train-gcode-qwen35-pathbound-r8-s140`
used the same Qwen3.5-9B LoRA settings for 140 steps and reached final losses
around `9.6e-6`. The paired eval at
`/tmp/tb21-task-local-parametric-gcode-pathbound-20260707/eval-gcode-qwen35-pathbound-r8-s140`
still scored baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`, delta `0`.
The treatment compressed the path-bound target into a wrong variant,
`out=/app/out.bin` with content `AVAY`, so the official verifier again failed
because `/app/out.txt` did not exist. Across these gcode variants, the
Qwen3.5 LoRA changes action shape but pure task-local SFT is not enough to
reliably preserve literal output-path and content bindings. The next method
should either add a constrained decode/validation layer for critical artifact
paths, or move from single-task memorization to broader path-binding
supervision before treating `gcode-to-text` as a positive-control task.

The local parametric eval runner now exposes the constrained-path experiment
variable explicitly with `--artifact-path-guard {off,audit,repair}` and repeated
`--required-artifact-path /app/...`. This is not a new memory backend: baseline
and treatment both receive the same guard mode. It records whether the
Terminal-Bench/EvoLab execution layer should only audit path mismatches or try
to repair supported `tb_exec` commands before execution, and it writes the
selected mode plus required paths into the dry-run/live summary. The default is
`off`; actual audit/repair behavior depends on the installed task package
honoring `EVOLAB_TB_ARTIFACT_PATH_GUARD` and
`EVOLAB_TB_REQUIRED_ARTIFACT_PATHS`.

A guard-backed gcode eval at
`/tmp/tb21-task-local-parametric-gcode-pathbound-guard-20260707/eval-gcode-qwen35-pathbound-repair-r8-s140`
used the path-bound Qwen3.5-9B adapter above with
`--artifact-path-guard repair`, `--required-artifact-path /app/out.txt`,
managed vLLM on GPU 6, deterministic decoding, and the
`qwen3_5_vllm_language_model` adapter key rewrite. The summary recorded
`rewritten_key_count=64`, baseline pass@1/pass@k `0/1`,
parametric-memory pass@1/pass@k `0/1`, and delta `0`. This run validates the
guard plumbing and summary contract, but it is not positive method evidence.
The baseline wrote `/app/out.txt` with the wrong content, so there was no
single wrong output path for repair to rewrite. The treatment loaded the
adapter but produced no tool call: the OpenAI-compatible response ended with
`finish_reason="length"`, empty content, and no `tool_calls`, which the Harbor
bridge reported as `{"error": "empty_model_response"}`. Inspecting the raw
reasoning showed the model stalled before `tb_read_task` because the
direct-solver prompt did not expose a concrete usable task id. Before adding
more gcode SFT variants, fix the local Qwen direct-solver bootstrap so the
runtime task id is explicit or defaulted consistently; the artifact-path guard
can only repair supported `tb_exec` commands after a tool call exists.

A local Terminal-Bench/EvoLab package patch then prepended the concrete task id
to the direct-solver task resources and `TerminalSolveAgent` system prompt. The
rerun at
`/tmp/tb21-task-local-parametric-gcode-pathbound-guard-20260707/eval-gcode-qwen35-pathbound-repair-taskid-r8-s140`
used the same adapter and guard settings and again scored baseline pass@1/pass@k
`0/1`, parametric-memory pass@1/pass@k `0/1`, delta `0`. The prompt fix worked
for the first failure layer: the treatment's first model call emitted
`tb_read_task({"task_id":"terminal-bench-task"})` instead of stalling before any
tool call. The second model call, after the real task description had been read,
still produced no `tb_exec`; Harbor recorded the final assistant message as
`{"error":"empty_model_response"}`, and the official verifier failed because
`/app/out.txt` was never created. This isolates the next method problem as
post-`tb_read_task` action generation, not task-id discovery or LoRA serving.

For `password-recovery`, a Qwen3.5-9B local LoRA smoke was trained from the
existing failed/successful tool-policy trajectory at
`/tmp/tb21-parametric-memory-password-toolpolicy-20260702-110343/local-eval-password-toolpolicy-2048/baseline/harbor_jobs/baseline-password-recovery/password-recovery__AzMbthq`.
The first Qwen3.5 adapter used the short-target recipe with
`target_task_id=terminal-bench-task` and wrote artifact
`art_2cd9a7338f1d4891` under
`/tmp/tb21-parametric-memory-qwen35-password-20260707-train`. It trained 84
steps on 8 projected records and reached final losses around `4e-5`. A paired
local eval at
`/tmp/tb21-parametric-memory-qwen35-password-20260707-eval` initially completed
with pass@1 delta `0`, but the treatment vLLM log showed late context-boundary
rejections when the prompt reached roughly 16,375 input tokens. Rerunning the
same adapter with `max_output_tokens=512`, `context_reserve_tokens=512`, and
`tool_result_prompt_max_chars=512` at
`/tmp/tb21-parametric-memory-qwen35-password-20260707-eval-tight` produced a
clean paired result: baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`,
delta `0`, and no vLLM context errors. The treatment server loaded the LoRA,
but both conditions failed by subagent budget and the treatment did not emit
the trained `find varsea/disks` recipe.

A second Qwen3.5 adapter aligned the recipe target to the current direct-solver
tool id, `target_task_id=static-node-0`, increased the after-read target repeat
to 32, and trained LoRA rank 16 for 160 steps on 37 projected records. The
artifact is `art_fd254dacec40485d` under
`/tmp/tb21-parametric-memory-qwen35-password-staticnode-20260707`; trainer
diagnostics report final losses around `3.5e-5`. The paired eval at
`/tmp/tb21-parametric-memory-qwen35-password-staticnode-20260707-eval-tight`
again completed cleanly with baseline pass@1 `0/1`, parametric-memory pass@1
`0/1`, and delta `0`. Both vLLM stderr logs had no
`VLLMValidationError`/context-length error, and the treatment stdout confirmed
the LoRA module was mounted. However, the baseline and treatment tool-call
sequences were exactly identical for all 25 calls, starting with
`tb_read_task(static-node-0)`, `tb_exec("ls -la")`, and repeated shallow
inspection of `*.bin` files. A direct single-prefix probe against training
record 1 then isolated the failure: under HF/PEFT, the base Qwen3.5 model
emitted the exploratory `ls -la /app/varsea/` call while the adapter emitted
the trained `find varsea/disks` recovery command. Serving the same adapter
through vLLM without an adapter key rewrite exposed the adapter model id but
produced the same `ls -la` output as the base model. After rewriting the
adapter keys for vLLM `--language-model-only`, the direct vLLM probe split as
expected: base emitted `ls -la`, treatment emitted the trained recovery
command. Treat the paired eval above as a serving-compatibility negative, not a
method-performance negative; rerun it with
`--adapter-key-rewrite qwen3_5_vllm_language_model` before interpreting the
Qwen3.5 `password-recovery` delta.

The rerun at
`/tmp/tb21-parametric-memory-qwen35-password-staticnode-20260707-eval-rewrite`
used the same static-node adapter with
`--adapter-key-rewrite qwen3_5_vllm_language_model`; the summary recorded
64 rewritten keys, baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`, and
delta `0`. This run confirms the serving fix reaches the full Terminal-Bench
harness: the treatment no longer matches the baseline tool-call sequence and
vLLM logs show LoRA kernels in use. The remaining failure is method/schema
alignment. After `tb_read_task`, the adapter emitted a `tb_exec` call whose
arguments were captured as `_raw_arguments` without the required `task_id`;
the agent then failed on a vLLM/OpenAI-compatible 400
`Unterminated string starting at: line 1 column 13`. The generated command also
drifted from the trained `varsea/disks` and `8XD...W54` pattern toward
`varsea/disconnected` and numeric fragments. Treat this as evidence that the
Qwen3.5 static-node adapter is active but not yet a usable task-local memory
method.

Inspection of the static-node adapter's `training.jsonl` versus the live
treatment `llm_calls` found a second, earlier alignment issue: the training
tool-result prefix included a duplicated appended `Tool result payload` section,
while the runtime prefix contained only the compact tool message. Exact-prefix
HF/PEFT and vLLM probes therefore showed the adapter could memorize the shaped
training prefix, but full Terminal-Bench evaluation queried a different prefix.
For follow-up Qwen3.5 password-recovery recipe runs, use the recipe shaping
flags above so the SFT records strip appended payloads and cap tool-result
content before filtering and training.

A shaped follow-up adapter was trained from the same `password-recovery`
trajectory with `target_task_id=static-node-0`, `after_read_input_contains` set
to `tb_read_task_inventory`, stripped appended tool-result payloads, capped
input tool content at 512 characters, 32 after-read repeats, and 4 correction
repeats. The dry projection produced 37 records with no `Tool result payload`
marker and max tool-message content length 512. The training run at
`/tmp/tb21-parametric-memory-qwen35-password-staticnode-shaped-v2-train-20260707`
registered artifact `art_6d348034b5f2421b`; diagnostics report 160 trained
steps, CUDA device `6`, and final loss around `1.52e-5`. The paired local eval
at
`/tmp/tb21-parametric-memory-qwen35-password-staticnode-shaped-v2-eval-urlfix-20260707`
used `--server-url http://127.0.0.1:8011/v1`,
`--server-port 8011`, `--adapter-key-rewrite qwen3_5_vllm_language_model`,
`max_output_tokens=512`, `context_reserve_tokens=512`, and
`tool_result_prompt_max_chars=512`. It completed with baseline pass@1 `0/1`,
parametric-memory pass@1 `1/1`, and delta `+1` on `password-recovery`. The
treatment vLLM `/v1/models` endpoint exposed both the base model and adapter
model id, and the summary recorded 64 rewritten LoRA keys. The treatment
tool-call trace read `static-node-0` and then emitted the trained recovery
command in one `tb_exec` call; the verifier passed both
`test_recovery_file_exists` and `test_password_match`. A first attempt at the
same eval accidentally set only `--server-port 8011` while leaving
`--server-url` at the default port 8000, so it stayed in server readiness and
never launched Harbor. The CLI now derives the default URL from
`--server-port`; if `--server-url` is supplied explicitly, keep it aligned with
the managed-server port.

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
  --adapter-key-rewrite qwen3_5_vllm_language_model \
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
managed vLLM `--max-model-len`; the Harbor agent receives a context window 64
tokens smaller through `EVOLAB_TB_CONTEXT_WINDOW_TOKENS` to avoid exact-boundary
server rejections. `--tool-result-prompt-max-chars` sets
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

Use `--adapter-key-rewrite qwen3_5_vllm_language_model` when a Qwen3.5/Qwen3.6
PEFT LoRA is served through vLLM's `--language-model-only` wrapper. This applies
to both dense Qwen3.5 local models and Qwen3.5/Qwen3.6 MoE aliases in vLLM
0.21.0: the PEFT trainer writes `base_model.model.model.layers.*`, while vLLM
expects the effective language-model LoRA keys under
`base_model.model.model.language_model.layers.*`. The runner copies the adapter
under `run_root/prepared_adapters/<adapter-id>/<rewrite>/adapter`, rewrites only
the safetensors key prefixes, and records both the source and serving adapter
paths plus the rewritten key count in the summary. Treat `rewritten_key_count=0`
as a configuration error; without this rewrite, vLLM can expose the adapter
model id while the generated logits remain unchanged. The older
`qwen3_5_moe_vllm_language_model` spelling is still accepted as a compatibility
alias for existing scripts and summaries.
