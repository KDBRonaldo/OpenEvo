# OpenEvo Desktop and Dev Kit Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the architecture in `docs/superpowers/specs/2026-07-08-openevo-desktop-devkit-architecture-design.md`: OpenEvo Desktop and OpenEvo Dev Kit both wrap one OpenEvo Core, with Codex-only ordinary-user science flows, capability-driven non-parametric evolution, remote lifecycle controls, artifact viewing, exact version sync, and macOS `.dmg` packaging.

**Architecture:** Build the Core contracts first, then migrate Desktop and remote lifecycle to consume those contracts. Keep historical lower-level modules as implementation details while adding OpenEvo-facing facades, capability metadata, public naming normalization, and release checks. Each task is a separate PR slice linked to issue #120.

**Tech Stack:** Python 3.11+, Pydantic v2, FastAPI, pytest, TypeScript, React, Vitest, Vite, Tauri, GitHub Actions.

Tracked by issue #120.

---

## Execution Protocol

- Base all development on `stable`.
- Use the subagent-driven workflow for implementation.
- Spawn implementation and review subagents with `gpt-5.5` and high reasoning effort.
- Keep each task as a reviewable phase. After a task's implementation and focused tests pass, run at least one fresh-context independent review subagent before merging or starting the next task.
- Do not let review subagents inherit the implementation context. They should inspect the diff and current tree from scratch.
- Commit with `ivowang <ziyiwang@ieee.org>`.
- Push completed task branches or committed `stable` work to the remote promptly after verification.
- Link PRs or commits to issue #120. Use `Part of #120` for intermediate PRs and reserve `Closes #120` for the final completion PR.
- Protect unrelated user changes. Do not use destructive git commands.

---

## Scope Split

The spec spans multiple subsystems. Implement it as a sequence of independent PRs:

1. Core capability and method metadata.
2. Execution-mode naming, reflection provider, and token metric semantics.
3. Sidecar Core API facade and artifact content access.
4. Remote lifecycle service contract.
5. Exact remote Core version install.
6. Desktop ordinary-user UI migration.
7. Tauri `.dmg` packaging and version-parity release checks.
8. Dev Kit documentation and benchmark contract alignment.
9. End-to-end smoke verification.

Do not bundle all slices into one PR. Each slice must update focused tests and docs.

## File Structure

- Create `src/openevo/core/__init__.py`: public OpenEvo Core facade exports.
- Create `src/openevo/core/capabilities.py`: execution modes, artifact targets, method metadata, capability response models.
- Modify `src/polar_evolution/methods.py`: add metadata next to the existing registry without changing worker method call signatures.
- Modify `src/openevo/science/models.py`: accept and emit public `self-deployed` mode while normalizing older values.
- Modify `src/openevo/science/compiler.py`: compile self-deployed science projects to Codex proxy harness with explicit Codex-based reflector config.
- Modify `src/openevo/experiment/compiler.py`: stop defaulting ordinary-user covered proxy/self-deployed reflection to `openai_chat`.
- Modify `src/openevo/sidecar/models.py`: expose public Desktop status fields and actual-capture token metric semantics.
- Modify `src/openevo/sidecar/api.py`: add logical capability, method, service, timeline, and artifact APIs.
- Modify `src/openevo/remote/services.py`: add status, health, logs, stop, and restart service operations.
- Modify `src/openevo/remote/bootstrap.py`: install exact matching Core version or return a failed bootstrap report.
- Modify `web/src/api/openevo.ts`: consume public `self-deployed`, capabilities, service contract, artifact content, and diagnostics models.
- Modify `web/src/routes/OpenEvoDesktop.tsx`: render ordinary-user workflow from capabilities and move raw internals into diagnostics.
- Create `web/src-tauri/`: Tauri app shell and macOS bundle metadata.
- Modify `.github/workflows/openevo-release-artifact.yml`: build and upload `.dmg`.
- Modify `scripts/ci/check_openevo_release.py`: verify Desktop/Core/Dev Kit version parity and packaged artifacts.
- Modify or create focused tests under `tests/openevo/**`, `tests/evolution/**`, and `web/src/**`.
- Update `docs/architecture/**` with Core capability, remote lifecycle, Desktop release, and Dev Kit contracts.

---

### Task 1: Core Capability and Method Metadata

**Files:**
- Create: `src/openevo/core/__init__.py`
- Create: `src/openevo/core/capabilities.py`
- Modify: `src/polar_evolution/methods.py`
- Test: `tests/openevo/test_core_capabilities.py`
- Docs: `docs/architecture/evolution-api-and-method-integration.md`

- [ ] **Step 1: Write failing capability tests**

Create `tests/openevo/test_core_capabilities.py`:

```python
from openevo.core.capabilities import (
    ArtifactTarget,
    ExecutionModeCapability,
    MethodVisibility,
    build_core_capabilities,
    method_metadata_by_id,
)


def test_core_capabilities_expose_desktop_visible_non_parametric_methods() -> None:
    capabilities = build_core_capabilities()

    target_ids = {target.artifact_type for target in capabilities.artifact_targets}
    assert {"text_memory", "skill_bundle", "agent_system"}.issubset(target_ids)
    assert "parametric_memory" not in {
        target.artifact_type
        for target in capabilities.artifact_targets
        if target.visible_in_desktop
    }

    method_ids = {
        method.method_id
        for method in capabilities.evolution_methods
        if method.visible_in_desktop
    }
    assert {"text_memory_reflector", "skill_bundle_reflector", "agent_system_reflector"}.issubset(method_ids)


def test_method_metadata_contains_required_schema_fields() -> None:
    metadata = method_metadata_by_id()
    text_memory = metadata["text_memory_reflector"]

    assert text_memory.method_id == "text_memory_reflector"
    assert text_memory.artifact_type == "text_memory"
    assert text_memory.visibility == MethodVisibility.ORDINARY_USER
    assert text_memory.supported_execution_modes == (
        "codex_subscription_transcript",
        "self-deployed",
    )
    assert text_memory.config_schema["type"] == "object"


def test_capability_models_have_stable_json_shape() -> None:
    payload = build_core_capabilities().model_dump(mode="json")

    assert payload["execution_modes"][0]["mode"] in {
        "codex_subscription_transcript",
        "self-deployed",
    }
    assert all("display_name" in item for item in payload["evolution_methods"])
    assert all("stability_level" in item for item in payload["evolution_methods"])
```

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src pytest tests/openevo/test_core_capabilities.py -q
```

Expected: fails because `openevo.core.capabilities` does not exist.

- [ ] **Step 3: Add Core capability models**

Create `src/openevo/core/__init__.py`:

```python
from openevo.core.capabilities import (
    ArtifactTarget,
    CoreCapabilities,
    EvolutionMethodCapability,
    ExecutionModeCapability,
    MethodVisibility,
    build_core_capabilities,
    method_metadata_by_id,
)

__all__ = [
    "ArtifactTarget",
    "CoreCapabilities",
    "EvolutionMethodCapability",
    "ExecutionModeCapability",
    "MethodVisibility",
    "build_core_capabilities",
    "method_metadata_by_id",
]
```

Create `src/openevo/core/capabilities.py` with frozen Pydantic models:

```python
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from polar_evolution.methods import METHOD_METADATA

ExecutionMode = Literal["codex_subscription_transcript", "self-deployed"]
ArtifactType = Literal["text_memory", "skill_bundle", "agent_system", "parametric_memory"]


class _CoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class MethodVisibility(StrEnum):
    ORDINARY_USER = "ordinary_user"
    DEV_KIT = "dev_kit"
    INTERNAL = "internal"


class ExecutionModeCapability(_CoreModel):
    mode: ExecutionMode
    display_name: str
    codex_only: bool = True
    transcript_only: bool = False
    requires_gpu: bool = False
    requires_hf_model: bool = False


class ArtifactTarget(_CoreModel):
    artifact_type: ArtifactType
    display_name: str
    visible_in_desktop: bool
    parametric: bool = False


class EvolutionMethodCapability(_CoreModel):
    method_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    artifact_type: ArtifactType
    description: str = Field(min_length=1)
    input_requirements: tuple[str, ...] = ()
    supported_execution_modes: tuple[ExecutionMode, ...]
    config_schema: dict[str, Any] = Field(default_factory=dict)
    default_config: dict[str, Any] = Field(default_factory=dict)
    stability_level: Literal["stable", "experimental", "internal"] = "experimental"
    visibility: MethodVisibility = MethodVisibility.DEV_KIT
    visible_in_desktop: bool = False


class CoreCapabilities(_CoreModel):
    execution_modes: tuple[ExecutionModeCapability, ...]
    artifact_targets: tuple[ArtifactTarget, ...]
    evolution_methods: tuple[EvolutionMethodCapability, ...]


def method_metadata_by_id() -> dict[str, EvolutionMethodCapability]:
    return {
        method_id: EvolutionMethodCapability.model_validate(payload)
        for method_id, payload in METHOD_METADATA.items()
    }


def build_core_capabilities() -> CoreCapabilities:
    metadata = tuple(method_metadata_by_id().values())
    return CoreCapabilities(
        execution_modes=(
            ExecutionModeCapability(
                mode="codex_subscription_transcript",
                display_name="Codex subscription",
                transcript_only=True,
            ),
            ExecutionModeCapability(
                mode="self-deployed",
                display_name="Self-deployed model",
                requires_gpu=True,
                requires_hf_model=True,
            ),
        ),
        artifact_targets=(
            ArtifactTarget(
                artifact_type="text_memory",
                display_name="Text memory",
                visible_in_desktop=True,
            ),
            ArtifactTarget(
                artifact_type="skill_bundle",
                display_name="Skill bundle",
                visible_in_desktop=True,
            ),
            ArtifactTarget(
                artifact_type="agent_system",
                display_name="Agent system",
                visible_in_desktop=True,
            ),
            ArtifactTarget(
                artifact_type="parametric_memory",
                display_name="Parametric memory",
                visible_in_desktop=False,
                parametric=True,
            ),
        ),
        evolution_methods=metadata,
    )
```

- [ ] **Step 4: Add legacy registry metadata**

In `src/polar_evolution/methods.py`, define `METHOD_METADATA` next to `METHOD_REGISTRY`. Start with stable Desktop-visible non-parametric methods and mark the rest Dev Kit/internal:

```python
METHOD_METADATA: dict[str, dict[str, Any]] = {
    "text_memory": {
        "method_id": "text_memory",
        "display_name": "Manual text memory",
        "artifact_type": "text_memory",
        "description": "Register text memory from explicit job configuration.",
        "input_requirements": (),
        "supported_execution_modes": ("codex_subscription_transcript", "self-deployed"),
        "config_schema": {"type": "object"},
        "default_config": {},
        "stability_level": "stable",
        "visibility": "dev_kit",
        "visible_in_desktop": False,
    },
    "text_memory_reflector": {
        "method_id": "text_memory_reflector",
        "display_name": "Reflect text memory",
        "artifact_type": "text_memory",
        "description": "Synthesize long-term text memory from task trajectories.",
        "input_requirements": ("dataset",),
        "supported_execution_modes": ("codex_subscription_transcript", "self-deployed"),
        "config_schema": {"type": "object"},
        "default_config": {},
        "stability_level": "stable",
        "visibility": "ordinary_user",
        "visible_in_desktop": True,
    },
    "skill_bundle_reflector": {
        "method_id": "skill_bundle_reflector",
        "display_name": "Reflect skill bundle",
        "artifact_type": "skill_bundle",
        "description": "Synthesize a skill bundle from task trajectories.",
        "input_requirements": ("dataset",),
        "supported_execution_modes": ("codex_subscription_transcript", "self-deployed"),
        "config_schema": {"type": "object"},
        "default_config": {},
        "stability_level": "stable",
        "visibility": "ordinary_user",
        "visible_in_desktop": True,
    },
    "agent_system_reflector": {
        "method_id": "agent_system_reflector",
        "display_name": "Reflect agent system",
        "artifact_type": "agent_system",
        "description": "Synthesize agent-system instructions from task trajectories.",
        "input_requirements": ("dataset",),
        "supported_execution_modes": ("codex_subscription_transcript", "self-deployed"),
        "config_schema": {"type": "object"},
        "default_config": {"target_path": "AGENTS.md"},
        "stability_level": "stable",
        "visibility": "ordinary_user",
        "visible_in_desktop": True,
    },
}
```

Add metadata entries for all remaining `METHOD_REGISTRY` keys before merging. Use `visibility="dev_kit"` and `visible_in_desktop=False` for experimental or parameter-related methods.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
PYTHONPATH=src pytest tests/openevo/test_core_capabilities.py tests/evolution/test_worker_methods.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/openevo/core/__init__.py src/openevo/core/capabilities.py src/polar_evolution/methods.py tests/openevo/test_core_capabilities.py docs/architecture/evolution-api-and-method-integration.md
git commit -m "feat: add openevo core capability metadata"
```

---

### Task 2: Execution Mode Naming, Reflection Provider, and Token Metrics

**Files:**
- Modify: `src/openevo/science/models.py`
- Modify: `src/openevo/science/compiler.py`
- Modify: `src/openevo/experiment/compiler.py`
- Modify: `src/openevo/sidecar/models.py`
- Modify: `src/openevo/sidecar/api.py`
- Test: `tests/openevo/science/test_science_models.py`
- Test: `tests/openevo/science/test_compiler.py`
- Test: `tests/openevo/test_experiment_compiler.py`
- Test: `tests/openevo/sidecar/test_api.py`

- [ ] **Step 1: Write failing execution-mode alias tests**

Add tests that prove public payloads emit `self-deployed` while accepting the legacy alias:

```python
from openevo.science import ScienceProjectConfig


def _base_project(execution: dict) -> dict:
    return {
        "version": 1,
        "project": {"name": "protein"},
        "remote_profile": "lab",
        "task": {"id": "fold", "objective": "Analyze folding", "source": {"type": "scratch"}},
        "execution": execution,
    }


def test_science_project_accepts_legacy_managed_local_inference_alias() -> None:
    project = ScienceProjectConfig.model_validate(
        _base_project(
            {
                "mode": "codex_managed_local_inference",
                "hf_model": "Qwen/Qwen3-8B",
            }
        )
    )

    assert project.execution.mode == "self-deployed"
    assert project.model_dump(mode="json")["execution"]["mode"] == "self-deployed"


def test_science_project_emits_public_self_deployed_mode() -> None:
    project = ScienceProjectConfig.model_validate(
        _base_project({"mode": "self-deployed", "hf_model": "Qwen/Qwen3-8B"})
    )

    assert project.execution.mode == "self-deployed"
```

- [ ] **Step 2: Write failing Codex reflector tests**

Add a compiler test:

```python
def test_self_deployed_science_compile_uses_codex_reflector_llm() -> None:
    project = ScienceProjectConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "protein"},
            "remote_profile": "lab",
            "task": {"id": "fold", "objective": "Analyze folding", "source": {"type": "scratch"}},
            "execution": {"mode": "self-deployed", "hf_model": "Qwen/Qwen3-8B"},
        }
    )

    experiment = compile_science_project(project)

    assert experiment.agent.auth == "proxy"
    assert experiment.agent.provider == "codex_cli"
```

Add an experiment compiler regression that `_reflector_llm()` or compiled job configs choose `codex_cli` when `agent.provider="codex_cli"` and never fall back to `openai_chat`.

- [ ] **Step 3: Write failing token metric status tests**

In `tests/openevo/sidecar/test_api.py`, add a status test where self-deployed status has no capture metadata:

```python
def test_desktop_status_does_not_infer_token_metrics_from_self_deployed_mode(project_factory, profile_factory) -> None:
    project = project_factory(execution={"mode": "self-deployed", "hf_model": "Qwen/Qwen3-8B"})
    app = create_sidecar_app_for_project(project, profile_factory(), transport_factory=fake_transport_factory)

    payload = client.get("/openevo-api/desktop/shell").json()

    assert payload["execution"]["mode"] == "self-deployed"
    assert payload["execution"]["token_metrics_available"] is False
```

- [ ] **Step 4: Implement alias normalization**

In `src/openevo/science/models.py`, replace the execution mode literal with a public alias type and normalize before validation:

```python
ExecutionMode = Literal["codex_subscription_transcript", "self-deployed"]
_LEGACY_EXECUTION_MODE_ALIASES = {
    "codex_managed_local_inference": "self-deployed",
}


def normalize_execution_mode(value: str) -> str:
    return _LEGACY_EXECUTION_MODE_ALIASES.get(value, value)
```

Use `normalize_execution_mode()` in `ExecutionConfig._default_subscription_codex_model()` before mode-specific checks. Update validation messages to say `self-deployed`.

- [ ] **Step 5: Compile self-deployed with explicit Codex reflection**

In `src/openevo/science/compiler.py`, update all self-deployed mode checks and set the agent provider:

```python
return {
    "preset": "codex",
    "model": project.execution.hf_model,
    "auth": "proxy",
    "provider": "codex_cli",
    "settings": {"auth_mode": "proxy"},
}
```

In `src/openevo/experiment/compiler.py`, keep `openai_chat` available for explicit Dev Kit configs, but ensure covered Science/Desktop configs with `agent.provider="codex_cli"` compile reflector jobs with `codex_cli`.

- [ ] **Step 6: Derive token metrics from evidence**

In `src/openevo/sidecar/api.py`, change `_execution_status()` so non-subscription defaults to `False` unless the session/run summary contains explicit token capture metadata:

```python
def _token_metrics_available(project: ScienceProjectConfig, summary: Mapping[str, Any] | None = None) -> bool:
    if project.execution.mode == "codex_subscription_transcript":
        return False
    capture = (summary or {}).get("capture", {})
    return bool(capture.get("token_level_metrics_available") is True)
```

Use this helper for shell status and run artifact responses.

- [ ] **Step 7: Verify GREEN**

Run:

```bash
PYTHONPATH=src pytest tests/openevo/science/test_science_models.py tests/openevo/science/test_compiler.py tests/openevo/test_experiment_compiler.py tests/openevo/sidecar/test_api.py -q
```

Expected: selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/openevo/science/models.py src/openevo/science/compiler.py src/openevo/experiment/compiler.py src/openevo/sidecar/models.py src/openevo/sidecar/api.py tests/openevo/science/test_science_models.py tests/openevo/science/test_compiler.py tests/openevo/test_experiment_compiler.py tests/openevo/sidecar/test_api.py
git commit -m "feat: normalize self-deployed execution mode"
```

---

### Task 3: Sidecar Core API Facade and Artifact Content Access

**Files:**
- Modify: `src/openevo/sidecar/api.py`
- Modify: `src/openevo/sidecar/models.py`
- Test: `tests/openevo/sidecar/test_api.py`

- [ ] **Step 1: Write failing API tests**

Add tests for:

```python
def test_sidecar_exposes_core_capabilities(client) -> None:
    payload = client.get("/openevo-api/desktop/capabilities").json()
    assert "execution_modes" in payload
    assert "evolution_methods" in payload
    assert any(method["method_id"] == "text_memory_reflector" for method in payload["evolution_methods"])


def test_sidecar_exposes_methods_alias(client) -> None:
    payload = client.get("/openevo-api/desktop/methods").json()
    assert any(method["artifact_type"] == "agent_system" for method in payload)
```

Add an artifact content test using a fake remote summary that points to a local fixture or mocked transport result:

```python
def test_artifact_content_api_returns_memory_markdown(client, fake_transport) -> None:
    fake_transport.add_file("/runs/latest/artifacts/memory.md", "# Memory\n\nUse assay controls.")

    payload = client.get("/openevo-api/desktop/artifacts/art_memory/content").json()

    assert payload["artifact_id"] == "art_memory"
    assert payload["artifact_type"] == "text_memory"
    assert payload["filename"] == "memory.md"
    assert "assay controls" in payload["content"]
```

- [ ] **Step 2: Add sidecar response models**

Add Pydantic models to `src/openevo/sidecar/models.py`:

```python
class DesktopArtifactContent(_StrictFrozenModel):
    artifact_id: str
    artifact_type: str
    filename: str
    content: str
    mime_type: str = "text/markdown"
```

- [ ] **Step 3: Add endpoints**

In `src/openevo/sidecar/api.py`, add logical API endpoints with current compatibility prefix:

```python
@app.get("/openevo-api/desktop/capabilities")
def desktop_capabilities() -> dict[str, Any]:
    return build_core_capabilities().model_dump(mode="json")


@app.get("/openevo-api/desktop/methods")
def desktop_methods() -> list[dict[str, Any]]:
    return [
        method.model_dump(mode="json")
        for method in build_core_capabilities().evolution_methods
    ]
```

Implement artifact content lookup by reading the latest summary, resolving the artifact id to a safe file path, and using the remote transport to read content. Reject path traversal and non-text artifacts with structured HTTP errors.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src pytest tests/openevo/sidecar/test_api.py tests/openevo/test_core_capabilities.py -q
```

Expected: selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/sidecar/api.py src/openevo/sidecar/models.py tests/openevo/sidecar/test_api.py
git commit -m "feat: expose desktop core capabilities and artifacts"
```

---

### Task 4: Remote Lifecycle Service Contract

**Files:**
- Modify: `src/openevo/remote/services.py`
- Modify: `src/openevo/remote/lifecycle.py`
- Modify: `src/openevo/sidecar/api.py`
- Test: `tests/openevo/remote/test_services.py`
- Test: `tests/openevo/remote/test_lifecycle.py`
- Test: `tests/openevo/sidecar/test_api.py`
- Docs: `docs/architecture/openevo-desktop-remote-bootstrap-lifecycle-foundation.md`

- [ ] **Step 1: Write failing service operation tests**

In `tests/openevo/remote/test_services.py`, add tests for:

```python
def test_service_status_reads_pid_and_health() -> None:
    status = inspect_remote_services(fake_transport, topology)
    assert status.services["gateway"].state in {"running", "ready", "failed", "unknown"}


def test_service_logs_reads_tail_with_redaction() -> None:
    logs = read_remote_service_logs(fake_transport, topology, service_id="gateway", lines=50)
    assert logs.service_id == "gateway"
    assert "Authorization" not in logs.content


def test_stop_remote_service_kills_pid_file_process() -> None:
    result = stop_remote_service(fake_transport, topology, service_id="gateway")
    assert result.service_id == "gateway"
    assert result.state == "stopped"
```

- [ ] **Step 2: Add lifecycle models**

In `src/openevo/remote/lifecycle.py`, add:

```python
class RemoteServiceState(StrEnum):
    PLANNED = "planned"
    STARTING = "starting"
    RUNNING = "running"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RemoteServiceLog(BaseModel):
    service_id: str
    content: str
    line_count: int
```

- [ ] **Step 3: Implement service facade**

In `src/openevo/remote/services.py`, implement:

```python
def inspect_remote_services(
    transport: RemoteExecutorTransport,
    topology: ServiceTopology,
) -> RemoteServicesStatus:
    statuses = [_inspect_one_service(transport, service) for service in topology.services]
    return RemoteServicesStatus(services=tuple(statuses))


def read_remote_service_logs(
    transport: RemoteExecutorTransport,
    topology: ServiceTopology,
    *,
    service_id: str,
    lines: int = 200,
) -> RemoteServiceLog:
    service = _service_by_id(topology, service_id)
    result = transport.run(f"tail -n {int(lines)} {shlex.quote(service.log_path)}")
    return RemoteServiceLog(
        service_id=service_id,
        content=redact_remote_text(result.stdout),
        line_count=len(result.stdout.splitlines()),
    )


def stop_remote_service(
    transport: RemoteExecutorTransport,
    topology: ServiceTopology,
    *,
    service_id: str,
) -> RemoteServiceOperationResult:
    service = _service_by_id(topology, service_id)
    result = transport.run(
        "sh -lc "
        + shlex.quote(
            f"test -f {shlex.quote(service.pid_path)} && "
            f"kill $(cat {shlex.quote(service.pid_path)}) && "
            f"rm -f {shlex.quote(service.pid_path)}"
        )
    )
    return RemoteServiceOperationResult(
        service_id=service_id,
        state="stopped" if result.return_code == 0 else "failed",
        message=result.stderr or result.stdout,
    )
```

Implement `restart_remote_service()` by calling `stop_remote_service()` followed by the existing service start command for that service id. The restart result must include the service id, final state, and redacted stdout/stderr summary.

Use existing pid/log paths written by service startup. Return structured failures instead of throwing for missing pid files.

- [ ] **Step 4: Add sidecar endpoints**

In `src/openevo/sidecar/api.py`, add:

```text
GET  /openevo-api/desktop/services/status
GET  /openevo-api/desktop/services/health
GET  /openevo-api/desktop/services/logs
POST /openevo-api/desktop/services/stop
POST /openevo-api/desktop/services/restart
```

Keep existing aggregate `/openevo-api/desktop/services` for compatibility.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
PYTHONPATH=src pytest tests/openevo/remote/test_services.py tests/openevo/remote/test_lifecycle.py tests/openevo/sidecar/test_api.py -q
```

Expected: selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/openevo/remote/services.py src/openevo/remote/lifecycle.py src/openevo/sidecar/api.py tests/openevo/remote/test_services.py tests/openevo/remote/test_lifecycle.py tests/openevo/sidecar/test_api.py docs/architecture/openevo-desktop-remote-bootstrap-lifecycle-foundation.md
git commit -m "feat: add remote service lifecycle controls"
```

---

### Task 5: Exact Remote Core Version Installation

**Files:**
- Modify: `src/openevo/remote/bootstrap.py`
- Modify: `src/openevo/sidecar/api.py`
- Modify: `scripts/ci/check_openevo_release.py`
- Test: `tests/openevo/remote/test_bootstrap.py`
- Test: `tests/openevo/sidecar/test_api.py`

- [ ] **Step 1: Write failing exact-version tests**

In `tests/openevo/remote/test_bootstrap.py`, add:

```python
def test_bootstrap_uploads_exact_core_wheel_when_available(tmp_path) -> None:
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    report = ensure_remote_openevo_cli(fake_transport, expected_version="0.1.0", bundled_wheel=wheel)

    assert report.status == "pass"
    assert any("pip install --user" in step.command and str(wheel.name) in step.command for step in report.steps)


def test_bootstrap_fails_instead_of_installing_latest_when_exact_version_missing() -> None:
    report = ensure_remote_openevo_cli(fake_transport, expected_version="0.1.0", bundled_wheel=None)

    assert report.status == "fail"
    assert "exact OpenEvo Core version" in report.message
    assert "pip install --user --upgrade openevo" not in " ".join(step.command for step in report.steps if step.command)
```

- [ ] **Step 2: Implement exact install contract**

In `src/openevo/remote/bootstrap.py`, extract the existing `ensure_openevo_cli` step into a helper with this behavior:

```python
def ensure_remote_openevo_cli_step(
    *,
    expected_version: str,
    bundled_wheel_remote_path: str | None,
    proxy_env: dict[str, str],
) -> RemoteBootstrapStep:
    version_check = "python3 -m openevo --version"
    if bundled_wheel_remote_path is None:
        command = "\n".join(
            [
                version_check,
                "python3 - <<'PY'",
                f"raise SystemExit('Remote OpenEvo Core must be {expected_version}; no bundled wheel was available')",
                "PY",
            ]
        )
    else:
        command = "\n".join(
            [
                f"python3 -m pip install --user --force-reinstall {shlex.quote(bundled_wheel_remote_path)}",
                version_check,
            ]
        )
    return RemoteBootstrapStep(
        id="ensure_openevo_cli",
        kind=RemoteBootstrapStepKind.CHECK_COMMAND,
        command=command,
        env=proxy_env,
        timeout_seconds=300.0,
        network=True,
        required=True,
        remediation_kind="upload_exact_openevo_wheel",
        manifest={"expected_version": expected_version},
    )
```

The command runner must verify the installed version after the step runs:

```python
remote_version = _remote_openevo_version(transport)
if remote_version == expected_version:
    return pass_report
return fail_report(
    message=f"Remote OpenEvo Core must be {expected_version}; found {remote_version or 'unknown'}.",
    remediation_kind="upload_exact_openevo_wheel",
)
```

Do not silently run `pip install --upgrade openevo` without a pinned version or wheel.

- [ ] **Step 3: Add release parity check**

In `scripts/ci/check_openevo_release.py`, verify:

```text
pyproject version == packaged sidecar version
web package version or bundle metadata agrees with Python version
release artifact includes an OpenEvo wheel used for remote exact install
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src pytest tests/openevo/remote/test_bootstrap.py tests/openevo/sidecar/test_api.py -q
python scripts/ci/check_openevo_release.py --help
```

Expected: selected tests pass and release checker CLI loads.

- [ ] **Step 5: Commit**

```bash
git add src/openevo/remote/bootstrap.py src/openevo/sidecar/api.py scripts/ci/check_openevo_release.py tests/openevo/remote/test_bootstrap.py tests/openevo/sidecar/test_api.py
git commit -m "feat: require exact remote openevo core install"
```

---

### Task 6: Desktop Capability-Driven Ordinary-User UI

**Files:**
- Modify: `web/src/api/openevo.ts`
- Modify: `web/src/api/openevo.test.ts`
- Modify: `web/src/routes/openevoDesktopModel.ts`
- Modify: `web/src/routes/openevoDesktopModel.test.ts`
- Modify: `web/src/routes/OpenEvoDesktop.tsx`
- Modify: `web/src/routes/OpenEvoDesktop.test.tsx`

- [ ] **Step 1: Write failing API/model tests**

Add tests proving:

```typescript
expect(normalizeExecutionMode("codex_managed_local_inference")).toBe("self-deployed");
expect(toDraftPayload(model).execution_mode).toBe("self-deployed");
expect(renderedEvolutionTargets).toEqual(["Text memory", "Skill bundle", "Agent system"]);
```

Add a render test that raw `Run ID`, `Output dir`, `stdout`, `stderr`, method ids, and artifact ids are hidden until the diagnostics disclosure is opened.

- [ ] **Step 2: Add TypeScript capability models**

In `web/src/api/openevo.ts`, add:

```typescript
export type OpenEvoExecutionMode = "codex_subscription_transcript" | "self-deployed";

export interface EvolutionMethodCapability {
  method_id: string;
  display_name: string;
  artifact_type: "text_memory" | "skill_bundle" | "agent_system" | "parametric_memory";
  supported_execution_modes: OpenEvoExecutionMode[];
  visible_in_desktop: boolean;
  stability_level: "stable" | "experimental" | "internal";
}
```

Fetch `/openevo-api/desktop/capabilities` and render targets from returned metadata.

- [ ] **Step 3: Update Desktop UI**

In `web/src/routes/OpenEvoDesktop.tsx`:

- Replace hardcoded execution label `codex_managed_local_inference` with `self-deployed`.
- Render model/HF fields only when `execution_mode === "self-deployed"`.
- Render model mirror/HF endpoint fields only for self-deployed mode.
- Render evolution target controls from capabilities.
- Move raw command/stdout/stderr/artifact id/method id blocks into a `<details>` element labeled `Diagnostics`.
- Add artifact content panel that calls the artifact content API and displays Markdown text.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
cd web && npm test -- --run src/api/openevo.test.ts src/routes/openevoDesktopModel.test.ts src/routes/OpenEvoDesktop.test.tsx
```

Expected: selected Vitest tests pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/openevo.ts web/src/api/openevo.test.ts web/src/routes/openevoDesktopModel.ts web/src/routes/openevoDesktopModel.test.ts web/src/routes/OpenEvoDesktop.tsx web/src/routes/OpenEvoDesktop.test.tsx
git commit -m "feat: drive desktop science UI from core capabilities"
```

---

### Task 7: Tauri macOS Desktop Packaging

**Files:**
- Create: `web/src-tauri/Cargo.toml`
- Create: `web/src-tauri/tauri.conf.json`
- Create: `web/src-tauri/src/main.rs`
- Modify: `web/package.json`
- Modify: `.github/workflows/openevo-release-artifact.yml`
- Modify: `scripts/ci/check_openevo_release.py`
- Docs: `docs/architecture/openevo-desktop-release.md`

- [ ] **Step 1: Add package scripts and skeleton tests**

In `web/package.json`, add scripts:

```json
{
  "tauri:dev": "tauri dev",
  "tauri:build": "tauri build",
  "build:desktop": "npm run build && npm run tauri:build"
}
```

Add a release checker test that fails when the artifact list does not include `.dmg`.

- [ ] **Step 2: Add minimal Tauri shell**

Create `web/src-tauri/tauri.conf.json` with:

```json
{
  "productName": "OpenEvo Desktop",
  "version": "0.1.0",
  "identifier": "org.openevo.desktop",
  "build": {
    "beforeBuildCommand": "npm run build",
    "frontendDist": "../dist"
  },
  "bundle": {
    "active": true,
    "targets": ["dmg"],
    "macOS": {
      "minimumSystemVersion": "12.0"
    }
  }
}
```

Create `web/src-tauri/src/main.rs`:

```rust
fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running OpenEvo Desktop");
}
```

- [ ] **Step 3: Update release workflow**

In `.github/workflows/openevo-release-artifact.yml`, add macOS build job steps:

```yaml
- uses: actions/setup-node@v4
  with:
    node-version: 20
- uses: dtolnay/rust-toolchain@stable
- run: npm ci
  working-directory: web
- run: npm run build:desktop
  working-directory: web
- uses: actions/upload-artifact@v4
  with:
    name: openevo-desktop-dmg
    path: web/src-tauri/target/release/bundle/dmg/*.dmg
```

- [ ] **Step 4: Verify package commands**

Run on a macOS-capable CI runner or local macOS builder:

```bash
cd web && npm run build
```

Expected: Vite build succeeds. The `.dmg` build is verified in GitHub Actions macOS CI.

- [ ] **Step 5: Commit**

```bash
git add web/package.json web/src-tauri .github/workflows/openevo-release-artifact.yml scripts/ci/check_openevo_release.py docs/architecture/openevo-desktop-release.md
git commit -m "feat: add tauri desktop release packaging"
```

---

### Task 8: Dev Kit and Benchmark Contract Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture/evolution-api-and-method-integration.md`
- Create: `docs/architecture/openevo-dev-kit.md`
- Modify: `src/openevo/cli.py`
- Test: `tests/openevo/test_cli.py`

- [ ] **Step 1: Add CLI/docs tests**

Add tests that `openevo --help` and relevant subcommands use OpenEvo Core/Desktop/Dev Kit terminology and do not expose the historical repo name as product branding.

- [ ] **Step 2: Add Dev Kit doc**

Create `docs/architecture/openevo-dev-kit.md` with:

```markdown
# OpenEvo Dev Kit

OpenEvo Dev Kit is the developer wrapper around OpenEvo Core. It includes CLI entrypoints, Python-facing Core facades, benchmark adapters, method development helpers, artifact inspection, and regression-test fixtures.

Desktop is not a developer console. Developer benchmark work should use Dev Kit and should produce Core records, datasets, metrics, jobs, artifacts, and context inputs.
```

Document the method metadata lifecycle from Task 1 and the benchmark adapter rule from the spec.

- [ ] **Step 3: Verify GREEN**

Run:

```bash
PYTHONPATH=src pytest tests/openevo/test_cli.py -q
```

Expected: CLI tests pass.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/architecture/evolution-api-and-method-integration.md docs/architecture/openevo-dev-kit.md src/openevo/cli.py tests/openevo/test_cli.py
git commit -m "docs: document openevo dev kit contract"
```

---

### Task 9: End-to-End Smoke and Completion Audit

**Files:**
- Modify: `scripts/ci/smoke_openevo_desktop_wheel.py`
- Modify: `.github/workflows/openevo-release-smoke.yml`
- Modify: `tests/openevo/sidecar/test_api.py`
- Modify: `web/src/api/openevo.test.ts`

- [ ] **Step 1: Add smoke fixture**

Add a local fixture that exercises:

```text
GET /openevo-api/desktop/capabilities
GET /openevo-api/desktop/methods
GET /openevo-api/desktop/shell
POST /openevo-api/desktop/project
GET /openevo-api/desktop/services/status
GET /openevo-api/desktop/run/artifacts
GET /openevo-api/desktop/artifacts/{id}/content
```

- [ ] **Step 2: Add spec checklist test**

Create a test helper that verifies the sidecar status payload satisfies the spec’s ordinary-user invariants:

```python
def assert_desktop_science_payload(payload: dict[str, object]) -> None:
    assert payload["execution"]["mode"] in {"codex_subscription_transcript", "self-deployed"}
    assert "token_metrics_available" in payload["execution"]
    assert "capabilities" not in payload.get("diagnostics", {})
```

- [ ] **Step 3: Run full focused verification**

Run:

```bash
PYTHONPATH=src pytest tests/openevo tests/evolution/test_worker_methods.py -q
cd web && npm test -- --run src/api/openevo.test.ts src/routes/OpenEvoDesktop.test.tsx src/routes/openevoDesktopModel.test.ts
git diff --check
```

Expected: all selected tests pass and diff check is clean.

- [ ] **Step 4: Completion audit**

Before marking issue #120 complete, verify each acceptance criterion against current evidence:

```text
Core capability API: tests/openevo/test_core_capabilities.py
Desktop capability rendering: web route tests
self-deployed public naming: science model and web API tests
Codex reflection path: science/compiler and experiment/compiler tests
token metric semantics: sidecar tests
remote lifecycle: remote services and sidecar tests
exact remote Core install: bootstrap tests and release checks
artifact content viewer: sidecar and web tests
ordinary-user diagnostics: Desktop render tests
Tauri release: GitHub Actions artifact and release checker
docs: architecture docs updated
```

- [ ] **Step 5: Commit**

```bash
git add scripts/ci/smoke_openevo_desktop_wheel.py .github/workflows/openevo-release-smoke.yml tests/openevo/sidecar/test_api.py web/src/api/openevo.test.ts
git commit -m "test: add openevo desktop architecture smoke coverage"
```

---

## Final Verification Commands

Run these before closing #120:

```bash
PYTHONPATH=src pytest tests/openevo tests/evolution/test_worker_methods.py tests/evolution/test_terminal_bench_bridge.py tests/evolution/test_terminal_bench_per_task.py -q
cd web && npm test -- --run
git diff --check
ruff check src/openevo tests/openevo
```

The macOS `.dmg` build must also pass in GitHub Actions on a macOS runner.

## Plan Self-Review

- Spec coverage: each acceptance criterion in the spec maps to at least one task above.
- Issue link: all implementation PRs should reference issue #120 with `Part of #120` until the final PR closes it.
- Scope control: parameter evolution algorithms, dynamic adapter loading, extra harnesses, benchmark dashboards, and multi-user cloud mode are intentionally out of scope.
- Migration rule: old internal names may be accepted at boundaries only when normalized to public OpenEvo terminology.
