# Runtime Backends

`openevo.runtime` gives each rollout session its own **sandbox** — one container
(Docker or Apptainer) that lives for the whole session. The gateway uses it to
run the prepare recipe, execute the agent and evaluator commands, move files in
and out, then tear it down.

## Mental model

- **One `RuntimeSpec` → one container**, shared across the init → run → eval
  stages of a session.
- The host session directory is **bind-mounted** to a fixed in-container path,
  `/openevo/session` (`RUNTIME_SESSION_DIR`). Uploads/downloads under that path
  are plain host-side file copies (fast); paths outside it fall back to
  `docker cp` / `tar` streaming.
- Commands run in a login shell (`bash -lc`) with working directory
  `cwd or spec.workdir or /openevo/session`.
- The factory verifies the chosen backend actually supports what the spec asks
  for (GPUs, CPU/memory limits, internet-off) before building it.

## Main files

- `models.py`: `RuntimeSpec`, `PrepareAction`, `ExecInput`, `ExecResult`.
- `base.py`: the `BaseRuntime` contract, the `/openevo/session` path constants, and
  the bind-mount copy helpers.
- `docker.py`: `DockerRuntime` — the default backend.
- `apptainer.py`: `ApptainerRuntime` — daemonless, for clusters.
- `factory.py`: backend lookup + capability validation; also loads a custom
  backend via `RuntimeSpec.import_path`.

## The contract

A backend implements `start`, `stop`, `exec`, `upload_file`, `upload_dir`,
`download_file`, `download_dir` (plus `cancel`), hiding container details from
harnesses and evaluators. Well-known in-container paths (from `base.py`) are
`/openevo/session` and, under it, `artifacts/`, `logs/`, `logs/agent/`,
`logs/eval/`, and `eval_artifacts/`.

## Prepare recipe

`RuntimeSpec.prepare` and `RuntimeSpec.eval_prepare` are ordered lists of
`PrepareAction` steps:

- `upload_file`: copy one host file in.
- `upload_dir`: copy one host directory in.
- `exec`: run a command inside the container.

`prepare` runs before the agent. `eval_prepare` runs before evaluation — and if
it's omitted, the eval runtime simply replays `prepare`.

## Docker vs Apptainer

Docker is the default for local examples and supports `--cpus` / `--memory`
limits. Apptainer is daemonless (good for clusters that forbid the Docker
socket), uses a host-backed overlay, and exposes GPUs with `--nv`. Both
bind-mount the session directory and run commands via `bash -lc`, so harnesses
and evaluators behave the same on either.

## Container user policy

`RuntimeSpec.container_user` is a closed `image | host` choice. `image` keeps
the image's declared user and is the default for benchmark automation, custom
images, and existing experiment configs. Docker `host` starts the container
with the Core process UID/GID and therefore keeps the bind-mounted session
writable without recursively widening host file permissions.

OpenEvo-managed Science profiles use `host`; user-supplied custom images keep
`image`. Subscription admission additionally binds the profile to its exact
Core-managed image and Docker backend. It is not a general compatibility
promise that arbitrary images can run under a replaced user identity. A custom
image, loader, option/volume, entrypoint, image-user runtime, or non-literal
transcript capture mode is rejected before credential bytes are staged.

Managed Science fixes `HOME=/openevo/session/home` and a closed `PATH` beginning
with `/home/openevo/.local/bin`, where the managed image installs the pinned
Codex binary. Subscription execution invokes
`/home/openevo/.local/bin/codex` directly, so a workspace executable cannot
shadow it. Core also fixes `CODEX_HOME=/openevo/credentials/codex`, a separate
private bind mount outside `/openevo/session`. Caller `agent.env`, `runtime.env`,
and prepare-action env cannot override any of these three values.

Host-user startup, upload, stop, and failed-start cleanup never invoke the
legacy recursive `a+rwX` compatibility path. Gateway teardown instead pins the
session root device/inode/owner at dispatch, restores only owner directory
permissions through stable descriptors, and removes a bounded no-follow tree.
An owner or identity mismatch fails closed rather than acting on a replacement
path.

Docker records the immutable container ID returned by `docker create` and uses
that ID for execution and cleanup. Every stop attempt, including a successful
`rm -f`, ends with `docker container inspect <id>` and marks the runtime
destroyed only when the response proves that exact ID absent. Gateway does not
remove either bind root until that proof succeeds; cleanup ownership remains in
a private startup/shutdown retry journal otherwise.
