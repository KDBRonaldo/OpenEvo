#!/usr/bin/env python3
"""Smoke installed OpenEvo Core with the source Desktop harness, not a packaged app."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import posixpath
import sys
from tempfile import TemporaryDirectory
import time
from typing import Any
from urllib.parse import urlsplit
import warnings

from openevo import __version__ as OPENEVO_VERSION
from openevo.experiments import compile_experiment
from openevo.evolution.framework import (
    CapabilityAudience,
    EvolutionExecutionProfile,
    FrameworkDistributionLock,
    build_evolution_capabilities,
    execution_profile_for_release_mode,
)
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)
from openevo.projects.science import compile_science_project, load_science_project_config
from openevo.harness.models import AgentSpec
from openevo.harness.presets.codex import CodexHarness
from desktop.server.app import create_desktop_app
from openevo.deployment import RemoteCommandResult
from desktop.sidecar.api import create_sidecar_app
from desktop.sidecar.api import SIDECAR_MUTATION_TOKEN_HEADER

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)

from fastapi.testclient import TestClient  # noqa: E402


class SmokeFailure(RuntimeError):
    """Raised when the installed Desktop smoke path does not satisfy release gates."""


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name not in {"href", "src"} or value is None:
                continue
            asset = _asset_reference(value)
            if asset is not None:
                self.assets.append(asset)


def _asset_reference(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    path = parsed.path
    if path.startswith("/assets/"):
        path = path[1:]
    elif not path.startswith("assets/"):
        return None
    normalized = posixpath.normpath(path)
    if normalized == "assets" or not normalized.startswith("assets/"):
        raise ValueError(f"Invalid Desktop asset reference: {value}")
    return normalized


def _asset_references(index_html: str) -> list[str]:
    parser = _AssetParser()
    parser.feed(index_html)
    return sorted(set(parser.assets))


class _LifecycleSmokeTransport:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.uploaded_framework_lock: FrameworkDistributionLock | None = None

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append(command)
        if command == 'df -Pk "$HOME"':
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=(
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/root 100000000 1 99999999 1% /home\n"
                ),
            )
        if "importlib.metadata" in command and "version('openevo')" in command:
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=f"{OPENEVO_VERSION}\n",
            )
        if "openevo-backend --version" in command:
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=f"openevo {OPENEVO_VERSION}\n",
            )
        if "openevo-backend --help" in command:
            return RemoteCommandResult(command=command, return_code=0, stdout="help")
        if "json.dumps" in command and "pid_path =" in command:
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=json.dumps(
                    {
                        "pid_exists": True,
                        "pid": 120,
                        "alive": True,
                    }
                ),
            )
        if command.startswith('PATH="$HOME/.local/bin:$PATH" openevo-backend run '):
            return RemoteCommandResult(command=command, return_code=0, stdout="ok")
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        staging = Path(local_path)
        wheels = list(staging.glob("openevo-*.whl"))
        if len(wheels) != 1:
            raise SmokeFailure("Desktop bootstrap did not stage one exact Core wheel.")
        lock_path = staging / "framework-lock.json"
        lock = FrameworkDistributionLock.model_validate_json(
            lock_path.read_text(encoding="utf-8")
        )
        digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
        if lock.wheel_filename != wheels[0].name or lock.distribution_digest != digest:
            raise SmokeFailure("Desktop framework lock did not bind the staged Core wheel.")
        self.uploaded_framework_lock = lock
        self.uploads.append((local_path, remote_path))


class _SmokeBackendClient:
    def __init__(self) -> None:
        self.validation_requests: list[dict[str, Any]] = []

    @staticmethod
    def _snapshot():
        return build_builtin_registry(
            ImplementationDistributionIdentity(
                distribution="openevo-smoke",
                distribution_version=OPENEVO_VERSION,
                distribution_digest="d" * 64,
            )
        )

    def capabilities(self, execution_mode: str) -> dict[str, Any]:
        return build_evolution_capabilities(
            self._snapshot(),
            profile=execution_profile_for_release_mode(execution_mode),
            audience=CapabilityAudience.DESKTOP,
            core_version=OPENEVO_VERSION,
        ).model_dump(mode="json")

    def validate_evolution_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.validation_requests.append(payload)
        return {
            "valid": True,
            "registry_digest": self._snapshot().registry_digest,
        }

    def run_timeline(self, run_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": f"{run_id}-memory",
                "phase": "evolution",
                "title": "Memory updated",
                "message": "Text memory worker promoted one artifact.",
                "artifact_ids": ["artifact-text-memory"],
            }
        ]

    def run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "artifact-text-memory",
                "run_id": run_id,
                "artifact_type": "text_memory",
                "title": "Initial memory draft",
                "promoted": True,
                "lineage": {
                    "method": "text_memory_reflector",
                    "dataset_id": "dataset-release-smoke",
                },
            }
        ]

    def artifact_content(self, artifact_id: str) -> dict[str, Any]:
        return {
            "id": artifact_id,
            "artifact_type": "text_memory",
            "content": "# Learned Memory\n\n- Prefer stable folds.\n",
            "metadata": {
                "target_path": "memory.md",
                "lineage": {"method": "text_memory_reflector"},
            },
        }

    def artifact_diff(self, artifact_id: str) -> dict[str, Any]:
        return {
            "id": artifact_id,
            "before": "",
            "after": "# Learned Memory\n\n- Prefer stable folds.\n",
            "format": "unified_text",
        }


def main() -> int:
    transport = _LifecycleSmokeTransport()
    backend = _SmokeBackendClient()
    try:
        with TemporaryDirectory(prefix="openevo-desktop-smoke-") as config_root:
            app = create_desktop_app(
                create_sidecar_app(
                    config_root=Path(config_root),
                    transport_factory=lambda _profile: transport,
                    backend_client_factory=lambda: backend,
                )
            )
            with TestClient(app) as client:
                assets = _smoke_packaged_assets(client)
                _smoke_config_backed_lifecycle(client, transport, backend)
    except SmokeFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        "Installed Core + source Desktop harness smoke passed for "
        f"{len(assets)} asset(s)."
    )
    print("Source Desktop config-backed lifecycle harness passed.")
    return 0


def _smoke_packaged_assets(client: TestClient) -> list[str]:
    index = client.get("/openevo")
    _require_status(index, "/openevo")
    assets = _asset_references(index.text)
    if not assets:
        raise SmokeFailure("/openevo did not reference any packaged assets")
    for asset in assets:
        response = client.get(f"/{asset}")
        _require_status(response, f"/{asset}")
    return assets


def _smoke_config_backed_lifecycle(
    client: TestClient,
    transport: _LifecycleSmokeTransport,
    backend: _SmokeBackendClient,
) -> None:
    headers = _mutation_headers(client)
    capabilities_path = (
        "/openevo-api/desktop/capabilities"
        "?execution_mode=codex_subscription_transcript"
    )
    capabilities = _get_json(client, capabilities_path, headers=headers)
    method_ids = {
        method["method_id"]
        for target in capabilities["targets"]
        for method in target["methods"]
    }
    if not {
        "text_memory_expel_reflector",
        "skill_bundle_reflector",
        "agent_system_gepa_reflector",
    }.issubset(method_ids):
        raise SmokeFailure(
            "Remote registry capabilities did not expose release science methods."
        )

    draft = _desktop_config_draft_payload()
    config = _post_json(
        client,
        "/openevo-api/desktop/project-config",
        headers=headers,
        json=draft,
    )
    if config["status"]["project"]["name"] != "Release Smoke Science":
        raise SmokeFailure("Desktop project config response did not load submitted project.")
    if config["status"]["project"]["evolution_targets"] != draft["evolution"][
        "targets"
    ]:
        raise SmokeFailure("Desktop project config response changed evolution targets.")
    science_project = load_science_project_config(
        Path(config["config"]["science_config_path"])
    )
    compiled = compile_experiment(
        compile_science_project(science_project),
        registry_snapshot=build_builtin_registry(
            ImplementationDistributionIdentity(
                distribution="openevo-smoke",
                distribution_version=OPENEVO_VERSION,
                distribution_digest="a" * 64,
            )
        ),
        execution_profile=EvolutionExecutionProfile(
            execution_mode="subscription",
            capture_mode="transcript",
            harness_id="codex",
        ),
    )
    compiled_task = compiled.tasks[0]
    if compiled_task.agent["settings"].get("reasoning_effort") != draft[
        "reasoning_effort"
    ]:
        raise SmokeFailure("Desktop saved reasoning effort did not reach Core compilation.")
    codex_command = CodexHarness(
        AgentSpec.model_validate(compiled_task.agent)
    ).run_steps("Validate release configuration.")[0].command
    expected_effort = f"-c model_reasoning_effort={draft['reasoning_effort']}"
    if (
        f"--model {draft['codex_model']}" not in codex_command
        or expected_effort not in codex_command
    ):
        raise SmokeFailure("Compiled Codex command did not preserve model and reasoning effort.")
    compiled_methods = {
        spec.method
        for spec in compiled.evolution_methods_for_round(
            0,
            prior_dataset_artifact_ids=[],
        )
    }
    if "future_target" in compiled_methods or compiled_methods != {
        "text_memory_reflector",
        "skill_bundle_reflector",
        "agent_system_reflector",
    }:
        raise SmokeFailure("Desktop saved targets did not compile as expected.")

    catalog = _get_json(client, "/openevo-api/desktop/project-configs")
    if len(catalog["configs"]) != 1 or catalog["configs"][0]["valid"] is not True:
        raise SmokeFailure("Desktop project config catalog did not list saved config.")

    workspace = _post_json(client, "/openevo-api/desktop/workspace", headers=headers)
    if workspace["workspace"]["ready"] is not True:
        raise SmokeFailure("Desktop workspace sync did not become ready.")

    bootstrap = _post_json(client, "/openevo-api/desktop/bootstrap", headers=headers)
    if bootstrap["bootstrap"]["ready"] is not True:
        raise SmokeFailure("Desktop bootstrap did not become ready.")
    if transport.uploaded_framework_lock is None or not transport.uploads:
        raise SmokeFailure("Desktop bootstrap did not upload the Core wheel and lock.")

    services = _post_json(client, "/openevo-api/desktop/services", headers=headers)
    if services["services"]["ready"] is not True:
        raise SmokeFailure("Desktop services did not become ready.")

    services_status = _get_json(
        client,
        "/openevo-api/desktop/services/status",
        headers=headers,
    )
    if services_status["ready"] is not True:
        raise SmokeFailure("Desktop services status did not become ready.")

    launch = _post_json(client, "/openevo-api/desktop/run", headers=headers)
    if backend.validation_requests != [
        {
            "execution_mode": "codex_subscription_transcript",
            "expected_registry_digest": capabilities["registry_digest"],
            "agent_model": draft["codex_model"],
            "reasoning_effort": draft["reasoning_effort"],
            "targets": draft["evolution"]["targets"],
        }
    ]:
        raise SmokeFailure("Desktop run did not preflight the full active project.")
    run_id = launch["run"]["id"]
    terminal = _wait_latest_run_state(client, headers, "succeeded")
    if terminal["run"]["id"] != run_id or terminal["run"]["ready"] is not True:
        raise SmokeFailure("Desktop run did not finish successfully.")

    timeline = _get_json_array(
        client,
        f"/openevo-api/backend/runs/{run_id}/timeline",
        headers=headers,
    )
    if timeline[0]["artifact_ids"] != ["artifact-text-memory"]:
        raise SmokeFailure("Desktop backend timeline did not expose artifact ids.")

    artifacts = _get_json_array(
        client,
        f"/openevo-api/backend/runs/{run_id}/artifacts",
        headers=headers,
    )
    if artifacts[0]["run_id"] != run_id or artifacts[0]["promoted"] is not True:
        raise SmokeFailure("Desktop backend artifacts did not match latest run.")

    content = _get_json(
        client,
        "/openevo-api/backend/artifacts/artifact-text-memory/content",
        headers=headers,
    )
    if content["content"] != "# Learned Memory\n\n- Prefer stable folds.\n":
        raise SmokeFailure("Desktop backend artifact content was not readable.")

    diff = _get_json(
        client,
        "/openevo-api/backend/artifacts/artifact-text-memory/diff",
        headers=headers,
    )
    if diff["after"] != "# Learned Memory\n\n- Prefer stable folds.\n":
        raise SmokeFailure("Desktop backend artifact diff was not readable.")


def _mutation_headers(client: TestClient) -> dict[str, str]:
    shell = _get_json(client, "/openevo-api/desktop/shell")
    token = shell["sidecar"]["mutation_token"]
    if not token:
        raise SmokeFailure("Desktop sidecar did not expose a mutation token.")
    return {SIDECAR_MUTATION_TOKEN_HEADER: token}


def _post_json(
    client: TestClient,
    path: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.post(path, headers=headers, json=json)
    _require_status(response, path)
    payload = response.json()
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{path} did not return a JSON object.")
    return payload


def _get_json(
    client: TestClient,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = client.get(path, headers=headers)
    _require_status(response, path)
    payload = response.json()
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{path} did not return a JSON object.")
    return payload


def _get_json_array(
    client: TestClient,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> list[Any]:
    response = client.get(path, headers=headers)
    _require_status(response, path)
    payload = response.json()
    if not isinstance(payload, list):
        raise SmokeFailure(f"{path} did not return a JSON array.")
    return payload


def _require_status(response, path: str, expected: int = 200) -> None:
    if response.status_code != expected:
        raise SmokeFailure(
            f"{path} returned HTTP {response.status_code}: {response.text}"
        )


def _wait_latest_run_state(
    client: TestClient,
    headers: dict[str, str],
    expected_state: str,
) -> dict[str, Any]:
    for _ in range(50):
        payload = _get_json(client, "/openevo-api/desktop/run", headers=headers)
        if payload["run"]["state"] == expected_state:
            return payload
        time.sleep(0.05)
    raise SmokeFailure(f"Desktop run did not reach {expected_state}.")


def _desktop_config_draft_payload() -> dict[str, Any]:
    return {
        "project_name": "Release Smoke Science",
        "task_id": "literature-baseline",
        "objective": "Validate the installed OpenEvo Desktop lifecycle smoke path.",
        "source_type": "remote_path",
        "source_path": "/datasets/openevo-release-smoke",
        "remote_profile_id": "release-smoke-gpu",
        "remote_host": "gpu.example.edu",
        "remote_port": 22,
        "remote_user": "alice",
        "auth_method": "ssh_agent",
        "https_proxy": "http://127.0.0.1:7890",
        "huggingface_endpoint": "https://hf-mirror.com",
        "codex_model": "gpt-5.5",
        "reasoning_effort": "high",
        "evolution": {
            "targets": {
                "text_memory": {
                    "enabled": True,
                    "method": "text_memory_reflector",
                    "config": {},
                },
                "skill_bundle": {
                    "enabled": True,
                    "method": "skill_bundle_reflector",
                    "config": {},
                },
                "agent_system": {
                    "enabled": True,
                    "method": "auto",
                    "config": {"target_path": "AGENTS.md"},
                },
                "parametric_memory": {
                    "enabled": False,
                    "method": "parametric_memory_register",
                    "config": {},
                },
                "future_target": {
                    "enabled": False,
                    "method": None,
                    "config": {
                        "largest_safe_integer": 9_007_199_254_740_991,
                        "nested": {"values": [1, True, None]},
                    },
                },
            }
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
