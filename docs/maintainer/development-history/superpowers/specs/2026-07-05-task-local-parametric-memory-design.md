# Task-Local Parametric Memory Design

Issue: #36
Date: 2026-07-05
Status: design approved, awaiting written-spec review

## Context

OpenEvo now has a local/proxy parametric-memory path for Terminal-Bench 2.1 and a
clean one-task proof point on `password-recovery`:

- baseline local Qwen3.6 without an adapter: pass@1 `0/1`;
- treatment local Qwen3.6 with a task adapter: pass@1 `1/1`;
- guard mode: `successful_auto_tested_exec`;
- run root:
  `/tmp/tb21-parametric-memory-v2-autostop-fixed-launcher-20260703-064752/local-eval-auto-tested-exec`.

That proof point is useful but too task-specific. The current
`terminal_bench_password_recovery_shorttarget_recipe` is hand-shaped around one
task and cannot establish whether parametric memory helps on a broader controlled
Terminal-Bench subset.

This design turns the password-recovery pattern into a reusable task-local
parametric-memory method. The first milestone is to produce and evaluate one
adapter per task. Cross-task adapters are delayed until at least two task-local
adapters show positive clean results.

## Goals

- Build task-local LoRA adapters from mixed success/failure trajectory pools.
- Keep the artifact boundary as `parametric_memory`; do not introduce a new
  artifact type or backend schema.
- Keep `text_memory`, `skill_bundle`, and `agent_system` disabled during
  parametric-memory-only evaluation.
- Evaluate on local/proxy inference only. Parametric memory must not be claimed
  for subscription-mode runs that cannot load or select adapters.
- Record enough run metadata to distinguish true adapter benefit from timeout,
  stale artifact selection, harness exceptions, or solver guard behavior.

## Non-Goals

- Do not run full Terminal-Bench 2.1 until the task-local path is stable on a
  small controlled subset.
- Do not mix finish/stop behavior into the first solve-action adapter. Prior
  password-recovery runs showed that finish-boundary examples can destabilize an
  otherwise useful solve adapter.
- Do not physically merge adapter weights in the gateway. The serving backend
  must expose the adapter, and OpenEvo selects it at request time.
- Do not hard-code new research method logic into gateway, store, scheduler, or
  context resolver layers.
- Do not train on protected held-out answers or literals that are only available
  from verifier output. Targets must be derived from task files, successful
  command patterns, or public trajectory context.

## Existing Evidence

The positive password-recovery run supports the narrow claim that a local LoRA
adapter can rescue a same-task failure under a controlled memory-only setting.
The evidence does not yet support a full Terminal-Bench 2.1 claim.

Several negative or noisy password-recovery runs are also informative:

- continued generation after a successful command can turn reward success into a
  timeout;
- finish-boundary training can reduce solve quality;
- local evaluation must verify Harbor exceptions, task status, tool-call counts,
  LLM-call counts, and server cleanup rather than relying only on reward.

The task-local design keeps those lessons as guardrails.

## Data Sources And Candidate Tasks

The initial trajectory pool is:

```text
/home/jyw/openevo-skill-rl-experiments/pools/tb21-current-20260702/trajectory_pool.jsonl
```

It contains mixed Terminal-Bench 2.1 outcomes across 89 tasks. First-pass task
selection requires:

- at least one successful trajectory and at least one failed trajectory for the
  same task id;
- enough failed prefixes to make a correction target meaningful;
- manageable runtime for local smoke evaluation;
- public task context sufficient to derive the target action without copying
  hidden answer literals;
- no dependency on text memory, skill evolution, or agent-system changes.

Initial task-local candidates are:

- `train-fasttext`;
- `make-mips-interpreter`;
- `gcode-to-text`.

Tasks with only failures, such as `filter-js-from-html`, `dna-insert`, and
`make-doom-for-mips`, are excluded from the first pass because they do not supply
positive demonstrations for the task-local builder.

## Method Shape

The implementation should add a reusable task-local builder under the existing
evolution method boundary:

```text
trajectory pool
  -> task-local record selector
  -> target extraction policy
  -> compact SFT dataset artifact
  -> parametric_memory_lora_sft
  -> ArtifactRegisterRequest(type="parametric_memory")
  -> local Terminal-Bench baseline-vs-adapter eval
```

The method should keep two layers separate:

1. Generic task-local plumbing: task selection, prefix extraction, successful
   action extraction, dataset writing, metadata, lineage, and validation.
2. Task-specific extraction policy: small adapters that know how to choose the
   next solve action for a task family when generic extraction is insufficient.

This avoids another single-task recipe while still allowing task-aware extraction
where Terminal-Bench tasks differ materially.

## Task-Local Dataset Builder

The builder consumes a trajectory pool and emits a compact SFT dataset. Each
record should include:

- `task_id`;
- failed trajectory id and source prefix metadata when available;
- successful trajectory id and source action metadata;
- prompt messages or real prefix messages used as the training input;
- one target assistant tool call or response segment;
- extraction policy name and version;
- rejection reason for any skipped candidate;
- source pool path and record offsets for reproducibility.

The first implementation should prefer real-prefix corrective records: take a
failed trajectory prefix at the point where the next action matters, then train
the model to emit a successful next action from a same-task successful trajectory.
If exact alignment is unavailable, the builder can fall back to a shorter
task-context prefix, but it must record the fallback in metadata.

The dataset should be small by default. The current goal is parametric memory of
task-solving behavior, not broad behavior cloning of complete transcripts.

## Target Extraction Policy

The initial policy should extract solve actions only. It should not add final
finish calls, stop tokens beyond normal chat-template requirements, or
post-success continuation examples.

Valid targets include:

- tool calls that inspect task files or execute the minimal solve command;
- command patterns found in successful trajectories and reproducible from public
  task state;
- code or shell snippets whose behavior can be validated by the Terminal-Bench
  verifier or by an explicit guard command.

Invalid targets include:

- hidden verifier answers;
- copied files or literals only visible after a protected test;
- targets that depend on unrelated text memory, skill bundles, or agent-system
  prompts;
- responses that only train the model to stop after success rather than solve.

The builder should fail closed. If it cannot derive a defensible target for a
task, it should mark the task unsupported for this pass instead of emitting a
low-confidence training record.

## Trainer And Artifact Contract

Use the existing `parametric_memory_lora_sft` contract. The trainer still writes
a PEFT-compatible adapter directory and registers:

- `ArtifactRegisterRequest(type="parametric_memory")`;
- `manifest.adapter_id`;
- `manifest.base_model`;
- `manifest.adapter_format`;
- lineage pointing to the SFT dataset and source trajectory pool;
- compatibility limiting the adapter to the intended harness, model, and task
  tags;
- scores such as `quality`, `train_record_count`, and later
  `heldout_reward_delta`.

No artifact schema change is needed. Any method-specific details belong in
`manifest`, `lineage`, and `scores`.

The Qwen chat-template and loss-mask requirements from the local parametric eval
work still apply. In particular, tool-call examples must use the model's chat
template with `tools`, and the first generated target token must be included in
the loss mask.

For Qwen MoE models, local eval should continue to support adapter key rewriting,
including:

```text
--adapter-key-rewrite qwen3_5_moe_vllm_language_model
```

Rank and key compatibility must be smoke-tested before treating an adapter as
deployable for a model.

## Local Evaluation Matrix

Model priority:

1. `Qwen/Qwen3.6-35B-A3B` as the main model.
2. `Qwen/Qwen3-30B-A3B-Instruct-2507` as the newer Qwen3 fallback.
3. `Qwen/Qwen3.5-9B` as a lower-cost smoke model.

Each task starts with a one-attempt smoke:

```text
baseline: local model, no adapter
treatment: same local model, selected task-local adapter
```

If the treatment improves pass@1 without Harbor exceptions or timeout artifacts,
expand that task to `n=3` or `n=5`. Results must report:

- task id;
- model name and served model name;
- adapter artifact id, adapter id, adapter path, and rewritten key count;
- active artifact allowlist;
- disabled artifact types;
- pass@1, pass@k, reward, and task status;
- Harbor exception count and exception types;
- tool-call count and LLM-call count;
- timeout state;
- vLLM server command, PID, port, GPU list, and log path;
- whether server and GPU processes were cleaned up after the run.

The success gate for a task-local adapter is:

- adapter pass@1 is greater than baseline pass@1 on the same task and model;
- reward success is not paired with a later timeout or harness exception;
- the transcript shows the adapter changed solve behavior rather than only
  changing post-success stopping;
- no stale promoted artifact outside the explicit allowlist was injected.

## Cross-Task Promotion

Cross-task adapters come after task-local evidence. The trigger is at least two
clean task-local positives across the first controlled subset.

The cross-task builder should merge only accepted task-local SFT records. It
should keep task ids and extraction policies in metadata so negative transfer can
be diagnosed. The cross-task adapter must be evaluated against:

- tasks that improved with task-local adapters, to check retained benefit;
- at least one mixed-outcome task not used in training, to check transfer;
- baseline without adapter, to avoid confusing general model variance with
  parametric memory.

If the cross-task adapter regresses previously positive task-local results, keep
the task-local adapters as the promoted artifacts for controlled evaluation.

## CLI And Config Surface

The first implementation should extend the local parametric-memory tooling rather
than changing subscription runners. The CLI/config surface should allow:

- selecting task ids;
- selecting the trajectory pool path;
- selecting the extraction policy or using an auto policy;
- writing the task-local SFT dataset without training, for inspection;
- training one adapter per task;
- evaluating baseline versus adapter with text memory, skill bundle, and
  agent-system artifacts disabled;
- setting model, server URL, managed-server mode, GPU list, timeout, and
  adapter-key rewrite;
- writing a single summary that links dataset artifact, adapter artifact, and
  eval output.

The default path should be inspectable and resumable. Dataset building, training,
and evaluation should be separate enough that failed local inference does not
force regenerating datasets.

## Error Handling And Guardrails

- Refuse parametric-memory evaluation in subscription mode.
- Refuse evaluation when the selected adapter `base_model` is incompatible with
  the local model unless an explicit compatibility override is provided.
- Refuse to use implicit promoted artifacts during controlled memory-only
  ablations; require an explicit artifact allowlist.
- Reject target records that do not include a generated assistant target.
- Reject unsupported tasks with a recorded reason rather than producing empty or
  low-confidence SFT records.
- Preserve all source metadata needed to reproduce a target extraction decision.
- Clean up managed vLLM process groups on normal completion, errors, and
  interrupt paths.

## Testing Strategy

Implementation should be test-first. Focused tests should cover:

- task selection from a mixed success/failure trajectory pool;
- exclusion of all-fail and all-pass tasks from the first task-local pass;
- target extraction metadata and rejection reasons;
- SFT dataset writing with stable record fields;
- refusal to emit records with missing assistant targets;
- `parametric_memory_lora_sft` registration metadata for task-local adapters;
- local eval command construction for baseline and treatment runs;
- strict artifact allowlist behavior for parametric-memory-only evaluation;
- adapter key rewrite propagation for Qwen MoE models;
- summary aggregation of pass rate, exceptions, timeouts, and cleanup metadata.

Regression coverage should stay in evolution and local Terminal-Bench parametric
tests unless a gateway or context resolver contract changes.

## Experiment Plan

1. Build dry-run datasets for `train-fasttext`, `make-mips-interpreter`, and
   `gcode-to-text`. Inspect record counts, targets, and rejection reasons.
2. Train one local adapter for the most straightforward accepted task.
3. Run `n=1` baseline versus adapter on `Qwen/Qwen3.6-35B-A3B`.
4. If clean positive, expand that task to `n=3` or `n=5`.
5. Repeat for the remaining first-pass tasks.
6. After at least two clean positives, build and evaluate one cross-task adapter.

Every experiment summary must label the scope explicitly as a controlled subset.
Full Terminal-Bench 2.1 performance should only be claimed after running the full
benchmark under the same memory-only controls.

## Documentation And PR Workflow

This design is tracked by #36 and should be referenced from the implementation
PR as `Part of #36` unless the PR completes every acceptance criterion. Any code
change should update the relevant docs under:

- `docs/architecture/evolution-api-and-method-integration.md`;
- `docs/architecture/reference-evolution-worker.md`;
- `docs/dev/terminal-bench-memory-eval.md`.

The PR body should list the focused tests, `git diff --check`, and any live
Terminal-Bench smoke runs. If an implementation PR is docs-only or experiment
script-only, it still needs to explain why broader regression tests were not run.

## Open Questions

- Which of the first three candidate tasks yields the cleanest target extraction
  policy after dry-run inspection?
- Should unsupported task policies live in a registry keyed by task id, task tag,
  or extractor capability?
- How many positive task-local adapters are enough before a full cross-task run?

These are implementation-planning questions. They do not block writing the first
task-local builder and dry-run dataset tests.
