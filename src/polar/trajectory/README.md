# Trajectories

`polar.trajectory` defines the data shape used after gateway completion records
are reconstructed into trainable traces.

## Main Files

- `models.py`: completion sessions, completion records, traces, trajectories,
  builder specs, evaluator specs, and evaluator results.
- `registry.py`: builder and evaluator registration.
- `builder/`: trajectory construction strategies.
- `evaluator/`: reward and validation strategies.

## Data Model

- `CompletionRecord`: one normalized model call and response.
- `CompletionSession`: all completion records captured during one agent run.
- `Trace`: token ids, loss mask, messages, logprobs, reward, and metadata.
- `Trajectory`: terminal status plus one or more traces.

## Reward Attachment

Evaluators return an `EvalResult`. The gateway merges outcome rewards or
per-trace rewards into the built trajectory before sending the session result
back to the rollout server.

## Extension Points

Register new builders or evaluators through `registry.py`. Keep strategy names
stable because task files and Slime configs refer to them by string.
