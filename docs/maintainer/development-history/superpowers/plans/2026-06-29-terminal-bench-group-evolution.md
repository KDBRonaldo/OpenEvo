# Terminal Bench Group Evolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add configurable Terminal Bench group-level agent-system evolution where one task group evolves one shared `agent_system` artifact.

**Architecture:** Keep the existing per-task runner intact. Add a group-level runner in `src/polar_evolution/terminal_bench_per_task.py` that reuses job creation, artifact discovery, Harbor command construction, and trial reward parsing, but changes the orchestration unit from one task to one named group. Add CLI support and documentation for the new flow.

**Tech Stack:** Python, argparse, pytest, existing Polar Evolution local store/worker helpers, Terminal Bench Harbor command integration.

---

### Task 1: Group Runner Tests

**Files:**
- Modify: `tests/evolution/test_terminal_bench_per_task.py`

- [ ] **Step 1: Write failing tests**

Add tests that demonstrate:

```python
def test_group_evolution_evaluates_each_candidate_across_all_tasks_and_selects_macro_mean(...):
    ...

def test_group_evolution_feeds_selected_group_trials_into_next_round(...):
    ...
```

The first test should create two baseline tasks and two candidate artifacts. Candidate 1 should score `[1.0, 0.0]`, candidate 2 should score `[1.0, 1.0]`; the selected artifact must be candidate 2 with aggregate score `1.0`.

The second test should run two rounds and verify round 2 job inputs are the selected candidate's per-task trial directories from round 1.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
pytest tests/evolution/test_terminal_bench_per_task.py -q
```

Expected: FAIL because `run_group_evolution` is not defined/importable.

### Task 2: Group Runner Implementation

**Files:**
- Modify: `src/polar_evolution/terminal_bench_per_task.py`

- [ ] **Step 1: Implement group data model and dry-run**

Add:

```python
@dataclass(frozen=True)
class TerminalBenchTaskGroup:
    group_id: str
    task_ids: list[str]
    objective: str = "macro_mean_reward"
```

Add `run_group_evolution_dry_run(...)` returning `groups` rather than `tasks`.

- [ ] **Step 2: Implement aggregate scoring**

Add `_aggregate_group_score(task_rewards, objective)` with first-version support for `macro_mean_reward`.

- [ ] **Step 3: Implement `run_group_evolution(...)`**

The runner should:

```python
for group in groups:
    baseline_trials_by_task = {task_id: _find_baseline_trial(...)}
    input_trials_by_task = baseline_trials_by_task
    for round_number in range(...):
        generation_input_trials = list(input_trials_by_task.values())
        create one agent-system job for group
        for candidate in agent_system_artifacts:
            for task_id in group.task_ids:
                run Harbor with same candidate artifact
                collect trial reward
            aggregate score
        select best candidate by aggregate score
        input_trials_by_task = selected["task_trials"]
```

It must store group artifacts under `run_root/groups/<group_id>/evolution` and Harbor jobs under `run_root/groups/<group_id>/r<round>/...`.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
pytest tests/evolution/test_terminal_bench_per_task.py -q
```

Expected: PASS.

### Task 3: CLI Support

**Files:**
- Modify: `src/polar_evolution/cli.py`
- Modify: `tests/evolution/test_terminal_bench_per_task.py`

- [ ] **Step 1: Write failing CLI tests**

Add tests for:

```python
def test_terminal_bench_group_evolution_cli_dry_run_writes_plan(...):
    ...

def test_terminal_bench_group_evolution_cli_live_mode_parses_env_and_writes_output(...):
    ...
```

The command should be `terminal-bench-group-evolution`.

- [ ] **Step 2: Implement parser and main dispatch**

Add CLI flags mirroring per-task evolution, with:

```text
--group-id
--task-id
--objective macro_mean_reward
```

Require at least two `--task-id` values for group mode.

- [ ] **Step 3: Run CLI tests**

Run:

```bash
pytest tests/evolution/test_terminal_bench_per_task.py -q
```

Expected: PASS.

### Task 4: Documentation

**Files:**
- Modify: `src/polar_evolution/README.md`

- [ ] **Step 1: Document group mode**

Add a section explaining that per-task mode evolves one artifact per task, while group mode evolves one shared artifact for a named group. Include an example command and summary semantics.

- [ ] **Step 2: Run docs-neutral checks**

Run:

```bash
git diff --check
```

Expected: no whitespace errors.

### Task 5: Review and Verification

**Files:**
- Review all changed files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
pytest tests/evolution/test_terminal_bench_per_task.py -q
git diff --check
```

- [ ] **Step 2: Request subagent code review**

Use a reviewer subagent to inspect the diff for correctness, security, regression risk, and missing tests.

- [ ] **Step 3: Fix review feedback and repeat**

Address all Critical/Important/P2+ issues, rerun focused tests, and request another review. Repeat until there are no obvious actionable issues.
