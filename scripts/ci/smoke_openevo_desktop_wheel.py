#!/usr/bin/env python3
"""Smoke test the installed OpenEvo Desktop app and packaged static assets."""

from __future__ import annotations

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

from openevo.desktop.app import create_desktop_app
from openevo.remote import RemoteCommandResult
from openevo.sidecar.api import create_sidecar_app
from openevo.sidecar.api import SIDECAR_MUTATION_TOKEN_HEADER

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
        if command.startswith('PATH="$HOME/.local/bin:$PATH" openevo run '):
            return RemoteCommandResult(command=command, return_code=0, stdout="ok")
        if "summary.json" in command:
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=json.dumps(_sample_run_summary()),
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        self.uploads.append((local_path, remote_path))


def main() -> int:
    transport = _LifecycleSmokeTransport()
    try:
        with TemporaryDirectory(prefix="openevo-desktop-smoke-") as config_root:
            app = create_desktop_app(
                create_sidecar_app(
                    config_root=Path(config_root),
                    transport_factory=lambda _profile: transport,
                )
            )
            with TestClient(app) as client:
                assets = _smoke_packaged_assets(client)
                _smoke_config_backed_lifecycle(client)
    except SmokeFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OpenEvo Desktop wheel smoke passed for {len(assets)} asset(s).")
    print("OpenEvo Desktop config-backed lifecycle smoke passed.")
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


def _smoke_config_backed_lifecycle(client: TestClient) -> None:
    headers = _mutation_headers(client)
    config = _post_json(
        client,
        "/openevo-api/desktop/project-config",
        headers=headers,
        json=_desktop_config_draft_payload(),
    )
    if config["status"]["project"]["name"] != "Release Smoke Science":
        raise SmokeFailure("Desktop project config response did not load submitted project.")

    catalog = _get_json(client, "/openevo-api/desktop/project-configs")
    if len(catalog["configs"]) != 1 or catalog["configs"][0]["valid"] is not True:
        raise SmokeFailure("Desktop project config catalog did not list saved config.")

    workspace = _post_json(client, "/openevo-api/desktop/workspace", headers=headers)
    if workspace["workspace"]["ready"] is not True:
        raise SmokeFailure("Desktop workspace sync did not become ready.")

    bootstrap = _post_json(client, "/openevo-api/desktop/bootstrap", headers=headers)
    if bootstrap["bootstrap"]["ready"] is not True:
        raise SmokeFailure("Desktop bootstrap did not become ready.")

    services = _post_json(client, "/openevo-api/desktop/services", headers=headers)
    if services["services"]["ready"] is not True:
        raise SmokeFailure("Desktop services did not become ready.")

    launch = _post_json(client, "/openevo-api/desktop/run", headers=headers)
    run_id = launch["run"]["id"]
    terminal = _wait_latest_run_state(client, headers, "succeeded")
    if terminal["run"]["id"] != run_id or terminal["run"]["ready"] is not True:
        raise SmokeFailure("Desktop run did not finish successfully.")

    artifacts = _get_json(
        client,
        "/openevo-api/desktop/run/artifacts",
        headers=headers,
    )
    if artifacts["run_id"] != run_id:
        raise SmokeFailure("Desktop run artifacts response did not match latest run.")
    if artifacts["summary_status"] != "completed" or not artifacts["tasks"]:
        raise SmokeFailure("Desktop run artifacts summary was not parsed.")


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
        "codex_model": "gpt-5.1-codex-mini",
        "text_memory": True,
        "skill_bundle": True,
        "agent_system": True,
    }


def _sample_run_summary() -> dict[str, Any]:
    return {
        "mode": "run",
        "status": "completed",
        "experiment_id": "release-smoke-science",
        "experiment_name": "Release Smoke Science",
        "round_count": 1,
        "tasks": [
            {
                "task_id": "literature-baseline",
                "rounds": [
                    {
                        "round_index": 0,
                        "policy_version": "policy-r0",
                        "rollout_status": "completed",
                        "dataset_status": "ready",
                        "artifact_ids": {
                            "dataset": ["dataset-release-smoke"],
                            "text_memory": ["artifact-text-memory"],
                            "skill_bundle": ["artifact-skill-bundle"],
                            "agent_system": ["artifact-agent-system"],
                        },
                        "jobs": [
                            {
                                "artifact_type": "text_memory",
                                "method": "text_memory_reflector",
                                "worker_status": "succeeded",
                                "artifact_ids": ["artifact-text-memory"],
                                "approved_artifact_ids": ["artifact-text-memory"],
                                "promotion_status": "approved",
                            }
                        ],
                    }
                ],
            }
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
