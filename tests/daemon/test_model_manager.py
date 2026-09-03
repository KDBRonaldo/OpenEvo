from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import time

import pytest

from openevo.backend.contracts.v2.models import ScienceProjectConfigV2
from openevo.daemon.errors import StateConflictError
from openevo.daemon.model_manager import HuggingFaceModelManager, VLLM_IMAGE, VllmModelRuntime


REVISION = "a" * 40
WEIGHTS = b"verified safetensors fixture"
CONFIG = b'{"architectures":["FixtureForCausalLM"]}'


def test_project_contract_pins_complete_daemon_managed_model_identity() -> None:
    config = ScienceProjectConfigV2.model_validate({
        "schema_version": "2",
        "task": {"title": "Local model", "objective": "Use the managed model."},
        "workspace": {"kind": "scratch", "display_name": "Workspace"},
        "execution": {
            "mode": "self-deployed",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "harness_id": "codex",
            "model_profile_id": None,
            "model_resource_id": "model-ready",
            "repository_id": "OpenEvo/Fixture-0.1B",
            "model_revision": REVISION,
            "token_limit": 8_192,
            "task_network_allow_internet": False,
        },
        "evolution": {"targets": {}},
    })

    assert config.execution.codex_model == "OpenEvo/Fixture-0.1B"
    with pytest.raises(ValueError, match="model authority"):
        ScienceProjectConfigV2.model_validate({
            **config.model_dump(mode="json"),
            "execution": {
                **config.execution.model_dump(mode="json"),
                "model_profile_id": "qwen3-0.6b-v1",
            },
        })


def _sibling(path: str, payload: bytes, *, lfs: bool = False) -> object:
    return SimpleNamespace(
        rfilename=path,
        size=len(payload),
        lfs=(
            SimpleNamespace(sha256=hashlib.sha256(payload).hexdigest())
            if lfs
            else None
        ),
    )


class PublicModelApi:
    def model_info(self, repo_id: str, **kwargs: object) -> object:
        assert repo_id == "OpenEvo/Fixture-0.1B"
        assert kwargs == {
            "revision": "main",
            "files_metadata": True,
            "token": False,
            "timeout": 30,
        }
        return SimpleNamespace(
            private=False,
            gated=False,
            sha=REVISION,
            siblings=[
                _sibling("config.json", CONFIG),
                _sibling("model.safetensors", WEIGHTS, lfs=True),
            ],
        )


def _wait_for_terminal(manager: HuggingFaceModelManager, model_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        model = manager.get(model_id)
        if model["state"] in {"ready", "failed"}:
            return model
        time.sleep(0.01)
    raise AssertionError("model worker did not become terminal")


def test_downloads_public_safetensors_to_a_private_persistent_snapshot(
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def download(**kwargs: object) -> str:
        observed.update(kwargs)
        destination = Path(str(kwargs["local_dir"]))
        (destination / "config.json").write_bytes(CONFIG)
        (destination / "model.safetensors").write_bytes(WEIGHTS)
        return str(destination)

    manager = HuggingFaceModelManager(
        state_path=tmp_path / "state.sqlite3",
        root=tmp_path / "models",
        api_factory=PublicModelApi,
        snapshot_download=download,
    )

    admitted = manager.register(
        action_id="register-fixture",
        repository_id="OpenEvo/Fixture-0.1B",
    )
    ready = _wait_for_terminal(manager, admitted["model_resource_id"])

    assert ready["state"] == "ready"
    assert ready["resolved_revision"] == REVISION
    assert ready["downloaded_bytes"] == ready["total_bytes"]
    assert ready["manifest_sha256"] is not None
    assert "local_path" not in ready
    assert observed["repo_id"] == "OpenEvo/Fixture-0.1B"
    assert observed["revision"] == REVISION
    assert observed["token"] is False
    assert "*.py" not in observed["allow_patterns"]
    snapshot = manager.snapshot_path(ready["model_resource_id"])
    assert snapshot.parent == (tmp_path / "models" / "snapshots").resolve()
    assert (snapshot / "model.safetensors").read_bytes() == WEIGHTS
    assert oct(snapshot.stat().st_mode & 0o777) == "0o700"
    assert oct((snapshot / "model.safetensors").stat().st_mode & 0o777) == "0o600"


def test_registration_action_and_repository_identity_are_idempotent(tmp_path: Path) -> None:
    release = HuggingFaceModelManager(
        state_path=tmp_path / "state.sqlite3",
        root=tmp_path / "models",
        api_factory=PublicModelApi,
        snapshot_download=lambda **_: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    first = release.register(
        action_id="same-action",
        repository_id="OpenEvo/Fixture-0.1B",
    )
    replay = release.register(
        action_id="same-action",
        repository_id="OpenEvo/Fixture-0.1B",
    )
    second_action = release.register(
        action_id="second-action",
        repository_id="OpenEvo/Fixture-0.1B",
    )

    assert replay["model_resource_id"] == first["model_resource_id"]
    assert second_action["model_resource_id"] == first["model_resource_id"]
    with pytest.raises(StateConflictError, match="another request"):
        release.register(
            action_id="same-action",
            repository_id="OpenEvo/Different-0.1B",
        )


@pytest.mark.parametrize(
    "info,error",
    [
        (
            SimpleNamespace(private=True, gated=False, sha=REVISION, siblings=[]),
            "only public",
        ),
        (
            SimpleNamespace(
                private=False,
                gated=False,
                sha="main",
                siblings=[
                    _sibling("config.json", CONFIG),
                    _sibling("model.safetensors", WEIGHTS, lfs=True),
                ],
            ),
            "immutable",
        ),
        (
            SimpleNamespace(
                private=False,
                gated=False,
                sha=REVISION,
                siblings=[
                    _sibling("config.json", CONFIG),
                    _sibling("pytorch_model.bin", WEIGHTS, lfs=True),
                ],
            ),
            "safetensors",
        ),
    ],
)
def test_fails_closed_for_unsupported_hugging_face_repositories(
    tmp_path: Path,
    info: object,
    error: str,
) -> None:
    manager = HuggingFaceModelManager(
        state_path=tmp_path / "state.sqlite3",
        root=tmp_path / "models",
        api_factory=lambda: SimpleNamespace(model_info=lambda *_, **__: info),
        snapshot_download=lambda **_: pytest.fail("unsupported models must not download"),
    )

    admitted = manager.register(
        action_id=f"unsupported-{error}",
        repository_id="OpenEvo/Fixture-0.1B",
    )
    failed = _wait_for_terminal(manager, admitted["model_resource_id"])

    assert failed["state"] == "failed"
    assert error in str(failed["error"])


def test_restart_marks_an_interrupted_download_failed(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite3"
    first = HuggingFaceModelManager(state_path=state_path, root=tmp_path / "models")
    with first._connection() as connection:
        now = "2026-09-02T00:00:00Z"
        connection.execute(
            "INSERT INTO development_models(model_resource_id, repository_id, "
            "requested_revision, state, created_at, updated_at) "
            "VALUES ('model-interrupted', 'OpenEvo/Fixture-0.1B', 'main', "
            "'downloading', ?, ?)",
            (now, now),
        )

    restarted = HuggingFaceModelManager(state_path=state_path, root=tmp_path / "models")

    recovered = restarted.get("model-interrupted")
    assert recovered["state"] == "failed"
    assert "interrupted" in str(recovered["error"])


def test_download_progress_counts_incomplete_weight_but_not_cache_metadata(
    tmp_path: Path,
) -> None:
    root = tmp_path / "staging"
    cache = root / ".cache" / "huggingface" / "download"
    cache.mkdir(parents=True)
    (root / "config.json").write_bytes(b"config")
    (cache / "model.safetensors.incomplete").write_bytes(b"partial-weight")
    (cache / "model.safetensors.metadata").write_bytes(b"metadata")

    assert HuggingFaceModelManager._tree_size(root) == len(b"configpartial-weight")


def test_vllm_runtime_bounds_context_window_from_model_config(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text(
        json.dumps({"max_position_embeddings": 131_072}), encoding="utf-8"
    )

    assert VllmModelRuntime._model_context_window(snapshot) == 32_768


def test_vllm_runtime_is_loopback_only_reuses_one_model_and_stops_its_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    commands: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "--entrypoint" in command:
            stdout = "0, 24576, 12000\n1, 24576, 24500\n"
        else:
            stdout = "b" * 64 if command[1] == "run" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    class Proxy:
        def start(self) -> str:
            return "http://127.0.0.1:19191/v1"

        def close(self) -> None:
            return

    def proxy_factory(**_: object) -> Proxy:
        return Proxy()

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    runtime = VllmModelRuntime(
        command_runner=run,
        health_probe=lambda _: True,
        proxy_factory=proxy_factory,  # type: ignore[arg-type]
    )

    first = runtime.ensure_running(
        model_resource_id="model-fixture",
        repository_id="OpenEvo/Fixture-0.1B",
        snapshot_path=snapshot,
    )
    replay = runtime.ensure_running(
        model_resource_id="model-fixture",
        repository_id="OpenEvo/Fixture-0.1B",
        snapshot_path=snapshot,
    )
    runtime.close()

    assert first == replay
    assert first["base_url"].startswith("http://127.0.0.1:")
    starts = [command for command in commands if "--detach" in command]
    assert len(starts) == 1
    assert VLLM_IMAGE in starts[0]
    assert "--pull=never" in starts[0]
    assert "--gpus" in starts[0]
    assert starts[0][starts[0].index("--gpus") + 1] == "device=1"
    assert "--rm" not in starts[0]
    assert "--cap-add=DAC_READ_SEARCH" in starts[0]
    assert "--enable-auto-tool-choice" in starts[0]
    assert "hermes" in starts[0]
    assert any(value.startswith("127.0.0.1:") for value in starts[0])
    assert f"{snapshot}:/model:ro" in starts[0]
    assert commands[-1] == ["/usr/bin/docker", "rm", "--force", "b" * 64]


def test_vllm_runtime_reports_gpu_pressure_before_starting_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    commands: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "--entrypoint" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="0, 32607, 11000\n1, 32607, 9000\n",
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    runtime = VllmModelRuntime(command_runner=run)

    with pytest.raises(StateConflictError, match="no GPU has enough free memory") as error:
        runtime.ensure_running(
            model_resource_id="model-fixture",
            repository_id="OpenEvo/Fixture-0.1B",
            snapshot_path=snapshot,
        )

    assert "GPU 0: 11000 MiB free" in str(error.value)
    assert not any("--detach" in command for command in commands)


def test_vllm_runtime_preserves_failure_logs_before_removing_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    container_id = "c" * 64
    commands: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "--entrypoint" in command:
            return subprocess.CompletedProcess(
                command, 0, stdout="0, 32607, 32600\n", stderr=""
            )
        if "--detach" in command:
            return subprocess.CompletedProcess(command, 0, stdout=container_id, stderr="")
        if command[1] == "inspect" and "{{.State.Running}}" in command:
            return subprocess.CompletedProcess(command, 0, stdout="false\n", stderr="")
        if command[1] == "logs":
            return subprocess.CompletedProcess(
                command, 0, stdout="CUDA out of memory", stderr=""
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    runtime = VllmModelRuntime(
        command_runner=run,
        health_probe=lambda _: False,
    )

    with pytest.raises(StateConflictError, match="CUDA out of memory"):
        runtime.ensure_running(
            model_resource_id="model-fixture",
            repository_id="OpenEvo/Fixture-0.1B",
            snapshot_path=snapshot,
        )

    assert ["/usr/bin/docker", "logs", "--tail", "80", container_id] in commands
    assert commands[-1] == ["/usr/bin/docker", "rm", "--force", container_id]
