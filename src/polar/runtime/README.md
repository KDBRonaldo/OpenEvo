# Runtime Backends

`polar.runtime` provides isolated sandboxes for agent sessions.

## Main Files

- `models.py`: `RuntimeSpec`, `PrepareAction`, `ExecInput`, and `ExecResult`.
- `base.py`: runtime backend contract.
- `docker.py`: Docker runtime implementation.
- `apptainer.py`: Apptainer runtime implementation.
- `factory.py`: backend lookup.

## Runtime Contract

A runtime backend prepares files and directories, executes commands, exposes a
workspace, and cleans up after the session. It should hide container-specific
details from agent harnesses and evaluators.

## Prepare Steps

`RuntimeSpec.prepare` and `RuntimeSpec.eval_prepare` accept ordered actions:

- `upload_file`: copy one host file into the runtime.
- `upload_dir`: copy one host directory into the runtime.
- `exec`: run a command inside the runtime.

Prepare steps run before the agent. Eval-prepare steps run before evaluation
when an evaluator needs extra setup.

## Docker And Apptainer

Docker is the default backend for local examples. Apptainer is supported for
clusters where container execution must avoid Docker daemon access.
