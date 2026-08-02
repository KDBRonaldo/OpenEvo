# Terminal Bench 2.1 Memory-Only Evaluation

This note records the control-variable setup for evaluating memory evolution on
Terminal Bench 2.1.

> **Status: historical experiment record.** The
> `parametric_memory_lora_sft`, trainer-command, corrective projection, and
> routing configurations below are retired and are not accepted by the current
> Core registry. Current parametric-memory development uses the internal
> `parametric_memory_sd_lora` method, a fixed Daemon-owned trainer, and one
> cumulative PEFT adapter. See
> [Terminal Bench 2.1 Continual Parametric-Memory Evaluation](terminal-bench-continual-memory-eval.md)
> for the current command and evidence. Use the architecture documents for
> current contracts; use the remainder of this note only to interpret earlier
> Terminal Bench runs.

The consolidated, task-level MemEvolve and continual-parametric results are in
[Terminal Bench 2.1 Memory Method Results](memory-method-experiment-results.md).

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
openevo-terminal-bench terminal-bench-per-task-evolution \
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

## Declarative MemEvolve Conditional Result

The later `text_memory_memevolve` run used the same 21-task conditional pool,
`gpt-5.5` Codex subscription inference, one attempt per condition, and only the
`text_memory` evolution target. It is a declarative static-Markdown adaptation:
artifact manifests record `adaptation_scope=declarative_text_memory_v1` and
`paper_equivalent=false`. It does not execute the upstream MemEvolve provider
runtime.

After correcting two baseline verifier timeouts with successful baseline
reruns, the paired result was:

- corrected baseline pass@1: 2/21;
- declarative MemEvolve pass@1: 9/21;
- absolute gain: 7 tasks and 33.33 percentage points;
- transitions: 7 fail-to-pass, 2 pass-to-pass, 12 fail-to-fail, and 0
  pass-to-fail.

The corrected summary has SHA-256
`1ba21c752d91d184fdd0aac8f5050e6597b212679ae06791293a18b390916b8e`.
See the
[canonical task-level record](memory-method-experiment-results.md#task-level-results)
for all 21 outcomes, the uncorrected-summary identity, implementation status,
and limitations. This result is not directly comparable to the five-attempt
ExpeL-family evidence above and is not an unbiased full-suite score.

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
arguments. Use `--target-command` when a successful Codex trajectory completed
through a non-shell `file_change` event, or when the desired research target is
a manually audited shell command rather than a selected successful
`command_execution`; this is supported only with `--target-mode final` and is
recorded in the dataset metadata as a manual target. Use
`--include-tool-schema-lock` for local-inference adapters that still emit
malformed `tb_exec` arguments after `tb_read_task`; it adds one short
after-read-task record whose supervised target is a complete `tb_exec` call with
`task_id`, `command`, and optional `timeout_seconds`. With `--target-mode
sequence`, the schema-lock target is the first successful command in the
sequence so the record shapes the first post-read-task action. Use
`--target-repeat N` with `--target-mode final` when a manually audited final
target needs extra SFT weight, for example exact literal file writes that are
otherwise easy for a small local adapter to copy with casing or quoting drift.
Repeated records keep the same supervised `tb_exec` target and record
`target_repeat_index` / `target_repeat_count` in metadata for auditability. Use
`--tool-schema-lock-repeat N` together with `--include-tool-schema-lock` when
literal target repeats preserve the command text but the local model drifts into
incomplete tool arguments such as missing `task_id`; repeated schema-lock records
keep the same complete `tb_exec` target and record
`tool_schema_lock_repeat_index` / `tool_schema_lock_repeat_count`. Use
`--run-worker` only when the trainer is ready to run locally.

```sh
OPEN_EVO_REPO=/path/to/OpenEvo
openevo-terminal-bench terminal-bench-task-local-parametric-memory-job \
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
Each SFT record records the selected `target_command` and extracted
`target_app_paths` in metadata, and the dataset manifest records the target
filters used by the CLI. These fields are audit metadata only; they do not
change the supervised messages. They let later gcode/path-binding experiments
separate what the adapter was trained to emit from what an eval-time artifact
path guard repaired.
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
openevo-terminal-bench terminal-bench-parametric-memory-job \
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
openevo-terminal-bench terminal-bench-parametric-memory-job \
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
openevo-terminal-bench terminal-bench-parametric-memory-job \
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

A second `train-fasttext` follow-up used local Qwen failed trajectories instead
of Codex-only failed references. The pool at
`/tmp/tb21-task-local-parametric-trainfasttext-local-correction-20260708/local_correction_pool.jsonl`
combined three failed local Qwen3.5 baseline attempts with one successful Codex
reference row. A filtered target search requiring the literal
`fasttext supervised` produced no records, but the unfiltered dry-run at
`/tmp/tb21-task-local-parametric-trainfasttext-local-correction-20260708/dryrun-any`
selected a 755-character target command that trains fastText and saves
`/app/model.bin`. It exported three task-local records, all from
`live_replay_llm_call:2`, all targeting `/app/model.bin`, and no
`tb_run_tests` or `tb_collect_result` correction records. This makes the run a
local-live target-command experiment, not a full correction-stage experiment.

Training at
`/tmp/tb21-task-local-parametric-trainfasttext-local-correction-20260708/train-trainfasttext-qwen35-local-live-r8-s80`
used Qwen3.5-9B on GPU 6 with LoRA rank 8, alpha 16, max length 4096, and
80 SFT steps. The registered artifact was
`tb-parametric-memory-trainfasttext-local-live-r8-s80` with
`training_record_count=3`; diagnostics recorded loss moving from about `0.93`,
`0.89`, `0.82` to roughly `5.6e-05`. The paired eval at
`/tmp/tb21-task-local-parametric-trainfasttext-local-correction-20260708/eval-trainfasttext-qwen35-local-live-r8-s80`
used the clean Terminal-Bench/EvoLab package PR #34, managed Qwen3.5-9B vLLM
on GPU 6 and port 8014, deterministic decoding,
`--context-window-tokens 32768`, and no artifact-path guard. Only
`parametric_memory` was enabled; the summary recorded baseline pass@1/pass@k
`0/1`, parametric-memory pass@1/pass@k `0/1`, and delta `0`, with no Harbor
exceptions. The treatment server command included LoRA serving and the baseline
server command did not; GPU 6 and port 8014 were released after the run.

The failure mode is method-relevant. Baseline read the task, issued four
`tb_exec` inspection/package commands, and then hit `empty_model_response`.
The LoRA treatment read the task and immediately hit `empty_model_response`
without issuing any `tb_exec`; it never attempted the trained `/app/model.bin`
target. This is a clean negative result for the current three-record
local-live SFT recipe on a second task. The next task-local parametric method
should not simply repeat one long final command from a successful transcript;
it needs shorter staged targets, more local correction points, or an explicit
finish-policy/objective stage that keeps action generation alive after
`tb_read_task`.

A staged-target experiment then tested that direction without changing the
framework. The dry-run at
`/tmp/tb21-task-local-parametric-trainfasttext-staged-20260708/dryrun-install-sequence`
used the same three local Qwen failures plus one Codex success, but switched to
`--target-mode sequence`, required the successful trajectory to reach
`python -m pip install fasttext`, and excluded the earlier `rg --files`
exploration command that is brittle in the live container. It exported 21
records: each failed local prefix contributed seven short targets, starting
with data/environment inspection and ending at fastText installation. All
records used `live_replay_llm_call:2`, and the target commands were much
shorter than the previous 755-character final training command.

Training at
`/tmp/tb21-task-local-parametric-trainfasttext-staged-20260708/train-trainfasttext-qwen35-install-sequence-r8-s100`
used Qwen3.5-9B on GPU 6 with LoRA rank 8, alpha 16, max length 4096, and
100 SFT steps. The registered artifact was
`tb-parametric-memory-trainfasttext-install-sequence-r8-s100` with
`training_record_count=21`; diagnostics recorded losses moving from about
`1.10`, `0.65`, `0.67` to the `1e-5` to `2e-4` range. The paired eval at
`/tmp/tb21-task-local-parametric-trainfasttext-staged-20260708/eval-trainfasttext-qwen35-install-sequence-r8-s100`
used the clean Terminal-Bench/EvoLab package PR #34, managed Qwen3.5-9B vLLM
on GPU 6 and port 8015, deterministic decoding,
`--context-window-tokens 32768`, and no artifact-path guard. Only
`parametric_memory` was enabled. The official summary still scored baseline
pass@1/pass@k `0/1`, parametric-memory pass@1/pass@k `0/1`, delta `0`, with no
Harbor exceptions. LoRA serving was enabled only for treatment, and GPU 6 plus
port 8015 were released after the run.

Although this did not improve reward, it is a stronger method diagnostic than
the previous local-live final-command run. Baseline executed 18 `tb_exec` calls
and failed by budget; treatment executed 26 `tb_exec` calls and also failed by
budget. The important difference is that the prior treatment read the task and
immediately returned `empty_model_response`, while the staged adapter kept
action generation alive and produced many fastText-related commands. This
supports the staged-target direction, but the current bootstrap stage stops too
early in the solve process: it restores exploration/install behavior without
teaching a reliable final model-training and `/app/model.bin` production stage.
The next method iteration should extend the sequence through verified local
training and add correction records around failed package/model commands, rather
than only training the bootstrap install segment.

The next extended-stage `train-fasttext` adapter follows that recommendation
and is ready for paired eval when a clean local GPU is available. The dry-run at
`/tmp/tb21-task-local-parametric-trainfasttext-extended-20260708/dryrun-model-sequence`
again used the three local Qwen failures plus one Codex success, but required
the successful trajectory to reach `/app/model.bin` and still excluded
`rg --files`. With `--target-mode sequence` and `--max-records-per-task 30`, it
exported 30 records: one failed local prefix received the full 22-step sequence
from data/environment inspection through model production, and a second failed
local prefix received the final eight model-production targets. Six records
explicitly targeted `/app/model.bin`; the rest teach the preceding data,
package, normalization, and fastText training stages.

Training at
`/tmp/tb21-task-local-parametric-trainfasttext-extended-20260708/train-trainfasttext-qwen35-model-sequence-r8-s100`
used Qwen3.5-9B on GPU 6 with LoRA rank 8, alpha 16, max length 4096, and
100 SFT steps. The adapter directory was written at
`artifacts/workers/job-tb-parametric-memory-trainfasttext-model-sequence-r8-s100/parametric_memory_lora_sft/adapter`
and contains `adapter_model.safetensors`, tokenizer files, and
`trainer_diagnostics.json`. Diagnostics recorded `record_count=30`,
`trained_steps=100`, and losses moving from about `1.10`, `0.65`, `0.68` to a
noisier final tail between roughly `0.0014` and `0.37`. The initial process
was interrupted before the CLI wrote its top-level `job.json`; this was later
recovered by replaying the same payload generation command without
`--run-worker`. The recovered `job.json` records `record_count=30`,
`selected_tasks=["train-fasttext"]`, `prompt_style=live_replay`, and
`target_mode=sequence`. The adapter artifact itself is complete and can be used
directly for eval with adapter id
`tb-parametric-memory-trainfasttext-model-sequence-r8-s100`.

The paired eval for this extended adapter was deferred until a clean local GPU
was available, then run at
`/tmp/tb21-task-local-parametric-trainfasttext-extended-20260708/eval-trainfasttext-qwen35-model-sequence-r8-s100-tight1024-tool512`.
It used managed Qwen3.5-9B vLLM on GPU 7, deterministic decoding,
`max_output_tokens=1024`, `context_reserve_tokens=1024`,
`tool_result_prompt_max_chars=512`, no artifact-path guard, adapter key rewrite
`qwen3_5_vllm_language_model`, and only `parametric_memory` enabled. The summary
recorded baseline pass@1/pass@k `0/1`, parametric-memory pass@1/pass@k `0/1`,
and delta `0`, with no Harbor exceptions. The baseline exhausted the tool
budget after 31 tool calls without producing `/app/model.bin`. The treatment
loaded the LoRA, read the task, inspected data files, installed fastText, then
hit a here-document quoting error while inspecting the parquet schema. It
retried `python3 -m pip install fasttext`, but the next LLM response was
`{"error": "empty_model_response"}` and no `/app/model.bin` was created. This
negative result confirms that the 30-record extended sequence improved the
early action path relative to baseline, but still did not teach robust recovery
or final model production.

Before that extended eval result was available, an offline iterative-data
dry-run checked whether the current builder can reuse active-adapter failures.
The pool
at
`/tmp/tb21-task-local-parametric-trainfasttext-iterative-20260708/iterative_pool.jsonl`
combined the failed install-sequence treatment trajectory
`train-fasttext__R86vp3U` with the same successful Codex reference. The dry-run
at
`/tmp/tb21-task-local-parametric-trainfasttext-iterative-20260708/dryrun-model-sequence`
again targeted `/app/model.bin`, excluded `rg --files`, used
`--target-mode sequence`, and exported 22 records. All records used
`prefix_source=live_replay_llm_call:2`; prompt tool-message counts then grew
from 1 to 22 only because the builder appended successful previous-command
messages from the Codex sequence. It did not use the staged treatment's later
failed `tb_exec` outputs as correction prefixes.

This is an important method boundary. Replacing the failed pool row with a
more informative active-adapter failure is not enough under the current
`live_replay` semantics: the backend still trains from the first post-read-task
action point and does not directly condition on the actual failed package,
normalization, or model-training commands. The framework now exposes this
missing stage as `--include-tb-exec-failure-correction`, analogous to the
existing `tb_run_tests`/`tb_collect_result` correction records. It selects later
failed `tb_exec` tool results from the local trajectory, preserves the live
prefix through the concrete failed command output, and targets the selected
successful final `tb_exec` command. It is intentionally final-target-only for
now; per-failure alignment to intermediate recovery commands remains a future
method extension.

For `train-fasttext__R86vp3U`, the failed-tool evidence gives a concrete
contract for that stage. The treatment had 26 `tb_exec` artifacts: 17 with
exit code 0, eight with exit code 1, and one with exit code 2. The failed
commands were not random terminal noise; they clustered around actionable
training failures: two short fastText import/version checks failed with
here-document syntax errors, one parquet inspection command failed with a shell
syntax error, four fastText/parquet training attempts failed with tracebacks,
two attempts failed around closed temporary files, and the final long command
failed with an unterminated here-document string. A useful
`tb_exec_failure` correction record should therefore select an LLM call whose
input contains a failed `tb_exec` tool result, preserve the compact live replay
prefix through that failed result, and target the next successful recovery or
model-production `tb_exec` from the successful Codex sequence. The metadata
now exposes `target_correction_stage="tb_exec_failure"`,
`failed_tool_name="tb_exec"`, `failed_exit_code`, `failed_tool_index`, and a
small normalized failure flag list such as `syntax`, `traceback`,
`fasttext`, `parquet`, or `model_bin`. This is the missing dataset shape for
learning from active-adapter failures rather than only replaying first-action
or verifier-result corrections.

After that framework change, the `tb_exec_failure` dry projection was rerun
without starting GPU training. The first run used only the active-adapter failed
trial:
`/tmp/tb21-task-local-parametric-trainfasttext-tbexec-correction-20260708/dryrun-tb-exec-failure-final`.
It exported two records from
`/tmp/tb21-task-local-parametric-trainfasttext-iterative-20260708/iterative_pool.jsonl`:
one base `live_replay_llm_call:2` record and one
`live_replay_tb_exec_failure_correction_llm_call:10` record tagged
`target_correction_stage="tb_exec_failure"`. The correction preserved the real
failed `tb_exec` output and tagged the failure as
`syntax`, `fasttext`, and `timeout`; the selected target remained the successful
`/app/model.bin` write command with `timeout_seconds=300`.

The wider candidate pool at
`/tmp/tb21-task-local-parametric-trainfasttext-tbexec-correction-20260708/combined_local_and_active_pool.jsonl`
then combined three failed local baseline trials, the failed active-adapter
trial `train-fasttext__R86vp3U`, and the same successful Codex reference. The
candidate job at
`/tmp/tb21-task-local-parametric-trainfasttext-tbexec-correction-20260708/dryrun-combined-tb-exec-failure-final/job.json`
exported eight records: four base first-action records and four
`tb_exec_failure` correction records. Three corrections captured
`ModuleNotFoundError: No module named 'fasttext'` style import failures and were
tagged `traceback`, `fasttext`, `timeout`; the active-adapter correction captured
the here-document fastText command failure and was tagged `syntax`, `fasttext`,
`timeout`. All eight targets were the same successful `/app/model.bin` command,
with `--command-contains /app/model.bin`, `--exclude-command-contains 'rg --files'`,
`--prompt-style live_replay`, `--target-mode final`, and
`--include-tb-exec-failure-correction`.

The corrected trainer payload for this candidate uses
`/root/evolab-vllm/bin/python` and `scripts/qwen_lora_sft.py` with
`--model-name Qwen/Qwen3.5-9B`, LoRA rank 8, alpha 16, `max_length=4096`, and
120 steps. As of the dry-run audit, all GPUs still had high resident memory,
GPU 3-6 had high utilization, and GPU 7 was serving `qwen3.5-curator`; training
and paired eval are therefore deferred until a safe local GPU is available. The
intended training command is:

```sh
CUDA_VISIBLE_DEVICES=<free_gpu> \
openevo-terminal-bench terminal-bench-task-local-parametric-memory-job \
  --trajectory-pool /tmp/tb21-task-local-parametric-trainfasttext-tbexec-correction-20260708/combined_local_and_active_pool.jsonl \
  --task-id train-fasttext \
  --output-root /tmp/tb21-task-local-parametric-trainfasttext-tbexec-correction-20260708/train-combined-tb-exec-failure-r8-s120 \
  --dataset-name tb21-task-local-train-fasttext-combined-tbexec-failure \
  --base-model Qwen/Qwen3.5-9B \
  --adapter-id tb-parametric-memory-train-fasttext-combined-tbexec-failure-r8-s120 \
  --trainer-command /root/evolab-vllm/bin/python \
  --trainer-timeout-seconds 3600 \
  --trainer-arg /root/.config/superpowers/worktrees/ProRL-Agent-Server/openevo-memory-backends/scripts/qwen_lora_sft.py \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --trainer-arg --model-name \
  --trainer-arg Qwen/Qwen3.5-9B \
  --trainer-arg --max-steps \
  --trainer-arg 120 \
  --trainer-arg --lora-r \
  --trainer-arg 8 \
  --trainer-arg --lora-alpha \
  --trainer-arg 16 \
  --trainer-arg --max-length \
  --trainer-arg 4096 \
  --command-contains /app/model.bin \
  --exclude-command-contains 'rg --files' \
  --prompt-style live_replay \
  --target-mode final \
  --target-exec-timeout-seconds 300 \
  --include-tb-exec-failure-correction \
  --max-records-per-task 8 \
  --run-worker \
  --artifact-root /tmp/tb21-task-local-parametric-trainfasttext-tbexec-correction-20260708/train-combined-tb-exec-failure-r8-s120/artifacts \
  --output /tmp/tb21-task-local-parametric-trainfasttext-tbexec-correction-20260708/train-combined-tb-exec-failure-r8-s120/job.json
```

Because the correction-only final-target job does not teach the multi-step
recipe, a mixed CPU-side candidate was also prepared without starting training.
It concatenates the already trained 30-record sequence dataset from
`/tmp/tb21-task-local-parametric-trainfasttext-extended-20260708/train-trainfasttext-qwen35-model-sequence-r8-s100/dataset/records.jsonl`
with only the four `tb_exec_failure` records from the combined correction
dataset, excluding duplicate first-action base records. The mixed job at
`/tmp/tb21-task-local-parametric-trainfasttext-mixed-20260708/dryrun-sequence-plus-tbexec-correction/job.json`
therefore contains 34 records: 30 sequence records and 4 failed-command
correction records. The sequence portion preserves progressive next-command
targets through the successful `/app/model.bin` write; the correction portion
covers three fastText import tracebacks and one active-adapter here-document
fastText syntax failure. The trainer payload uses
`tb-parametric-memory-train-fasttext-sequence-plus-tbexec-failure-r8-s140`,
`Qwen/Qwen3.5-9B`, LoRA rank 8, alpha 16, `max_length=4096`, and 140 steps.
This is the preferred next training candidate once a safe GPU is available,
because it combines recipe imitation with explicit failed-`tb_exec` repair.

The mixed candidate was then trained on GPU 7 at
`/tmp/tb21-task-local-parametric-trainfasttext-mixed-20260708/train-sequence-plus-tbexec-correction-r8-s140`
by running the `parametric_memory_lora_sft` worker directly against the dry-run
job payload. The resulting adapter directory is
`/tmp/tb21-task-local-parametric-trainfasttext-mixed-20260708/train-sequence-plus-tbexec-correction-r8-s140/artifacts/workers/job-tb-parametric-memory-train-fasttext-sequence-plus-tbexec-failure-r8-s140/parametric_memory_lora_sft/adapter`.
Trainer diagnostics record 34 training records, 140 trained steps,
`cuda_visible_devices=7`, LoRA rank 8, alpha 16, `max_length=4096`, and loss
moving from `1.1006397008895874` to `0.08282413333654404`. The local direct
worker artifact has no store-assigned artifact id, but its manifest records the
adapter id `tb-parametric-memory-train-fasttext-sequence-plus-tbexec-failure-r8-s140`,
base model `Qwen/Qwen3.5-9B`, and `training_record_count=34`.

Three paired `train-fasttext` evals were run on GPU 7 with
`--adapter-key-rewrite qwen3_5_vllm_language_model` and only
`parametric_memory` enabled. The first run at
`/tmp/tb21-task-local-parametric-trainfasttext-mixed-20260708/eval-sequence-plus-tbexec-correction-r8-s140`
used `max_output_tokens=1536`, `context_reserve_tokens=1536`, and
`tool_result_prompt_max_chars=2048`; it completed with baseline `0/1`,
parametric memory `0/1`, and delta `0`, but both conditions hit vLLM context
overflow retries. The baseline log contained 3,761 maximum-context errors and
the treatment log contained 2,459, so this run is a context-budget diagnostic,
not clean method evidence.

A second run at
`/tmp/tb21-task-local-parametric-trainfasttext-mixed-20260708/eval-sequence-plus-tbexec-correction-r8-s140-tight512`
used `max_output_tokens=512`, `context_reserve_tokens=512`, and
`tool_result_prompt_max_chars=512`. It removed context errors and again scored
baseline `0/1`, parametric memory `0/1`, delta `0`, but the small output cap
caused early malformed tool JSON in the baseline and treatment. The useful
clean comparison is therefore the third run at
`/tmp/tb21-task-local-parametric-trainfasttext-mixed-20260708/eval-sequence-plus-tbexec-correction-r8-s140-tight1024-tool512`,
with `max_output_tokens=1024`, `context_reserve_tokens=1024`, and
`tool_result_prompt_max_chars=512`. That run had zero vLLM context errors in
both conditions, baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`, and
delta `0`. The baseline failed by exhausting the tool budget after 30
`tb_exec` feedback records without producing `/app/model.bin`; the treatment
loaded the LoRA through vLLM, but after `tb_read_task` its first `tb_exec`
arguments were captured as malformed `_raw_arguments` with an empty
`timeout_seconds` and no `task_id`. This makes the current mixed
`train-fasttext` adapter an active but negative result: serving and key rewrite
work, but task-local parametric memory still needs stronger tool-call schema
shaping before it can be treated as a reliable backend.

The follow-up framework change adds `--include-tool-schema-lock` to the
task-local parametric-memory dataset builder. This keeps the existing sequence
and correction records intact while adding a short after-`tb_read_task` example
that targets a complete `tb_exec` argument object; it is intended for the
malformed `_raw_arguments` failure mode above, not as a replacement for
task-specific recipe records.

The schema-lock train-fasttext follow-up used the same local Qwen3.5 setup and
GPU 7. The dry projection at
`/tmp/tb21-task-local-parametric-trainfasttext-schemalock-20260708/dryrun-sequence-schema-lock-max100`
exported 69 records: 66 sequence records and 3 `tool_schema_lock` records. A
mixed training payload at
`/tmp/tb21-task-local-parametric-trainfasttext-schemalock-20260708/train-sequence-schema-lock-plus-tbexec-correction-r8-s180`
added 4 existing `tb_exec_failure` correction records, for 73 records total.
Training used `Qwen/Qwen3.5-9B`, LoRA `r=8`, alpha `16`, `max_length=4096`,
and 180 steps on `CUDA_VISIBLE_DEVICES=7`. The adapter was written to
`/tmp/tb21-task-local-parametric-trainfasttext-schemalock-20260708/train-sequence-schema-lock-plus-tbexec-correction-r8-s180/artifacts/workers/job-tb-parametric-memory-train-fasttext-sequence-schema-lock-tbexec-failure-r8-s180/parametric_memory_lora_sft/adapter`;
trainer diagnostics recorded loss moving from `1.1006397008895874` to
`0.05279954895377159`.

The paired eval at
`/tmp/tb21-task-local-parametric-trainfasttext-schemalock-20260708/eval-sequence-schema-lock-tbexec-r8-s180-tight1024-tool512`
used the previous clean caps: `max_output_tokens=1024`,
`context_reserve_tokens=1024`, `tool_result_prompt_max_chars=512`,
`--adapter-key-rewrite qwen3_5_vllm_language_model`, and managed vLLM on GPU 7.
The Harbor attempts completed, but the CLI runner did not write its normal
summary because the parametric job-level result stayed running during eval
teardown; `manual-summary.json` records the recovered result. Baseline pass@1
was `0/1`; parametric-memory pass@1 was also `0/1`, so delta remained `0`.
There were no maximum-context errors, no `_raw_arguments` failures, and no
`task_id` tool-rejection errors in either condition. The treatment's first
`tb_exec` was schema-clean and used `task_id=terminal-bench-task` with
`timeout_seconds=300`; the remaining failure mode shifted to budget exhaustion
after 31 schema-valid tool calls without producing `/app/model.bin`.

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

Direct vLLM probes against the exact live post-`tb_read_task` prefix then found
that this second empty-response layer was not a LoRA training failure. With the
path-bound adapter loaded, `temperature=0.0` produced the expected
`/app/out.txt` write command at 512, 1024, 2048, and 4096 output-token caps,
while requests that left temperature unset were unstable and sometimes returned
no tool call. The local Terminal-Bench/EvoLab package therefore needed to pass
its configured solver temperature through the OpenAI-compatible chat-completion
runtime, not just record it at the benchmark layer.

After that package-level temperature fix, the rerun at
`/tmp/tb21-task-local-parametric-gcode-pathbound-guard-20260707/eval-gcode-qwen35-pathbound-repair-tempfix-r8-s140`
still scored baseline `0/1`, parametric memory `0/1`, delta `0`, but the
treatment now emitted `tb_exec` after `tb_read_task`. Its first command had the
right content and `/app/out.txt` intent, but wrapped the write in an outer
`bash -lc` form whose `$out` and `$(cat /app/out.txt)` fragments were expanded
by the wrong shell layer before the inner command ran. This failed before
creating the required artifact, so the remaining issue was command
normalization at the execution boundary.

The final guard-backed rerun at
`/tmp/tb21-task-local-parametric-gcode-pathbound-guard-20260707/eval-gcode-qwen35-pathbound-repair-tempprintf-r8-s140`
used the same path-bound Qwen3.5-9B adapter, managed vLLM on GPU 6,
`--solver-temperature 0.0`, `--vllm-generation-config vllm`,
`--artifact-path-guard repair`, and `--required-artifact-path /app/out.txt`.
The summary recorded `enabled_artifacts=["parametric_memory"]` and disabled
`text_memory`, `skill_bundle`, and `agent_system`. Baseline pass@1/pass@k was
`0/1`; parametric-memory pass@1/pass@k was `1/1`; delta was `+1`.
The treatment trajectory read `terminal-bench-task`, generated the path-bound
`printf` write, and the guard repaired only that supported fragile printf form
to `printf '%s' ... > /app/out.txt` before execution. The official verifier
then returned reward `1.0`. Treat this as positive one-task controlled evidence
for the current parametric-memory path plus a shared artifact-path repair
guard, not as full Terminal Bench 2.1 performance evidence.

A follow-up task-local pass@5 run used the same adapter and guard, but increased
`--context-window-tokens` to `32768` so long baseline attempts would not mix
16k context-boundary 400s into the comparison. The run at
`/tmp/tb21-task-local-parametric-gcode-pathbound-guard-20260707/eval-gcode-qwen35-pathbound-repair-tempprintf-pass5-32k-r8-s140`
used `--n-attempts 5`, `--solver-temperature 0.0`,
`--vllm-generation-config vllm`, `--artifact-path-guard repair`, and
`--required-artifact-path /app/out.txt`. Baseline had five verifier rewards of
`0.0`, so pass@1/pass@5 was `0/1`. Parametric memory had rewards
`[1.0, 1.0, 0.0, 1.0, 1.0]`, so pass@1/pass@5 was `1/1` and mean reward was
`0.8`. The summary again recorded only `parametric_memory` enabled, with
`text_memory`, `skill_bundle`, and `agent_system` disabled. The passing
treatment attempts include six `normalize_required_artifact_printf_write`
repair events across the tool traces. The single treatment failure shows the
remaining stability gap: the adapter sometimes stops after `tb_read_task` or
later drifts into malformed tool-call JSON, so this is strong single-task
positive evidence for pass@5 under the guarded local setup, not a solved
general parametric-memory method.

A stricter correction-aware follow-up trained from local Harbor/Qwen failures
instead of only Codex command transcripts. The pool at
`/tmp/tb21-task-local-parametric-gcode-local-correction-20260707/local_correction_pool.jsonl`
combined five failed local baseline attempts from the pass@5 run above with one
successful Codex reference row. The dry-run at
`/tmp/tb21-task-local-parametric-gcode-local-correction-20260707/dryrun`
exported 12 task-local records: five base live-replay prefixes, five
`tb_run_tests` correction prefixes, and two `tb_collect_result` correction
prefixes. All records were filtered to the required final artifact path
`/app/out.txt`; the command literal is intentionally not reproduced in this
dev note.

Training at
`/tmp/tb21-task-local-parametric-gcode-local-correction-20260707/train-gcode-qwen35-local-correction-r8-s120`
used Qwen3.5-9B on GPU 6 with LoRA rank 8, alpha 16, max length 4096, and
120 SFT steps. The registered artifact was
`tb-parametric-memory-gcode-local-correction-r8-s120` with
`training_record_count=12`; trainer diagnostics recorded losses moving from
about `1.24`, `1.33`, `1.29` at the start to roughly `6.5e-05` at the end. The
paired local eval at
`/tmp/tb21-task-local-parametric-gcode-local-correction-20260707/eval-gcode-qwen35-local-correction-r8-s120`
used the clean Terminal-Bench/EvoLab package PR #34, managed vLLM on GPU 6,
deterministic decoding, `--vllm-generation-config vllm`,
`--artifact-path-guard repair`, and `--required-artifact-path /app/out.txt`.
Only `parametric_memory` was enabled; `text_memory`, `skill_bundle`, and
`agent_system` were disabled. The official verifier summary scored baseline
pass@1/pass@k `0/1`, parametric-memory pass@1/pass@k `1/1`, delta `+1`, with
no Harbor errors. The treatment server command included LoRA serving while the
baseline server did not, and GPU 6 was released after the run.

This is better method evidence than the earlier Codex-only SFT variants because
the supervised prefixes include real local failed model calls and correction
points. It still has an important caveat: the treatment's final official reward
was `1.0`, but the latest internal visible `tb_run_tests` artifact remained
failed with exit code 127 because the task package had no visible test
entrypoint. The treatment executed 10 `tb_exec` calls, and the shared guard
repaired five supported fragile printf writes with
`normalize_required_artifact_printf_write`; baseline executed 14 `tb_exec`
calls with no repairs and official reward `0.0`. Treat this as positive
single-task official-verifier evidence for correction-aware local parametric
memory plus the same execution guard, not as a clean finish-policy result or a
Terminal Bench 2.1 aggregate claim.

A current-runner pass@3 reproduction on GPU 7 at
`/tmp/tb21-task-local-parametric-gcode-local-correction-20260708/pass3-current-runner-gpu7`
used the same correction-aware adapter, `Qwen/Qwen3.5-9B`, deterministic
decoding, `--vllm-generation-config vllm`, 32k serving context,
`max_output_tokens=1024`, `context_reserve_tokens=1024`,
`tool_result_prompt_max_chars=1024`, `--adapter-key-rewrite
qwen3_5_vllm_language_model`, `--artifact-path-guard repair`, and
`--required-artifact-path /app/out.txt`. The summary recorded only
`parametric_memory` enabled, with `text_memory`, `skill_bundle`, and
`agent_system` disabled. Baseline rewards were `[0.0, 0.0, 0.0]`; parametric
memory rewards were `[1.0, 1.0, 1.0]`. Thus baseline pass@1/pass@3 was `0/1`,
parametric-memory pass@1/pass@3 was `1/1`, mean reward improved from `0.0` to
`1.0`, and delta pass@1/pass@3 was `+1`. The treatment server command included
`--enable-lora` and loaded
`tb-parametric-memory-gcode-local-correction-r8-s120`; the summary recorded 64
rewritten LoRA keys. The shared artifact-path guard recorded seven treatment
repairs with `normalize_required_artifact_printf_write`. The treatment trials
are official-verifier positive but still not perfectly clean finish-policy
traces: later tool calls in two attempts failed after the required artifact had
already been written, while the official verifier passed because `/app/out.txt`
was present and correct. Baseline runtime was dominated by slow verifier apt
downloads, so use the reward/pass metrics rather than wall-clock as the
controlled comparison signal.

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

A current-runner pass@3 reproduction on GPU 7 at
`/tmp/tb21-parametric-memory-qwen35-password-staticnode-shaped-v2-rerun-20260708/pass3-current-runner-gpu7`
used the same shaped-v2 adapter, `Qwen/Qwen3.5-9B`, deterministic decoding,
`--adapter-key-rewrite qwen3_5_vllm_language_model`,
`max_output_tokens=512`, `context_reserve_tokens=512`, and
`tool_result_prompt_max_chars=512`. The summary again recorded only
`parametric_memory` enabled, with `text_memory`, `skill_bundle`, and
`agent_system` disabled. Baseline rewards were `[0.0, 0.0, 0.0]`; parametric
memory rewards were `[1.0, 1.0, 0.0]`. Thus baseline pass@1/pass@3 was `0/1`,
parametric-memory pass@1/pass@3 was `1/1`, mean reward improved from `0.0` to
`0.667`, and delta pass@1/pass@3 was `+1`. There were no Harbor exceptions in
either condition, the treatment server loaded the rewritten LoRA with 64
rewritten keys, and the passing treatment attempts emitted the intended
`find varsea/disks` recovery command after `tb_read_task`. The one treatment
failure still emitted the same command family but drifted over repeated
attempts, so this is stronger single-task evidence for a real local
parametric-memory gain, not a solved aggregate Terminal Bench 2.1 result.

A third task-local Qwen3.5 probe on `vulnerable-secret` exposed a useful
framework boundary and a negative method result. The pool had one failed
baseline trajectory and many successful Codex trajectories, but the successful
ones wrote `/app/results.txt` through Codex `file_change` events rather than
shell `command_execution` events. A normal `--command-contains results.txt`
dry-run therefore selected a read-back validation command instead of a command
that creates the required file. The task-local builder now supports
`--target-command` for this case. The first manual-target adapter at
`/tmp/tb21-task-local-parametric-vulnerable-secret-20260708/train-manual-target-r8-s80`
trained two records, the explicit `printf 'FLAG{...}\n' > /app/results.txt`
target plus a schema-lock record, using Qwen3.5-9B on GPU 7 with LoRA rank 8,
alpha 16, `max_length=4096`, and 80 steps. The pass@3 eval at
`/tmp/tb21-task-local-parametric-vulnerable-secret-20260708/eval-manual-target-r8-s80-pass3`
kept only `parametric_memory` enabled, used deterministic Qwen3.5 local vLLM
with `qwen3_5_vllm_language_model` key rewrite, and scored baseline `[0.0,
0.0, 0.0]`, parametric memory `[0.0, 0.0, 0.0]`, delta `0`. The treatment
loaded the adapter and often tried to write `/app/results.txt` immediately, but
the command drifted into nested `printf` quoting errors or wrote lowercase
`flag{...}`, so the verifier failed exact `FLAG{...}` checks.

A second `vulnerable-secret` manual-target variant tested whether avoiding the
literal `FLAG` token would stabilize exact output. It trained
`tb-parametric-memory-vulnerable-secret-manual-hex-r8-s100` at
`/tmp/tb21-task-local-parametric-vulnerable-secret-20260708/train-manual-hex-r8-s100`
with a Python one-liner target that writes `bytes.fromhex(...)` to
`/app/results.txt`; diagnostics recorded two records, 100 steps, and final loss
around `2e-5`. The pass@1 eval at
`/tmp/tb21-task-local-parametric-vulnerable-secret-20260708/eval-manual-hex-r8-s100-pass1`
again scored baseline `0/1`, parametric memory `0/1`, delta `0`. This time the
treatment did not emit the supervised hex write at all: it first tried to read
`/app/vulnerable/make_syms.sh`, then produced a malformed `tb_exec` without a
valid `task_id`. The method conclusion is that manual target injection is now
available and active, but two direct-solver records are not enough to make
Qwen3.5 reliably copy exact literal/file-write commands.

The follow-up `vulnerable-secret` literal-shaping run added task-local target
repeats and schema-lock repeats. The first repeated direct-solver run trained
`tb-parametric-memory-vulnerable-secret-repeat16-r8-s120` at
`/tmp/tb21-task-local-parametric-vulnerable-secret-repeat-20260708/train-repeat16-schemalock-r8-s120`
from 16 repeated manual target records plus one schema-lock record. Its pass@1
eval at
`/tmp/tb21-task-local-parametric-vulnerable-secret-repeat-20260708/eval-repeat16-schemalock-r8-s120-pass1`
kept the adapter active and rewrote 64 LoRA keys, but scored baseline `0/1`,
parametric memory `0/1`, delta `0`: the treatment wrote `/app/results.txt`
with unrelated Chinese text instead of the `FLAG{...}` literal. A second run
used a fresh local Qwen3.5 failed baseline trajectory as the `live_replay`
prefix and trained
`tb-parametric-memory-vulnerable-secret-live-repeat16-r8-s120` at
`/tmp/tb21-task-local-parametric-vulnerable-secret-repeat-20260708/train-live-repeat16-schemalock-r8-s120`.
Its pass@1 eval at
`/tmp/tb21-task-local-parametric-vulnerable-secret-repeat-20260708/eval-live-repeat16-schemalock-r8-s120-pass1`
again scored `0/1` vs `0/1`; this time the adapter copied the exact
`FLAG{...}` command but emitted incomplete `tb_exec` arguments missing
`task_id`, so the tool call failed.

The successful `vulnerable-secret` variant balanced the two failure modes by
training 32 records: 16 `live_replay` literal targets from the failed local
prefix plus 16 direct-solver schema-lock records. Training
`tb-parametric-memory-vulnerable-secret-live-repeat16-schema16-r8-s120` at
`/tmp/tb21-task-local-parametric-vulnerable-secret-repeat-20260708/train-live-repeat16-schema16-r8-s120`
used Qwen3.5-9B on GPU 7, LoRA rank 8, alpha 16, `max_length=4096`, 120 steps,
and final losses around `2.7e-5`. The paired pass@1 eval at
`/tmp/tb21-task-local-parametric-vulnerable-secret-repeat-20260708/eval-live-repeat16-schema16-r8-s120-pass1`
kept only `parametric_memory` enabled, disabled `text_memory`, `skill_bundle`,
and `agent_system`, loaded the adapter with `qwen3_5_vllm_language_model`, and
rewrote 64 LoRA keys. Baseline pass@1/pass@k was `0/1`; parametric-memory
pass@1/pass@k was `1/1`, delta `+1`. The treatment emitted the intended
complete `tb_exec` call:
`printf 'FLAG{b4ff3r_0v3rfl0w_m4st3r_k3y_2024}\n' > /app/results.txt` with
`task_id="terminal-bench-task"` and `timeout_seconds=30`, then ran
`tb_run_tests`, and the verifier rewarded `1.0`.

A pass@3 reproduction on GPU 7 at
`/tmp/tb21-task-local-parametric-vulnerable-secret-repeat-20260708/eval-live-repeat16-schema16-r8-s120-pass3`
used the same adapter, local Qwen3.5-9B serving setup, and
`artifact_path_guard=off`. `summary.json` recorded baseline rewards
`[0.0, 0.0, 0.0]` and
parametric-memory rewards `[1.0, 1.0, 1.0]`, so baseline pass@1/pass@3 was
`0/1`, parametric-memory pass@1/pass@3 was `1/1`, mean reward improved from
`0.0` to `1.0`, and delta pass@1/pass@3 was `+1`. All three treatment trials
emitted the exact `tb_exec` command above with complete `task_id` and
`timeout_seconds` arguments. The first treatment trial continued issuing
repeated write/test/report calls after the first successful report, but Harbor
still scored the final trial reward as `1.0`; this is a useful stability signal
for future finish-boundary training rather than a failure of the task-local
adapter.

Across the three current-runner Qwen3.5 controlled pass@3 reproductions above,
all three tasks kept only `parametric_memory` enabled and disabled `text_memory`,
`skill_bundle`, and `agent_system`. `gcode-to-text` moved from baseline
`[0.0, 0.0, 0.0]` to parametric `[1.0, 1.0, 1.0]`, so mean reward delta was
`+1.0` and pass@1/pass@3 delta was `+1`. `password-recovery` moved from
baseline `[0.0, 0.0, 0.0]` to parametric `[1.0, 1.0, 0.0]`, so mean reward
delta was about `+0.667` and pass@1/pass@3 delta was `+1`. `vulnerable-secret`
moved from baseline `[0.0, 0.0, 0.0]` to parametric `[1.0, 1.0, 1.0]`, so mean
reward delta was `+1.0` and pass@1/pass@3 delta was `+1`. This is the current
best controlled evidence that local parametric memory can improve selected
Terminal Bench 2.1 tasks under fixed non-memory evolution variables. It is not
yet a full-benchmark gain estimate; the negative `train-fasttext` probes below
show that task-local parametric memory still needs better method design and
runtime control before aggregate claims are justified.

Two train-fasttext task-local Qwen3.5-9B milestone-sequence probes on 2026-07-08
did not improve pass@1, but they clarified the next framework requirement. The
first adapter was trained at
`/tmp/tb21-task-local-parametric-trainfasttext-milestone-20260708/train-milestone-schema-lock-tbexec-r8-s120`
from 25 records: 3 schema-lock records, 18 milestone sequence records, and 4
`tb_exec` failure records. Its paired/manual eval at
`/tmp/tb21-task-local-parametric-trainfasttext-milestone-20260708/eval-milestone-r8-s120-tight1024-tool512`
had baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`, and delta `0`.
The treatment emitted schema-valid `tb_exec` calls with `task_id` and
`timeout_seconds`, reached `apt-get update`, but then timed out on
`apt-get install -y g++` at 300 seconds; the timeout left a `dpkg` lock and the
following retry failed before a later malformed `_raw_arguments` tool call
terminated the attempt.

The follow-up long-timeout adapter was trained at
`/tmp/tb21-task-local-parametric-trainfasttext-milestone-longtimeout-20260708/train-milestone-longtimeout-schema-lock-tbexec-r8-s120`
with the same 25 records but with long-running apt, pip, data-conversion, and
training targets supervised at 900 seconds. Its manual eval at
`/tmp/tb21-task-local-parametric-trainfasttext-milestone-longtimeout-20260708/eval-milestone-longtimeout-r8-s120-tight1024-tool512`
again had baseline pass@1 `0/1`, parametric-memory pass@1 `0/1`, and delta
`0`. The schema issue was gone: 4 LLM calls, no `_raw_arguments`, and no
missing `task_id`. The treatment completed the dependency check,
`apt-get update`, and `apt-get install -y g++`; however the install took 803.6
seconds, so the Harbor agent round budget was exhausted immediately afterward
before `pip install fasttext`, data conversion, model training, or verifier
execution. This indicates the current train-fasttext failure is dominated by
environment setup latency and rollout budget, not by adapter selection or basic
tool schema alignment. A production milestone backend should either avoid
repeated slow apt setup, increase the relevant Harbor/agent budget for local
parametric probes, or train a single long command that performs setup and model
creation within one tool call whose timeout matches the task.

A follow-up local probe used the same long-timeout adapter with the newly
exposed Harbor `--agent-timeout-multiplier 3.5` flag. The full
baseline-plus-adapter run at
`/tmp/tb21-task-local-parametric-trainfasttext-milestone-longtimeout-20260708/eval-milestone-longtimeout-r8-s120-agentx35-tight1024-tool512`
confirmed the Harbor config recorded `agent_timeout_multiplier=3.5`; the
baseline still failed (`0/1`) after exploring unrelated commands and was
interrupted during verifier apt setup to avoid spending the run on a repeated
failed-baseline verifier install. A treatment-only diagnostic run at
`/tmp/tb21-task-local-parametric-trainfasttext-milestone-longtimeout-20260708/eval-milestone-longtimeout-r8-s120-agentx35-treatment-only-tight1024-tool512`
served the rewritten adapter successfully (`rewritten_key_count=64`) and
returned reward `0.0` (`0/1`). The treatment was schema-clean and followed the
intended path through dependency check and `apt-get update`, but
`apt-get install -y g++` timed out after its own `900` second `tb_exec`
timeout. The trajectory also showed EvoLab's internal
`max_subagent_runtime_seconds` remained `900` even though Harbor's outer
agent timeout multiplier was set. Therefore the next framework requirement for
slow Terminal-Bench tasks is not only Harbor timeout passthrough; the task
package also needs a controllable direct-solver subagent runtime budget and a
larger `EVOLAB_TB_EXEC_TIMEOUT_CAP_SECONDS`, or the method needs to avoid
network apt setup entirely.

A treatment-only GPU 7 rerun at
`/tmp/tb21-task-local-parametric-trainfasttext-milestone-longtimeout-20260708/eval-internalbudget-treatment-only-gpu7-20260708`
used the same adapter, `Qwen/Qwen3.5-9B`,
`EVOLAB_TB_EXEC_TIMEOUT_CAP_SECONDS=1800`, and a local task-package patch that
reads `EVOLAB_TB_MAX_SUBAGENT_RUNTIME_SECONDS=2400`. It returned reward `0.0`
(`0/1`) after a 25m57s Harbor run, with `rewritten_key_count=64`. This
confirmed the internal subagent runtime budget was no longer the first
blocker: the EvoLab round ran for 1537.1 seconds rather than stopping at 900.
However, the agent still requested `timeout_seconds=900` on
`apt-get install -y g++`, so the command timed out after 900.1 seconds even
though the cap was 1800. `EVOLAB_TB_EXEC_TIMEOUT_CAP_SECONDS` is therefore only
an upper bound, not a way to raise explicit model-requested timeouts. The run
then continued through several short recovery/test/report calls but finally
failed on another malformed `_raw_arguments` `tb_exec` call. The next
parametric-memory method requirement for `train-fasttext` is to train or prompt
long setup commands to request a higher timeout, add a task-package supported
minimum/default timeout override, or avoid network apt setup; raising only the
cap is not sufficient.

A follow-up treatment-only GPU 7 run at
`/tmp/tb21-task-local-parametric-trainfasttext-milestone-longtimeout-20260708/eval-timeoutfloor-treatment-only-gpu7-20260708`
used the same adapter with `EVOLAB_TB_EXEC_TIMEOUT_CAP_SECONDS=1800`,
`EVOLAB_TB_EXEC_TIMEOUT_MIN_SECONDS=1800`, and
`EVOLAB_TB_MAX_SUBAGENT_RUNTIME_SECONDS=2400` in the local task package. It
again returned reward `0.0` (`0/1`) after 41m03s, with
`rewritten_key_count=64`, but it verified the timeout-floor path: `apt-get
update` requested 900 seconds and ran with an effective 1800 second timeout;
`apt-get install -y g++` also requested 900 seconds and timed out only after
1800.1 seconds. The run then continued through short recovery/test calls but
failed when the subagent budget was exceeded after 2443.7 seconds and 21 tool
calls; later generated Python snippets still contained invalid fragments such
as `swapNode@params`. At this point OpenEvo can expose the needed timeout
plumbing for compatible task packages, while the remaining `train-fasttext`
parametric-memory work is method-level: avoid slow network apt setup, provide a
cached task image or mirror, and train a cleaner long-command solve policy.

A single-command follow-up isolated that method variable on GPU 7. The dry
projection at
`/tmp/tb21-task-local-parametric-trainfasttext-singlecommand-20260708/dryrun-long-command`
used the two failed long-timeout treatment traces above and a staged corrective
projection with three stages: `read_task`, a repeated
`single_long_exec_after_read` target, and a repeated
`single_long_exec_after_failures` target. Training at
`/tmp/tb21-task-local-parametric-trainfasttext-singlecommand-20260708/train-single-long-command-r8-s120-v2`
used `Qwen/Qwen3.5-9B`, LoRA rank 8, alpha 16, `max_length=4096`, and 120
steps on `CUDA_VISIBLE_DEVICES=7`. It produced artifact
`art_8485054684174de1` with 76 SFT records and an adapter at
`artifacts/workers/job_48c9914abf464311/parametric_memory_lora_sft/adapter`;
diagnostics recorded loss falling from about `0.84` to `2.15e-4`.

The treatment-only eval at
`/tmp/tb21-task-local-parametric-trainfasttext-singlecommand-20260708/eval-single-long-command-r8-s120-v2-treatment-only-gpu7`
loaded the adapter through managed vLLM on GPU 7 with
`qwen3_5_vllm_language_model` key rewrite, enabled only
`parametric_memory`, and used the local task-package timeout floor/cap of 3600
seconds. The first live `tb_exec` did request the intended long setup/train
command with `timeout_seconds=3600`, proving the adapter can shape the policy
toward a long single command. The run still scored `0/1`: the generated Python
heredoc turned escaped newline string literals into literal line breaks inside
`str.replace(...)` and `f.write(...)`, causing `SyntaxError`; follow-up retries
then drifted into deleting `/tmp/fastText` before running `make`.

A no-escape retrain removed that Python string-literal failure from the target
command by using `splitlines()` and `print(..., file=f)` instead of embedded
newline escapes. Training at
`/tmp/tb21-task-local-parametric-trainfasttext-singlecommand-20260708/train-single-long-command-r8-s120-v3-noescape`
again exported 76 records, trained 120 steps on GPU 7, and produced artifact
`art_11ac3dd17a2647fe`; diagnostics recorded loss falling from about `0.84` to
`9.3e-5`. Two treatment-only evals at
`/tmp/tb21-task-local-parametric-trainfasttext-singlecommand-20260708/eval-single-long-command-r8-s120-v3-noescape-treatment-only-gpu7`
and
`/tmp/tb21-task-local-parametric-trainfasttext-singlecommand-20260708/eval-single-long-command-r8-s120-v3-noescape-treatment-only-gpu7-output3072`
both scored `0/1`. In both runs the first `tb_exec` was rejected before shell
execution because Qwen3 XML tool parsing captured malformed `_raw_arguments`
ending with `</tool_response>` instead of a complete JSON object containing
`task_id`, `command`, and `timeout_seconds`. Raising the effective output cap
from 1536 to 3072 tokens did not change the malformed arguments, so this is not
an output-budget truncation issue. The resulting method conclusion is that a
single very long `tb_exec` target is too brittle for this Qwen3 tool-call
surface: v2 preserved tool schema but failed Python escaping, while v3 fixed the
shell/Python target and broke tool-call closure. The next `train-fasttext`
backend candidate should prefer shorter staged targets plus explicit tool-schema
lock/correction records over another single-command adapter.

A key-sequence follow-up tested that shorter staged direction with a full
3600-second timeout target. The dry run at
`/tmp/tb21-task-local-parametric-trainfasttext-sequence3600-20260708/dryrun-keysequence3600-schema8`
exported 48 SFT records: 23 live-replay sequence records selected from the
successful Codex trajectory's key commands and 25 direct solver tool-schema-lock
records. The sequence targets started with `apt-get update`, `apt-get install
-y g++`, `python -m pip install fasttext`, data conversion, intermediate
tuning, final normalized training, and final raw training with
`final_model_size_bytes`; all sequence targets supervised
`timeout_seconds=3600`. Training at
`/tmp/tb21-task-local-parametric-trainfasttext-sequence3600-20260708/train-keysequence3600-schema8-r8-s140`
used `Qwen/Qwen3.5-9B`, LoRA rank 8, alpha 16, `max_length=4096`, and 140
steps on GPU 7. It registered adapter
`tb-parametric-memory-train-fasttext-keysequence3600-schema8-r8-s140`;
diagnostics recorded 48 records, 140 trained steps, and a final loss tail around
`4.5e-6`.

The paired eval at
`/tmp/tb21-task-local-parametric-trainfasttext-sequence3600-20260708/eval-keysequence3600-schema8-r8-s140-pass1`
completed cleanly with only `parametric_memory` enabled in the treatment.
Baseline pass@1/pass@k was `0/1`, parametric-memory pass@1/pass@k was `0/1`,
and delta was `0`. The adapter was served through managed vLLM on GPU 7 with
the `qwen3_5_vllm_language_model` key rewrite. The treatment produced 26
`tb_exec` calls; 25 carried `timeout_seconds=3600`, so the timeout supervision
mostly held. One early malformed call missed the timeout and contained an
unfinished `pd.read_parquet("data/train-00...` command followed by `</think>`,
but the run recovered into valid tool calls. It installed dependencies and
eventually issued two fastText training commands that saved relative
`model.bin`, but it never emitted the intended `/app/model.bin` plus
`final_model_size_bytes` target and ended with `subagent budget_exceeded` after
27 tool calls and no artifact. This makes the next method requirement more
specific: staged targets are less brittle than one very long command, but the
dataset still needs stronger ordering/path anchoring and final-answer boundary
supervision for the official `/app/model.bin` artifact.

Two follow-ups tested that diagnosis without changing framework code. The first
was a final-path weighted resampling at
`/tmp/tb21-task-local-parametric-trainfasttext-finalweighted-20260708/train-finalweighted-r8-s220`.
It reused the key-sequence dry run, produced 188 SFT records, kept 16
schema-lock records, and upweighted the two `/app/model.bin` /
`final_model_size_bytes` sequence targets to 112 records. Training used
`Qwen/Qwen3.5-9B`, LoRA rank 8, alpha 16, `max_length=4096`, 220 steps, and
GPU 7; diagnostics recorded `record_count=188`, `trained_steps=220`, and a
loss tail around `1e-4`. The paired eval at
`/tmp/tb21-task-local-parametric-trainfasttext-finalweighted-20260708/eval-finalweighted-r8-s220-pass1`
again scored baseline `0/1`, parametric memory `0/1`, delta `0`. The treatment
kept most tool calls schema-valid (`6/7` `tb_exec` calls carried
`timeout_seconds=3600`) but never emitted `/app/model.bin` or
`final_model_size_bytes`. It ran `apt-get update`, then skipped
`apt-get install -y g++`, attempted `python -m pip install fasttext`, failed on
the C++17 compiler requirement, drifted into `python -m pip install g++`, and
ended with malformed `_raw_arguments`. This shows final-path weighting alone
does not fix dependency-order recovery.

The second follow-up added active failure correction records from that failed
final-weighted trajectory. Training at
`/tmp/tb21-task-local-parametric-trainfasttext-activecorrection-20260708/train-activecorr-r8-s180`
combined weighted key-sequence records with 80 live-prefix correction records
from LLM calls containing `Unsupported compiler`, `Invalid requirement`,
`g++: command not found`, or `installagarbage`. The supervised correction target
was `apt-get install -y g++ && python -m pip install fasttext`. The run produced
243 SFT records, including 72 `/app/model.bin` final-path targets and 16
schema-lock records, then trained `Qwen/Qwen3.5-9B` for 180 steps on GPU 7. The
paired eval at
`/tmp/tb21-task-local-parametric-trainfasttext-activecorrection-20260708/eval-activecorr-r8-s180-pass1`
also scored baseline `0/1`, parametric memory `0/1`, delta `0`, but it changed
the failure mode. The treatment emitted 13 `tb_exec` calls; 12 carried
`timeout_seconds=3600`, 10 contained `/app/model.bin`, and 2 contained
`apt-get install -y g++`. It did recover toward installing `g++`, but generated
broken Python snippets: repeated keyword arguments, missing `fasttext`, invalid
`from fasttext.train_supervised import ...`, undefined `test_df`, and a
`subprocess` use before import. This narrows the next method requirement: for
`train-fasttext`, active correction must supervise the full runnable Python
program and fastText API shape, not only dependency recovery and output-path
tokens. The likely next backend candidate should use compact script-file targets
or a prevalidated command template with failure-state corrections, rather than
asking the adapter to synthesize large Python programs from sparse sequence
examples.

The compact-template follow-up tested exactly that method variable. A local
container validation first proved a concise fastText CLI recipe was sufficient:
install/build the upstream fastText CLI, convert Yelp parquet rows to
`__label__...` text files, then run `fasttext supervised` with word bigrams,
50 dimensions, 8 epochs, subword features, and a 500k bucket. The public
held-out check reached `P@1=0.626` / `R@1=0.626`, and the generated model was
`143211714` bytes, under the 150 MiB task limit. Training at
`/tmp/tb21-task-local-parametric-trainfasttext-clitemplate-20260708/train-clitemplate-r8-s220`
used `Qwen/Qwen3.5-9B`, GPU 7, LoRA rank 8, alpha 16, `max_length=4096`, and
220 SFT steps. The dataset contained 256 records: 128
`compact_cli_template_sequence`, 16 `compact_cli_template_direct_sequence`, 16
`run_tests_after_compact_cli_template`, 16 `tool_schema_lock`, and 80
`compact_cli_install_recovery` records. The supervised targets were 240
`tb_exec` calls and 16 `tb_run_tests` calls, and trainer diagnostics recorded
loss moving from about `0.90` to `8.6e-05`.

The paired eval at
`/tmp/tb21-task-local-parametric-trainfasttext-clitemplate-20260708/eval-clitemplate-r8-s220-pass1`
used managed Qwen3.5-9B vLLM on GPU 7 with
`--adapter-key-rewrite qwen3_5_vllm_language_model`, 32k context,
`max_output_tokens=3072`, `exec_timeout_min_seconds=3600`,
`exec_timeout_cap_seconds=3600`, and only `parametric_memory` enabled. The
summary recorded baseline pass@1/pass@k `0/1`, parametric-memory pass@1/pass@k
`0/1`, and delta `0`; GPU 7 was released after the managed server stopped. The
treatment did load the LoRA and shifted strongly toward the intended policy:
its first `tb_exec` installed `git build-essential`, requested
`timeout_seconds=3600`, and attempted to clone/build fastText. The run failed
because GitHub access inside the live task container repeatedly returned
`gnutls_handshake() failed: The TLS connection was non-properly terminated`.
Subsequent recovery attempts tried the same clone, wget/curl tarballs, a
spurious `fasthtml` path, malformed `rm -rf/...` fragments, and finally
`apt-get install -y rm -rf`; no `/app/model.bin` artifact was produced, and the
parametric trial failed after 13 `tb_exec` calls and a 1653.6 second agent
round.

This is another negative `train-fasttext` method result, but it is cleaner than
the earlier active-correction failure. The adapter learned the broad CLI
template, long timeout, and schema shape; the remaining failure is an
unreliable external GitHub dependency plus recovery drift after TLS failure.
The next candidate should avoid GitHub clone/tarball setup in the supervised
target, for example by using a prevalidated `build-essential`/PyPI `fasttext`
path or a cached local source archive, and should add explicit negative
correction records for `gnutls_handshake`, missing `curl`, and malformed
`rm -rf/` recovery commands before repeating paired eval.

The next dependency-source probe validated Debian's packaged fastText instead
of GitHub or PyPI. In the live `alexgshaw/train-fasttext:20251031` task image,
`apt-get update && apt-get install -y fasttext`, parquet-to-fastText text
conversion, and the same compact CLI training recipe produced public
`P@1=0.628` / `R@1=0.628`; `/app/model.bin` was `143211714` bytes. Training at
`/tmp/tb21-task-local-parametric-trainfasttext-aptpkg-20260709/train-aptpkg-r8-s220`
used `Qwen/Qwen3.5-9B`, GPU 7, LoRA rank 8, alpha 16, `max_length=4096`, and
220 SFT steps over 256 records. The records preserved the previous
compact-template shape but replaced all supervised targets with the apt
package path and added active recovery targets away from the failed GitHub/PyPI
setup.

The paired eval at
`/tmp/tb21-task-local-parametric-trainfasttext-aptpkg-20260709/eval-aptpkg-r8-s220-pass1`
still scored baseline `0/1`, parametric memory `0/1`, delta `0`. It was
nevertheless a useful method step: the treatment loaded the rewritten adapter
(`rewritten_key_count=64`) and immediately installed Debian `fasttext`, but
then generated a broken Python fastText API script, hit a syntax error, and
looped on repeated `apt-get install -y fasttext` calls instead of advancing to
the CLI data conversion and training command. This showed that the apt package
dependency was correct, while the SFT distribution over-weighted the install
action and under-supervised the post-install state.

The successful follow-up changed the method target to one-shot and
post-install CLI continuations. Training at
`/tmp/tb21-task-local-parametric-trainfasttext-oneshot-20260709/train-oneshot-r8-s260`
used the same Qwen3.5-9B LoRA setup, 256 records, and 260 SFT steps. The dataset
contained 160 first-action records whose target was a single `tb_exec` command
that installs Debian `fasttext`, converts the Yelp parquet files, trains, tests,
and prints model size, plus 96 correction records that map already-installed,
syntax-error, or repeated-apt states directly to the data-conversion/training
continuation. Local validation of the exact one-shot command reached public
`P@1=0.628` with the same `143211714` byte model.

The first one-shot eval at
`/tmp/tb21-task-local-parametric-trainfasttext-oneshot-20260709/eval-oneshot-r8-s260-pass1`
again reported official baseline `0/1`, parametric `0/1`, delta `0`, but the
failure was not task performance. The treatment produced `/app/model.bin` and a
manual in-container public check showed `P@1=0.624`, `R@1=0.624`, and model
size `143211714` bytes. The official verifier then failed because the run had
passed `--verifier-python-install-mirror /root/.cache/...`, which becomes an
invalid in-container uv Python download URL. This run also exposed a remaining
policy issue: the adapter kept issuing validation commands until tool budget
exhaustion instead of finalizing after a successful model artifact.

The corrected eval at
`/tmp/tb21-task-local-parametric-trainfasttext-oneshot-20260709/eval-oneshot-r8-s260-pass2-cacheenv`
set host-side `EVOLAB_TB_UV_CACHE_TARBALL` and
`EVOLAB_TB_UV_PYTHON_TARBALL` before launching the runner, allowing the Harbor
environment to upload uv and managed-Python caches into the verifier container.
It did not pass `--verifier-python-install-mirror`. This run completed with
baseline pass@1/pass@k `0/1`, parametric-memory pass@1/pass@k `1/1`, and delta
`+1` on `train-fasttext`, with only `parametric_memory` enabled and
`text_memory`, `skill_bundle`, and `agent_system` disabled. The serving adapter
was `tb-parametric-memory-train-fasttext-oneshot-r8-s260`, loaded through
managed Qwen3.5-9B vLLM on GPU 7 with
`--adapter-key-rewrite qwen3_5_vllm_language_model`; the summary recorded
`rewritten_key_count=64`.

The pass2 treatment still did not copy the supervised one-shot command exactly.
It tried several malformed Python/shell snippets, then installed Debian
`fasttext`, wrote `train.ft.txt` and `test.ft.txt`, and trained with fastText
CLI variants. The final official verifier passed both hidden checks; a manual
public check before verification showed `/app/model.bin` at 137 MiB with
`P@1=0.627`, and after an additional agent retraining command the final public
check was `P@1=0.622`. The method result is therefore a real controlled
parametric-memory gain for this single Terminal Bench 2.1 task, but not yet a
robust backend: the next iteration should supervise a stop/finalization boundary
after a public-passing model exists and reduce malformed shell/Python attempts
before the first successful CLI training command.

A state-machine follow-up attempted to preserve the one-shot gain while reducing
malformed commands and post-success loops. Training at
`/tmp/tb21-task-local-parametric-trainfasttext-statemachine-20260709/train-statemachine-r8-s280`
used Qwen3.5-9B on GPU 7, LoRA rank 8, alpha 16, `max_length=4096`, 336
records, and 280 SFT steps. The dataset mixed read-task, apt-install,
data-preparation, train/test, and final assistant-message stages; the worker
registered adapter
`/tmp/tb21-task-local-parametric-trainfasttext-statemachine-20260709/train-statemachine-r8-s280/artifacts/workers/job-tb-parametric-memory-train-fasttext-statemachine-r8-s280/parametric_memory_lora_sft/adapter`.

The paired eval at
`/tmp/tb21-task-local-parametric-trainfasttext-statemachine-20260709/eval-statemachine-r8-s280-pass2-cacheenv-preloaded`
used the same cache-env verifier setup as the successful one-shot pass and
preloaded `Qwen/Qwen3.5-9B` with `huggingface_hub.snapshot_download`; without
that preload, the managed vLLM server timed out after 600 seconds while
materializing model shards. The official summary recorded baseline
pass@1/pass@k `0/1`, parametric-memory pass@1/pass@k `0/1`, and delta `0`, with
only `parametric_memory` enabled and `text_memory`, `skill_bundle`, and
`agent_system` disabled. The adapter loaded successfully with 64 rewritten LoRA
keys, but the treatment made only one tool call (`tb_read_task`). Its second LLM
response produced no tool call, stopped, and contained repeated planning text in
the Qwen `reasoning` field, so no `tb_exec` command or `/app/model.bin` was
created. This v3 state-machine adapter is therefore a rejected method iteration;
the current valid `train-fasttext` parametric-memory gain remains the one-shot
adapter above.

Evaluate baseline local Qwen and adapter local Qwen against the same subset:

```sh
openevo-terminal-bench terminal-bench-local-parametric-memory-eval \
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
  --timeout-multiplier 2.0 \
  --agent-timeout-multiplier 3.5 \
  --exec-timeout-cap-seconds 1800 \
  --exec-timeout-min-seconds 1800 \
  --max-subagent-runtime-seconds 2400 \
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
`solver_temperature`, `vllm_generation_config`, optional Harbor
`timeout_multiplier` and `agent_timeout_multiplier`, optional internal
Terminal-Bench package budgets `exec_timeout_cap_seconds`,
`exec_timeout_min_seconds`, and `max_subagent_runtime_seconds`, and redacted
`agent_env` package knobs when supplied. The default
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
Bench tasks such as `train-fasttext` can spend most of the Harbor agent budget
inside slow setup commands such as `apt-get install`; use
`--agent-timeout-multiplier` when the adapter is expected to run multiple
setup/training commands before verification. `--exec-timeout-cap-seconds` sets
`EVOLAB_TB_EXEC_TIMEOUT_CAP_SECONDS`, which current EvoLab Terminal-Bench
packages use to cap each `tb_exec` call. `--exec-timeout-min-seconds` sets
`EVOLAB_TB_EXEC_TIMEOUT_MIN_SECONDS` for compatible packages that need to raise
explicitly requested short `tb_exec` timeouts, such as a model-requested 900
second install command. `--max-subagent-runtime-seconds` sets
`EVOLAB_TB_MAX_SUBAGENT_RUNTIME_SECONDS`; the OpenEvo runner records and
exports these newer package knobs, but they only affect runtime after the
installed task package reads those environment variables instead of using its
historical fixed timeout behavior. Many Terminal Bench verifiers run
`uvx -p ...`, which can download managed Python even when
wheel dependencies are local. When a local Python-build mirror is available,
pass `--verifier-python-install-mirror` with the uv-compatible
`.../python-build-standalone/releases/download` base; if the local mirror root
ends at `.../python-build-standalone`, the runner normalizes it to that download
base. Do not pass a host filesystem path to this option unless that path is also
valid inside the verifier container; uv will treat it as an invalid download URL.
For local cached verifier Python archives, prefer setting host-side
`EVOLAB_TB_UV_CACHE_TARBALL` and `EVOLAB_TB_UV_PYTHON_TARBALL` before launching
the runner so `DockerCpHarborEnvironment` can upload the archives and set
container-local `UV_DOWNLOAD_URL` and `UV_PYTHON_INSTALL_DIR`. The runner
records explicit verifier env values in the summary, but these host-side cache
upload variables are environment setup inputs rather than verifier env entries.
Treat controlled-subset results as subset evidence until the same path is run
over full Terminal Bench 2.1.

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

## Local-Success Replay Parametric Memory

`terminal-bench-local-success-replay-parametric-memory-job` builds a
`parametric_memory_lora_sft` job from successful local Harbor
`llm_calls.jsonl` rows. This path is opt-in and does not replace the
failed-prefix task-local corrective builder.

Use this path only with local/proxy inference. Textual memory works across
capture modes, but `parametric_memory` evaluation must use a local serving
backend that can load the LoRA adapter. Subscription harnesses are not valid for
this eval path.

The first controlled v2b run uses the successful local Qwen train-fasttext
trial:

```bash
openevo-terminal-bench terminal-bench-local-success-replay-parametric-memory-job \
  --success-trial-dir /tmp/tb21-task-local-parametric-trainfasttext-oneshot-20260709/eval-oneshot-r8-s260-pass2-cacheenv/parametric_memory/harbor_jobs/parametric_memory-train-fasttext/train-fasttext__7GGAsRu \
  --task-id train-fasttext \
  --output-root /tmp/tb21-local-success-replay-trainfasttext-20260709/train-replay-r8-s260 \
  --dataset-name tb21-local-success-replay-train-fasttext \
  --base-model Qwen/Qwen3.5-9B \
  --adapter-id tb-parametric-memory-train-fasttext-replay-r8-s260 \
  --trainer-command /root/evolab-vllm/bin/python \
  --trainer-arg /root/.config/superpowers/worktrees/ProRL-Agent-Server/openevo-memory-backends/scripts/qwen_lora_sft.py \
  --trainer-arg --train-file \
  --trainer-arg '{training_dataset}' \
  --trainer-arg --output-dir \
  --trainer-arg '{adapter_dir}' \
  --trainer-arg --model-name \
  --trainer-arg Qwen/Qwen3.5-9B \
  --trainer-arg --max-steps \
  --trainer-arg 260 \
  --trainer-arg --lora-r \
  --trainer-arg 8 \
  --trainer-arg --lora-alpha \
  --trainer-arg 16 \
  --trainer-arg --max-length \
  --trainer-arg 4096 \
  --require-tool-name tb_exec \
  --max-records 16 \
  --max-records-per-trial 16 \
  --output /tmp/tb21-local-success-replay-trainfasttext-20260709/train-replay-r8-s260/job.json
```

During paired eval, enable only `parametric_memory`; keep `text_memory`,
`skill_bundle`, and `agent_system` disabled. Set
`EVOLAB_TB_UV_CACHE_TARBALL` and `EVOLAB_TB_UV_PYTHON_TARBALL`, and do not pass
`--verifier-python-install-mirror` for this host's cached verifier path.

2026-07-09 training result:

- Run root:
  `/tmp/tb21-local-success-replay-trainfasttext-20260709/train-replay-r8-s260`.
- Dataset manifest:
  `/tmp/tb21-local-success-replay-trainfasttext-20260709/train-replay-r8-s260/dataset/manifest.json`.
- Dataset builder: `terminal_bench_local_success_replay`.
- Record count: 16 full-trace records from the successful one-shot local Qwen
  treatment trial.
- Adapter:
  `/tmp/tb21-local-success-replay-trainfasttext-20260709/train-replay-r8-s260/artifacts/workers/job-tb-parametric-memory-train-fasttext-replay-r8-s260/parametric_memory_lora_sft/adapter`.
- Trainer diagnostics: `Qwen/Qwen3.5-9B`, GPU7, LoRA `r=8`, `alpha=16`,
  `max_length=4096`, 260 steps, final logged loss `4.0529634134145454e-05`.

2026-07-09 paired eval evidence:

- Eval root:
  `/tmp/tb21-local-success-replay-trainfasttext-20260709/eval-replay-r8-s260-pass2-cacheenv-oldparams`.
- Baseline Harbor job completed 1/1 with reward `0.0`:
  `baseline/harbor_jobs/baseline-train-fasttext/result.json`.
- Treatment loaded adapter
  `tb-parametric-memory-train-fasttext-replay-r8-s260` on GPU7, but the
  outer eval exited before writing the top-level `summary.json`.
- The treatment Harbor job did not finish; its job result remained
  `n_running_trials=1`, `n_completed_trials=0`, and `finished_at=null`.
- Treatment trajectory made five tool calls. It first tried `/workspace`, then
  tried the Debian `fasttext` CLI directly on parquet data, then entered
  `apt-get install -y python3-pip && pip3 install fasttext`.
- The fifth `tb_exec` returned status `error` after `1258.3s` with no output
  artifact, and no `/app/model.bin` artifact was produced.

This replay adapter is therefore not a valid performance gain result. It proves
that the replay dataset/trainer/artifact path works, but this paired eval has
baseline `0.0` and no completed treatment score. The previous single-task
one-shot adapter remains the only observed `train-fasttext` parametric-memory
positive result in this log, and it should still be treated as task-specific
overfitting until repeated across more Terminal Bench 2.1 tasks.
