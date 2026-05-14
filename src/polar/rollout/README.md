# Rollout Service

`polar.rollout` owns task submission, gateway scheduling, callback collection,
status reporting, and result persistence.

## Main Files

- `server.py`: FastAPI app for task, callback, node, health, and status APIs.
- `manager.py`: task lifecycle and session expansion.
- `pipeline.py`: dispatch to gateways, wait for callbacks, poll fallback, and
  result persistence.
- `balancer.py`: node health, pressure tracking, and scheduling.
- `models.py`: public task, session, node, heartbeat, and result models.
- `timer.py`: per-stage timing helpers.

## Lifecycle

1. `TaskRequest` is accepted by the rollout server.
2. The manager creates one session per requested sample.
3. The scheduler picks a healthy non-draining gateway with available capacity.
4. The pipeline dispatches the session and waits for a callback.
5. Missing callbacks can fall back to gateway polling during the grace window.
6. Results are persisted when `rollout.save_dir` is configured.

## Task Request Shape

A minimal task looks like:

```json
{
  "task_id": "example-task-001",
  "instruction": "Write a calculator and save it as calculator.py",
  "num_samples": 8,
  "timeout_seconds": 900,
  "runtime": {
    "backend": "docker",
    "image": "polar-localhost-calculator:latest",
    "workdir": "/polar/session/workspace",
    "network": "host"
  },
  "agent": {
    "harness": "codex",
    "model_name": "openai/gpt-5.4"
  },
  "builder": {
    "strategy": "prefix_merging"
  },
  "evaluator": {
    "strategy": "test_on_output",
    "config": {
      "test_command": "python3 test_calculator.py && echo 'PASSED test_calculator'",
      "expected_output_json": {"test_calculator": "PASSED"}
    }
  }
}
```

Task fields:

- `runtime` selects Docker or Apptainer and describes the sandbox image.
- `agent` selects a built-in harness or a custom import path.
- `builder` selects how completion records become trajectories.
- `evaluator` attaches rewards after the agent run.
- `metadata` can carry training fields such as group id, rollout step, or policy
  version.

## Scheduling

Scheduling prefers healthy nodes with lower run pressure, then lower init
pressure. Nodes become ineligible when stale, draining, or blocked by post-run
backlog.
