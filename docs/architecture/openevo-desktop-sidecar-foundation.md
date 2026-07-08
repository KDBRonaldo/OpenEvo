# OpenEvo Desktop Sidecar Foundation

Tracked by #39.

This document defines the first Desktop sidecar contract for OpenEvo Science
projects. The sidecar layer is the local application/backend boundary that turns
a user-selected Science Project and remote profile into a deterministic dry-run
plan. That plan can be displayed by OpenEvo Desktop, passed to a future remote
executor, and compiled into the existing OpenEvo/Polar experiment contract.

The foundation plan layer does not run SSH, upload files, start Docker, start
vLLM, store secrets, or render UI. It validates configuration and produces the
plan consumed by the Desktop lifecycle endpoints. Later sidecar layers now use
that plan to execute workspace preparation, remote bootstrap, command-based
service startup, and run launch when `openevo sidecar serve --transport ssh` is
selected.

## Boundary

The sidecar consumes:

- a Science Project YAML loaded through `load_science_project_config`;
- a remote profile YAML loaded through `load_remote_profile_config`.

It produces a `SidecarSciencePlan` containing:

- `proxy_env`: immutable environment variables to apply on the remote side;
- `preflight`: `RemotePreflightSettings` for remote capability checks;
- `workspace`: a `WorkspacePreparationPlan`;
- `experiment`: an immutable JSON snapshot of the compiled `ExperimentConfig`.

The `experiment` field is intentionally a JSON snapshot, not a mutable
`ExperimentConfig` instance. Desktop and remote executors should treat it as a
handoff payload. If a later layer needs a model instance, it can validate the
snapshot back into `ExperimentConfig`.

## Remote Profile

Remote profiles describe how OpenEvo Desktop should identify a remote GPU
server. The foundation schema includes:

- `id`: stable profile id, which must match `science.remote_profile`;
- `host`, `port`, and `user`;
- `workspace_root`, defaulting to `/home/<user>/.openevo/workspaces`;
- `min_home_available_kb`, used by preflight disk checks;
- `auth`, which stores only secret references or local key paths;
- `proxy`, which renders remote environment variables.

Authentication modes are intentionally reference-only:

- `ssh_agent`: use the user's local SSH agent.
- `private_key`: points at a local private-key path and may include a
  `passphrase_ref`.
- `password_ref`: points at a password reference such as a keyring or future
  vault id.

The schema forbids raw secret fields such as `password` or `private_key`. A
future vault implementation can resolve references outside this model without
changing the profile contract.
Desktop shell status returns the same non-secret setup surface for the active
profile: host, port, user, auth method and reference ids, effective workspace
root, and proxy/mirror fields. The saved-config catalog remains more restrictive
and does not expose private-key paths or secret references.
For Desktop-created Science configs, the setup draft also includes the Science
execution mode plus exactly one mode-specific model field: `codex_model` for
`codex_subscription_transcript`, or Hugging Face `hf_model` for
`self-deployed`. The legacy draft value
`codex_managed_local_inference` is accepted only as an input alias and is
normalized to `self-deployed` in saved Science YAML and API responses. The
sidecar validates this draft through the same `ScienceProjectConfig` schema
used by hand-authored YAML.

Desktop shell status also returns `sidecar.transport` for the active local
sidecar process. That object is capability metadata, not a credential surface:
it includes the selected transport id and label plus
`supports_password_ref`/`supports_passphrase_ref` booleans. The Desktop UI uses
those booleans to keep future vault-compatible profile schemas round-trippable
while blocking lifecycle actions that the current transport cannot execute. The
sidecar mutating endpoints enforce the same check and return `409` before any
remote command runs.

## Proxy And Mirrors

The profile proxy settings support remote servers behind restricted networks or
mainland China firewalls. `ProxySettings.to_env()` renders:

- `HTTP_PROXY` and `http_proxy`;
- `HTTPS_PROXY` and `https_proxy`;
- `NO_PROXY` and `no_proxy`;
- `PIP_INDEX_URL`;
- `HF_ENDPOINT`;
- `HF_HOME`;
- arbitrary validated `extra_env` values.

Explicit proxy fields override conflicting `extra_env` keys. The
`docker_registry_mirror` field is validated and carried in the profile, but this
foundation slice does not configure the Docker daemon; the future remote
executor should consume it when it owns installation or daemon setup.

## Workspace Preparation

`plan_workspace_preparation()` maps Science Project task sources to deterministic
remote workspace actions:

| Source type | Sidecar action | Behavior |
|---|---|---|
| `local_folder` | `upload_dir` | Resolve the local path relative to the Science YAML, compute a source fingerprint, and target `<workspace_root>/<project>/<task>/<hash>`. |
| `git_repository` | `git_clone` | Record a shell-quoted `git clone --depth 1 ... -- <url> <target>` command and deterministic target path. |
| `remote_path` | `use_remote_path` | Use an existing absolute remote path as the prepared workspace. |
| `scratch` | none | Run without a prepared workspace. |

Workspace actions are dry-run descriptions. The sidecar does not perform the
upload or clone at plan time. The Desktop workspace lifecycle endpoint consumes
the same actions to upload local folders, clone git repositories, accept remote
paths, or leave scratch tasks empty.
`WorkspacePreparationPlan.to_prepared_workspaces()` converts actions into the
`PreparedWorkspace` mapping required by `compile_science_project()`.

Remote paths must be absolute. Relative local folder paths are resolved against
the Science Project file location when available, otherwise the current process
working directory.

## Preflight Mapping

`preflight_settings_for_project()` connects the user-facing Science execution
mode to remote preflight checks:

- `codex_subscription_transcript` sets
  `require_codex_subscription=true`. Remote preflight should check Codex CLI and
  subscription login after SSH, Docker, GPU, and disk checks.
- `self-deployed` sets
  `require_codex_subscription=false`. Remote preflight still checks the base
  remote capabilities, but Codex subscription login is not required.

Both modes carry `min_home_available_kb` from the remote profile.

## CLI

The dry-run CLI is:

```bash
openevo sidecar plan science.yaml --remote-profile remote.yaml --json
```

The JSON output is the same payload OpenEvo Desktop can render before execution:

- workspace actions to show uploads, clones, or existing paths;
- proxy environment that will be applied remotely;
- preflight requirements;
- compiled experiment snapshot for the OpenEvo/Polar backend.

Without `--json`, the same payload is printed as YAML for manual inspection.

## Desktop Lifecycle Consumption

OpenEvo Desktop keeps the foundation plan as the source of truth for mutating
sidecar endpoints:

- `GET /openevo-api/desktop/capabilities` exposes Core execution-mode,
  artifact-target, and evolution-method metadata. It is read-only and does not
  require a sidecar mutation token or active project session.
- `GET /openevo-api/desktop/methods` is a read-only catalog alias that returns
  `{"methods": [...]}` using the same evolution-method capability objects from
  Core.
- `POST /openevo-api/desktop/workspace` consumes the workspace preparation plan.
- `POST /openevo-api/desktop/bootstrap` compiles the experiment snapshot and
  prepares the remote user-site OpenEvo CLI, state root, and optional Hugging
  Face model snapshot.
- `POST /openevo-api/desktop/services` writes a deterministic topology file
  under the bootstrap state root, then starts and health-checks the remote
  evolution backend, rollout, gateway, evolution worker, and managed vLLM when
  the execution mode requires local inference.
- `POST /openevo-api/desktop/run` launches `openevo run` only after workspace,
  bootstrap, and services are all ready.
- `GET /openevo-api/desktop/run/artifacts` reads the latest terminal remote
  run's `summary.json` and returns a compact task/round/job artifact summary.
- `GET /openevo-api/desktop/artifacts/{artifact_id}/content` reads one
  human-readable text artifact from that latest terminal run summary. It
  requires the sidecar mutation token and an active config-backed session. The
  sidecar first confirms the artifact id appears in the latest run summary, then
  reads artifact metadata from the remote evolution backend registry. It supports
  `file://` artifact URIs, accepts only relative in-artifact content paths, and
  returns markdown/text content for `text_memory`, `skill_bundle`, and
  `agent_system`. It rejects non-file URI schemes, path traversal, active runs,
  missing runs, unknown registry artifacts, and unsupported binary artifacts
  instead of accepting arbitrary request-supplied file paths.

The service supervisor is intentionally command based. It exports the remote
profile proxy/PIP/Hugging Face environment for the full remote command script,
records pid files, writes stdout/stderr logs under the remote state root, and
polls local health URLs or worker pids before reporting readiness. Managed vLLM
startup gets a longer readiness window and verifies that `/v1/models` contains
the configured Hugging Face model id. Service reports redact proxy, pip index,
and URL userinfo credentials before returning stdout, stderr, or exception text
to Desktop. It is not a Docker Compose replacement and does not provide
restart-on-crash, process-group cancellation, tunnel management, GPU sizing, or
dynamic adapter lifecycle.

## Limitations

This foundation slice does not include:

- local credential vault or keychain integration;
- password-based SSH auth or private-key passphrase resolution;
- full remote installation or dependency repair beyond later bootstrap layers'
  user-site Python package checks;
- Docker daemon or Docker Compose lifecycle management;
- production vLLM/model serving tuning, restart policy, or adapter loading;
- runtime image build/push/upload;
- dynamic adapter or parametric-memory lifecycle.

Those layers should consume this contract instead of duplicating Science Project
parsing or workspace target derivation.
