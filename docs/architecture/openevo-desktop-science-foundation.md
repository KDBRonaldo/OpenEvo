# OpenEvo Desktop Science Foundation

Tracked by #37.

This document defines the foundation contract for OpenEvo Desktop Science
projects. A Science Project config is the user-facing input for ordinary
science users. It validates a task, environment, execution mode, and evolution
targets, then compiles to the existing OpenEvo `ExperimentConfig` contract.

Users do not configure runtime images, Polar gateway wiring, Docker lifecycle,
or model serving directly in the common path. The science layer chooses a
runtime profile and execution mode, and the lower-level OpenEvo/Polar experiment
compiler keeps responsibility for the concrete experiment payload.

## Boundary

The science layer is a config and compilation layer. It does not:

- run Codex;
- start Docker;
- open SSH connections;
- call model APIs;
- manage gateway, vLLM, or Docker Compose lifecycles.

It validates Science Project input and compiles it to the existing OpenEvo/Polar
experiment contract. OpenEvo still wraps the Codex harness; this layer only makes
that contract easier for Desktop science workflows to produce.

## Task Sources

Science Project task sources describe where the task workspace comes from:

- `remote_path`: use an already available path on the remote machine.
- `scratch`: run without an uploaded workspace.
- `local_folder`: upload or mirror a local folder before compilation.
- `git_repository`: clone or materialize a repository before compilation.

`local_folder` and `git_repository` require a Desktop or remote backend to
prepare the workspace first. After preparation, the compiler receives a prepared
remote workspace path for the task, for example through:

```bash
openevo science compile CONFIG --prepared-workspace task=/remote/path
```

The science compiler does not upload local files or clone repositories itself.

## Environment Profiles

Science environment profiles compile to runtime images:

| Profile | Runtime image |
|---|---|
| `managed_science` | `openevo/science-runtime:0.1.0` |
| `python_research` | `openevo/python-research-runtime:0.1.0` |
| `custom_image` | developer-supplied override |

`custom_image` is an escape hatch for developers and advanced experiments. It is
not the default user path.

## Execution Modes

`codex_subscription_transcript` uses Codex subscription authentication and sets
`agent.settings.capture_mode="transcript"`. This produces transcript trajectory
data for text evolution and explicitly has no token-level metrics.

`codex_managed_local_inference` uses proxy authentication and requires
`execution.hf_model`. The compiler sets the agent model to that Hugging Face
model and injects `OPENEVO_MANAGED_HF_MODEL` into the runtime environment. The
remote backend is responsible for starting and wiring vLLM, the gateway, and the
proxy path.

Science Projects do not support `evolution.parametric_memory` in this foundation
slice, including managed local inference projects. Parametric memory requires a
separate adapter source or trainer contract that is not part of the Science
Project schema yet.

## Runtime Prepare

Task `setup_commands` compile to `RuntimeSpec.prepare` exec actions running in
the experiment workspace. The science compiler first emits a prepare action that
creates `/polar/session/workspace`, then emits user setup commands. Workspace
upload is handled by the lower-level experiment compiler before these prepare
actions run for workspace-backed tasks, so dependency installation and other
setup steps can read workspace files.

## Evolution Targets

Science Projects support text memory, skill bundle, and agent system evolution
targets in this foundation slice. Parametric memory is intentionally rejected for
all Science Projects until adapter source and trainer configuration are defined.

## Preflight

`openevo.remote.preflight` defines fakeable remote preflight contracts. It does
not implement SSH transport. Callers provide a `RemoteProbe` that can run remote
commands and return structured results.

Preflight checks SSH first with `true`. If SSH fails, it returns immediately and
does not run later checks. After SSH succeeds, Docker is checked with
`docker info`, disk capacity is checked with `df -Pk "$HOME"`, and other remote
capabilities are reported through the same fakeable probe contract. Codex CLI
and subscription checks run only when subscription execution is required.

## CLI

The initial user-visible CLI for this slice is:

```bash
openevo science compile CONFIG [--json] [--prepared-workspace task=/remote/path]
```

`--prepared-workspace` can be repeated when multiple tasks need prepared remote
workspace paths.

## Limitations

This foundation slice does not include:

- a full Desktop UI;
- vault or SSH tunnel management;
- a remote backend implementation;
- Docker Compose lifecycle management;
- vLLM lifecycle management;
- parametric memory or adapter training for Science Projects.

Those capabilities remain separate layers above or below the Science Project
contract.
