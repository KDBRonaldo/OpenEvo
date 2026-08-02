# OpenEvo Terminal Bench Automation

This standalone package owns Terminal Bench-specific trial I/O, Harbor execution,
reporting, task-local orchestration, and maintainer commands. It imports the installed
OpenEvo Core package for event, dataset, job, artifact, worker, method, and protected
GEPA transition contracts. OpenEvo Core and Desktop do not import or package this
automation.

This is release-maintainer benchmark automation, not an ordinary-user OpenEvo CLI.
There is no legacy `openevo.evolution.terminal_bench_*` module or Core CLI alias.

## Install

Install the matching Core wheel first, then this package:

```bash
python -m pip install dist/openevo-0.1.0-py3-none-any.whl
python -m pip install benchmarks/terminal_bench
openevo-terminal-bench --help
```

For repository development:

```bash
python -m pip install -e .
python -m pip install -e benchmarks/terminal_bench
python -m pytest benchmarks/terminal_bench/tests -q
```

## Commands

The `openevo-terminal-bench` entrypoint provides:

- `terminal-bench-events` and `terminal-bench-dataset` for Harbor result conversion.
- `terminal-bench-agent-system-job` and `terminal-bench-text-memory-job` for benchmark
  dataset/job preparation through Core contracts.
- `terminal-bench-per-task-evolution` and `terminal-bench-group-evolution` for the
  existing task cadence and reporting workflow.
- `terminal-bench-continual-memory-eval` for the controlled base vs ordinary
  sequential-LoRA vs SD-LoRA task-stream experiment. It invokes model inference
  only through Core `CodexHarness` and Gateway, trains user adapters on the
  daemon profile, and never accepts an arbitrary trainer command.

Example dry-run plan:

```bash
openevo-terminal-bench terminal-bench-per-task-evolution \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id filter-js-from-html \
  --run-root /tmp/tb21-plan \
  --model gpt-5.5 \
  --reflector-model gpt-5.5 \
  --dry-run \
  --output /tmp/tb21-plan/plan.json
```

Continual-memory dry run:

```bash
openevo-terminal-bench terminal-bench-continual-memory-eval \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id password-recovery \
  --training-trial password-recovery=/path/to/successful/trial \
  --run-root /tmp/tb21-continual \
  --model Qwen/Qwen3-4B-Instruct-2507 \
  --model-revision cdbee75f17c01a7cc42f958dc650907174af0554 \
  --codex-version 0.144.1 \
  --gpu 3 \
  --dry-run \
  --output /tmp/tb21-continual/plan.json
```

## Continual-Memory Evaluation

The continual-memory command is a maintainer experiment, not a second OpenEvo
inference API. It requires:

- one ordered `--task-id` and successful `TASK_ID=TRIAL_DIR` training trial per
  task;
- an exact immutable Hugging Face model revision available to the local vLLM
  and trainer environments;
- one CUDA device visible to both the Daemon-owned trainer and vLLM;
- a Linux Codex native binary whose `--version` exactly matches
  `--codex-version`; and
- a Harbor Docker daemon that can reach the Gateway address selected by
  `--gateway-advertise-host` or unambiguously auto-detected from the host.

The benchmark hashes the host Codex binary and its bundled `rg`, uploads those
fixed payloads into each isolated Harbor task container, and verifies their
hashes and version before execution. Task inference then follows
`CodexHarness -> Gateway -> local vLLM`; adapter training does not call a model
API. The supplied training trials are read only as successful trajectory data
and must not expose verifier answers or other benchmark-private data. Before
training, the command captures the live base-model Gateway request contract for
each task and projects teacher turns into that exact messages/tools shape. Every
adapter evaluation must reproduce the same contract digest or fail closed.

OpenEvo's language-agent SD-LoRA variant retains a bounded trajectory replay
buffer in the cumulative artifact. Each new generation freezes historical A/B
global unit-Frobenius directions, restores their learned magnitudes, and jointly
optimizes the new direction plus shared magnitudes on current and deterministic
prior replay examples. New directions are normalized at the generation boundary
with their norm absorbed into the magnitude, preserving the exact effective
model. Serving still loads exactly one cumulative adapter; replay does not
introduce task routing or a bank of independently selected LoRAs.
Because upstream SD-LoRA is rehearsal-free, the OpenEvo artifact explicitly
records `paper_equivalent=false`, `rehearsal_free=false`, and
`retention_strategy=bounded_trajectory_replay`. Results must call this the
replay-assisted SD-LoRA adaptation rather than attribute them to the original
paper method.

For an ordered stream of `N` tasks, the command runs the base model once on all
tasks, then evaluates every ordinary sequential-LoRA generation and every
SD-LoRA generation on all tasks. This produces `N + 2N^2` Harbor attempts. The
report includes the full reward matrices, average accuracy, average incremental
accuracy, backward transfer, forgetting, adapter bytes, training time, and peak
allocated GPU memory. Ordinary LoRA and SD-LoRA use the same task order,
examples, base model revision, target modules, rank per generation, optimizer,
and training budget; their cumulative adapter sizes can differ and are reported
explicitly.

A small run with one attempt per task is an infrastructure and numerical smoke,
not a performance claim. Performance evidence requires the predeclared task
stream, repeated attempts or another justified uncertainty protocol, and the
complete task-level report retained with the exact model, Codex, image, and
artifact identities.

The completed MemEvolve and replay-assisted SD-LoRA smokes, task-level outcomes,
report hashes, and limitations are recorded in
[`docs/dev/memory-method-experiment-results.md`](../../docs/dev/memory-method-experiment-results.md).

## Frozen Gate Data

`src/openevo_terminal_bench/data/gates/` records data-only historical gate inputs:

- Terminal Bench 2.1, Codex subscription, `gpt-5.5`.
- Text memory: 21 applicable tasks, historical threshold 12/21.
- Skill bundle: 25 baseline-failed tasks, historical threshold 14/25.
- Agent system: 25 baseline-failed tasks, historical threshold 17/25.

The task sources are
`docs/dev/tb21_codex_gpt55_failed_tasks.md` and
`docs/dev/terminal-bench-memory-eval.md`. The skill-bundle manifest records 14/25
only as an unverified, user-provided historical aggregate. Historical per-task
skill-bundle evidence is unavailable, and this PR did not execute that gate. The
manifest's closed `evidence` object makes those facts machine-readable; it is not
an algorithm-performance claim or release evidence.

These manifests contain no fabricated per-task run results. Every final release
candidate must rerun the complete frozen gate for each family and retain the
required task-level evidence. The historical aggregate cannot replace that run,
and this migration does not execute or prove any release gate.

## Current Limitation

This PR is the mechanical A3 package migration and is `Part of #156`. It does not bind
the runner to B3 revision admission. Admission, queued/not-ready handling, and atomic
next-revision activation remain for the next PR; Issue #156 stays open. Until that work
lands, this automation must not be described as satisfying the cross-session product
contract.
