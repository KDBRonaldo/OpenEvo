# Shared Golden Standard Evaluator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable golden-standard evaluator for evolution workflows, then feed only sanitized methodology-level feedback into agent-system evolution.

**Architecture:** Add `polar_evolution.golden_standard` as a shared utility that loads records, evaluates candidate outputs by exact article-scoped sequence matching, produces aggregate metrics, and renders leakage-safe methodology feedback. Experiment orchestration computes the evaluation once per round and stores the sanitized summary in the session payload/trajectory, so any evolution method can consume it without re-reading the ground truth.

**Tech Stack:** Python standard library, pytest, existing `polar_evolution` worker/store APIs, Codex CLI subscription runner.

---

### Task 1: Shared Evaluator Core

**Files:**
- Create: `src/polar_evolution/golden_standard.py`
- Create: `tests/evolution/test_golden_standard.py`

- [ ] **Step 1: Write failing tests**

Cover article-scoped TP/FP/FN matching, duplicate prediction handling, aggregate metrics, and sanitized feedback that excludes exact sequences and source sheet/row literals.

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/evolution/test_golden_standard.py -q`
Expected: import failure for `polar_evolution.golden_standard`.

- [ ] **Step 3: Implement minimal evaluator**

Implement `load_golden_standard_records`, `evaluate_records_against_golden`, `render_sanitized_golden_feedback`, and `assert_no_golden_leakage`.

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/evolution/test_golden_standard.py -q`
Expected: all tests pass.

### Task 2: Generic Evolution Feedback Consumption

**Files:**
- Modify: `src/polar_evolution/methods.py`
- Modify: `tests/evolution/test_worker_methods.py`

- [ ] **Step 1: Write failing reflector test**

Add a dataset record with `payload.session_result.metadata.evolution_feedback.golden_standard` and assert the LLM prompt includes sanitized aggregate guidance but not forbidden golden literals.

- [ ] **Step 2: Run targeted test to verify failure**

Run: `uv run pytest tests/evolution/test_worker_methods.py::<test_name> -q`
Expected: prompt lacks shared golden feedback.

- [ ] **Step 3: Implement generic feedback extraction**

Add helper logic that reads `evolution_feedback` from dataset records and appends it to reflection records generically.

- [ ] **Step 4: Run targeted test to verify pass**

Run the same targeted pytest command.

### Task 3: Experiment Harness Integration

**Files:**
- Modify: `/root/codex-54mini-test/five_article_agentic_workflow_subset/runs/agent_system_evolution_final_only_3round_20260617T063049Z/run_three_rounds.py`

- [ ] **Step 1: Integrate shared evaluator**

Import `polar_evolution.golden_standard`, evaluate each final JSONL after schema validation, write `golden_standard_evaluation.json`, and store only sanitized feedback under `payload.session_result.metadata.evolution_feedback.golden_standard`.

- [ ] **Step 2: Add leakage guard for generated agent systems**

After each reflector job, check generated `agents.md` with `assert_no_golden_leakage`; mark the job result unusable if it leaks exact golden literals.

- [ ] **Step 3: Re-run the 3-round subscription experiment in a fresh run directory**

Run the harness with Codex subscription mode and verify that each round still keeps ground truth outside the agent workspace.

### Task 4: Verification

**Files:**
- Existing repo tests and experiment outputs.

- [ ] **Step 1: Run targeted tests**

Run: `uv run pytest tests/evolution/test_golden_standard.py tests/evolution/test_worker_methods.py -q`

- [ ] **Step 2: Run evolution test suite**

Run: `uv run pytest tests/evolution -q`

- [ ] **Step 3: Run lint and whitespace checks**

Run: `uv run ruff check src/polar_evolution/golden_standard.py src/polar_evolution/methods.py tests/evolution/test_golden_standard.py tests/evolution/test_worker_methods.py`
Run: `git diff --check`
