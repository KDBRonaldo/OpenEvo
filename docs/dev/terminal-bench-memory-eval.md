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
This is the path expected for OpenEvo deployments backed by a local inference
server: the proxy can select a request-level LoRA adapter, while subscription
mode cannot.
