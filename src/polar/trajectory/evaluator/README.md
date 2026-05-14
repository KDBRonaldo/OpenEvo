# Trajectory Evaluators

Evaluators attach rewards or validation metadata after an agent run finishes.

## Main Files

- `base.py`: evaluator contract.
- `session_completed.py`: simple success reward when the session completes.
- `test_on_output.py`: run a command and reward successful test output.
- `swebench_harness.py`: SWE-bench grading integration.
- `_patch_utils.py`: patch helpers used by SWE-style evaluators.

## Built-In Strategies

- `session_completed`: rewards sessions that reached a completed status.
- `test_on_output`: runs a configured command in the runtime and uses the exit
  result as reward signal.
- `swebench_harness`: grades SWE-bench style patches with the SWE-bench harness.

## Adding An Evaluator

Implement the base contract, return an `EvalResult`, and register the strategy
name in the evaluator registry. Keep external services, GPUs, and large
datasets out of default unit tests.
