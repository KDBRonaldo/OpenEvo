# Terminal Bench Per-Task Evolution Design

## Goal

Run Polar-based evolution on Terminal Bench with one independent evolution loop per
task. The first implementation target is agent-system evolution, but the
pipeline must treat agent systems, skills, and memory as evolution artifact types
so later work can add skill and memory backends without replacing the task
orchestration.

## Scope

The initial experiment uses the existing Terminal Bench Harbor wrapper with Codex
subscription mode. For each task, the system will:

1. Use an existing baseline trial as round 0, or run one if no baseline exists.
2. Convert that task's trial directory into a Polar dataset artifact.
3. Create a task-scoped Polar evolution job.
4. Materialize the evolved artifact into the next Harbor run.
5. Run the same Terminal Bench task again with the evolved artifact injected.
6. Ingest the new trial and repeat for additional rounds.

The first runnable version only injects agent-system artifacts. It records
artifact type metadata and uses a generic runner interface so skills and memory
can be added as additional materializers later.

## Artifact Model

Each per-task evolution round produces zero or more typed artifacts:

- `agent_system`: an `AGENTS.md`-style instruction artifact injected into Codex.
- `skill_bundle`: a future bundle of callable task-solving procedures or
  reusable playbooks.
- `memory`: a future structured or textual memory artifact for facts, failure
  modes, and reusable observations.

The runner tracks these fields for each artifact:

- `artifact_id`
- `artifact_type`
- `task_id`
- `round`
- `method`
- `path`
- `sha256`
- `source_dataset_artifact_ids`

For round 1, only `agent_system` is required. Skill and memory entries may be
empty.

## Leakage Policy

Per-task evolution is task-scoped, but the evolved content should still be
methodological rather than answer leakage. The artifact metadata may include the
Terminal Bench task id, but the generated artifact content must not contain
literal task ids, original task instructions, verifier test names, expected
outputs, or copied failure snippets that would let the next run bypass problem
solving.

The existing Polar `agent_system_audit.forbidden_literals` guard remains active.
Task-specific metadata is used for routing and evaluation, not as allowed
content in `AGENTS.md`.

## Runner Design

Add a small per-task experiment runner that accepts:

- Terminal Bench task root.
- Task ids.
- Optional existing baseline job or trial root.
- Number of evolution rounds.
- Reflector method and model.
- Codex subscription environment settings.
- Artifact types to evolve, initially `agent_system`.

For each task, the runner maintains an isolated directory:

```text
<run-root>/
  tasks/<task-id>/
    r0/
      baseline-trial/
      dataset.json
    r1/
      agent-system-job.json
      agent-system-artifact/
      trial/
    summary.json
```

Round 0 can reuse the already completed wrapper trials from the 10-task
comparison run. Later rounds call Harbor for one task at a time.

## Agent-System Injection

Extend `EvoLabHarborAgent` in `codex_subscription` mode with an explicit
artifact injection parameter:

- `agent_system_path`: path to an evolved `AGENTS.md` artifact.

The wrapper reads the file, computes a hash, writes the composed instruction to
the trial logs, and passes this to Harbor's installed Codex agent:

```text
Additional operating rules:
<agent_system_text>

Task:
<original_terminal_bench_instruction>
```

The wrapper records `agent_system_path` and `agent_system_sha256` in
`result.json` metadata so the Terminal Bench bridge can attach the active
artifact to the next Polar event.

## Skill And Memory Extension Points

The runner should not hard-code agent-system-only assumptions. It should use an
artifact materialization interface:

```text
materialize(artifact_type, artifact_path, harbor_agent_kwargs, run_dir)
```

Initial behavior:

- `agent_system`: set `--ak agent_system_path=<path>`.
- `skill_bundle`: unsupported, recorded as skipped with a clear reason.
- `memory`: unsupported, recorded as skipped with a clear reason.

Future skill support can mount a skill directory and pass the location through
Codex or EvoLab runtime settings. Future memory support can mount a memory file
or directory and inject a short pointer plus retrieval policy.

## Method Selection

The first per-task run should use:

- `agent_system_pareto_reflector` when multiple candidate or history artifacts
  are available.
- `agent_system_reflector` for a single round-0 dataset if the current worker
  path cannot yet create pareto candidates.
- `agent_system_history_reflector` after multiple per-task datasets exist and
  pareto selection is not enabled.

The design prefers history-aware or pareto reflection after round 1 so the
reflector sees all previous trajectories and score deltas for that same task.

## Evaluation Output

The runner writes a machine-readable summary with:

- task id
- baseline reward
- evolved reward per round
- pass/fail transition
- active artifact ids and hashes
- verifier failure summaries
- paths to Harbor trials and Polar jobs

The first experiment should compare the same 10 tasks already used for the
official Codex vs wrapper subscription check.

## Testing

Unit tests should cover:

- `agent_system_path` instruction composition.
- hash and metadata recording.
- missing `agent_system_path` failures.
- env JSON compatibility in Codex subscription mode.
- per-task runner planning without invoking Harbor.
- artifact materialization behavior for `agent_system`, `skill_bundle`, and
  `memory`.

The first integration run should use a small 3-task pilot before all 10 tasks if
runtime or subscription limits are tight. If the pilot confirms injection and
record capture, run the full 10-task comparison.

## Non-Goals

This design does not attempt full Terminal Bench 2.1 leaderboard reproduction,
shared cross-task evolution, or automatic promotion of a single global
Terminal-Bench agent system. Each task evolves independently.
