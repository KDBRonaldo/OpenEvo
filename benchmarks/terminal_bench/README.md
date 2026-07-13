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
- `terminal-bench-parametric-memory-job`,
  `terminal-bench-task-local-parametric-memory-job`,
  `terminal-bench-local-success-replay-parametric-memory-job`, and
  `terminal-bench-local-parametric-memory-eval` for the unchanged WIP parametric
  automation.

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
