from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket

import pytest

from openevo import experiments
from openevo.backend import launcher
from openevo.backend.runtime_identity import CoreReleaseIdentity
from openevo.experiments.models import ExperimentConfig
from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES


_MANAGED_SCIENCE_RUNTIME = {
    "kind": "docker",
    "profile": "managed_science",
    "image": MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference,
    "container_user": "host",
}


@pytest.mark.parametrize(
    (
        "auth",
        "settings",
        "runtime",
        "expected_execution_mode",
        "expected_runtime_capabilities",
    ),
    [
        (
            "subscription",
            {"auth_mode": "subscription", "capture_mode": "transcript"},
            _MANAGED_SCIENCE_RUNTIME,
            "subscription",
            (),
        ),
        (
            "proxy",
            {"auth_mode": "proxy", "capture_mode": "transcript"},
            None,
            "self_deployed",
            ("adapter_serving",),
        ),
    ],
)
def test_backend_launcher_builds_transcript_profile_for_science_execution_modes(
    auth: str,
    settings: dict[str, str],
    runtime: dict[str, str] | None,
    expected_execution_mode: str,
    expected_runtime_capabilities: tuple[str, ...],
) -> None:
    payload = {
        "experiment": {"name": "science"},
        "agent": {
            "preset": "codex",
            "model": "science-model",
            "auth": auth,
            "settings": settings,
        },
        "tasks": [{"id": "task", "instruction": "Run the task."}],
    }
    if runtime is not None:
        payload["runtime"] = runtime
    config = ExperimentConfig.model_validate(payload)

    profile = launcher._execution_profile_for_config(config)

    assert profile.execution_mode == expected_execution_mode
    assert profile.capture_mode == "transcript"
    assert profile.runtime_capabilities == expected_runtime_capabilities
    if auth == "subscription":
        assert config.runtime.kind == "docker"
        assert config.runtime.profile == "managed_science"
        assert (
            config.runtime.image
            == MANAGED_RUNTIME_RELEASES["managed_science"].immutable_reference
        )
        assert config.runtime.container_user == "host"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("kind", "apptainer", "Docker"),
        ("image", "attacker:latest", "exact Core-managed image"),
        ("container_user", "image", "container_user='host'"),
    ],
)
def test_backend_launcher_revalidates_managed_self_deployed_runtime(
    field: str,
    value: str,
    message: str,
) -> None:
    config = ExperimentConfig.model_validate(
        {
            "experiment": {"name": "science"},
            "agent": {
                "preset": "codex",
                "model": "science-model",
                "auth": "proxy",
                "settings": {"capture_mode": "transcript"},
            },
            "runtime": _MANAGED_SCIENCE_RUNTIME,
            "tasks": [{"id": "task", "instruction": "Run the task."}],
        }
    )
    object.__setattr__(config.runtime, field, value)

    with pytest.raises(ValueError, match=message):
        launcher._execution_profile_for_config(config)


def test_backend_launcher_run_invokes_experiment_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "experiment.yaml"
    output_dir = tmp_path / "run"
    config = object()
    framework_lock = tmp_path / "framework-lock.json"
    registry = object()
    profile = object()
    calls: dict[str, object] = {}

    def fake_load_experiment_config(path: Path) -> object:
        calls["config_path"] = path
        return config

    def fake_run_experiment(
        loaded_config: object,
        *,
        task_ids: list[str] | None,
        rounds_override: int | None,
        output_dir: Path | None,
        artifact_root: Path | None,
        executable_registry: object,
        execution_profile: object,
    ) -> dict[str, object]:
        calls["loaded_config"] = loaded_config
        calls["task_ids"] = task_ids
        calls["rounds_override"] = rounds_override
        calls["output_dir"] = output_dir
        calls["artifact_root"] = artifact_root
        calls["executable_registry"] = executable_registry
        calls["execution_profile"] = execution_profile
        return {"status": "completed", "summary_path": str(output_dir / "summary.json")}

    monkeypatch.setattr(
        experiments,
        "load_experiment_config",
        fake_load_experiment_config,
    )
    monkeypatch.setattr(experiments, "run_experiment", fake_run_experiment)
    monkeypatch.setattr(launcher, "load_verified_framework_registry", lambda path: registry)
    monkeypatch.setattr(launcher, "_execution_profile_for_config", lambda value: profile)

    exit_code = launcher.main(
        [
            "run",
            str(config_path),
            "--output-dir",
            str(output_dir),
            "--artifact-root",
            str(tmp_path / "state" / "evolution" / "artifacts"),
            "--rounds",
            "3",
            "--task-id",
            "task-a",
            "--task-id",
            "task-b",
            "--json",
            "--framework-lock",
            str(framework_lock),
        ]
    )

    assert exit_code == 0
    assert calls == {
        "config_path": config_path,
        "loaded_config": config,
        "task_ids": ["task-a", "task-b"],
        "rounds_override": 3,
        "output_dir": output_dir,
        "artifact_root": tmp_path / "state" / "evolution" / "artifacts",
        "executable_registry": registry,
        "execution_profile": profile,
    }
    assert json.loads(capsys.readouterr().out) == {
        "status": "completed",
        "summary_path": str(output_dir / "summary.json"),
    }


def test_backend_launcher_run_dry_run_uses_dry_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "experiment.yaml"
    config = object()
    framework_lock = tmp_path / "framework-lock.json"
    registry = type("Registry", (), {"snapshot": object()})()
    profile = object()
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        experiments,
        "load_experiment_config",
        lambda path: config,
    )

    def fake_dry_run_experiment(
        loaded_config: object,
        *,
        task_ids: list[str] | None,
        rounds_override: int | None,
        registry_snapshot: object,
        execution_profile: object,
    ) -> dict[str, object]:
        calls["loaded_config"] = loaded_config
        calls["task_ids"] = task_ids
        calls["rounds_override"] = rounds_override
        calls["registry_snapshot"] = registry_snapshot
        calls["execution_profile"] = execution_profile
        return {"mode": "dry_run"}

    monkeypatch.setattr(experiments, "dry_run_experiment", fake_dry_run_experiment)
    monkeypatch.setattr(launcher, "load_verified_framework_registry", lambda path: registry)
    monkeypatch.setattr(launcher, "_execution_profile_for_config", lambda value: profile)

    assert (
        launcher.main(
            [
                "run",
                str(config_path),
                "--dry-run",
                "--json",
                "--framework-lock",
                str(framework_lock),
            ]
        )
        == 0
    )

    assert calls == {
        "loaded_config": config,
        "task_ids": None,
        "rounds_override": None,
        "registry_snapshot": registry.snapshot,
        "execution_profile": profile,
    }
    assert json.loads(capsys.readouterr().out) == {"mode": "dry_run"}


def test_backend_launcher_serve_requires_supervised_core_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        launcher,
        "_serve_core_control",
        lambda args: calls.append(args) or 0,
    )

    assert (
        launcher.main(
            [
                "serve",
                "--service-root",
                "/home/user/.openevo/core",
                "--framework-lock",
                "/srv/openevo/framework-lock.json",
                "--source-commit",
                "1" * 40,
                "--socket-fd",
                "3",
                "--ready-fd",
                "4",
                "--spawn-lock-fd",
                "5",
                "--expected-release-identity",
                "2" * 64,
                "--generation",
                "3" * 32,
            ]
        )
        == 0
    )
    assert len(calls) == 1
    args = calls[0]
    assert args.service_root == Path("/home/user/.openevo/core")
    assert args.socket_fd == 3
    assert args.ready_fd == 4


def test_supervised_launcher_builds_release_core_control_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_root = tmp_path / "core"
    service_root.mkdir(mode=0o700)
    release = CoreReleaseIdentity(
        digest="a" * 64,
        registry_digest="b" * 64,
        framework_lock_sha256="c" * 64,
        source_commit="1" * 40,
    )
    registry = object()
    app = object()
    service_supervisor = object()
    run_owner = object()
    calls: dict[str, object] = {}

    monkeypatch.setattr(launcher, "require_host_global_service_root", lambda path: path)
    monkeypatch.setattr(launcher, "load_verified_framework_registry", lambda path: registry)
    monkeypatch.setattr(launcher, "compute_release_identity", lambda **kwargs: release)

    def build_service_supervisor(**kwargs: object) -> object:
        calls["service_supervisor"] = kwargs
        return service_supervisor

    monkeypatch.setattr(launcher, "CoreServiceSupervisor", build_service_supervisor)

    def build_run_owner(**kwargs: object) -> object:
        calls["run_owner"] = kwargs
        return run_owner

    monkeypatch.setattr(launcher, "CoreScienceRunOwner", build_run_owner)

    def claim_spawn(**kwargs: object) -> None:
        calls["claim"] = kwargs
        os.close(int(kwargs["spawn_lock_fd"]))

    monkeypatch.setattr(launcher, "claim_core_service_spawn", claim_spawn)
    monkeypatch.setattr(
        launcher,
        "_bind_host_service_identity",
        lambda *args, **kwargs: calls.setdefault("identity", (args, kwargs)),
    )

    def create_app(**kwargs: object) -> object:
        calls["create"] = kwargs
        return app

    async def run_server(
        received_app: object,
        *,
        inherited_socket: socket.socket,
        ready_fd: int,
        ready_payload: dict[str, object],
    ) -> int:
        calls["server"] = {
            "app": received_app,
            "socket": inherited_socket.getsockname(),
            "ready_payload": ready_payload,
        }
        os.close(ready_fd)
        return 0

    monkeypatch.setattr(launcher, "create_core_control_app", create_app)
    monkeypatch.setattr(launcher, "_run_supervised_server", run_server)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    read_fd, write_fd = os.pipe()
    spawn_lock_fd = os.open(tmp_path / "spawn.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        result = launcher._serve_core_control(
            argparse.Namespace(
                service_root=service_root,
                framework_lock=tmp_path / "framework-lock.json",
                source_commit=release.source_commit,
                socket_fd=listener.detach(),
                ready_fd=write_fd,
                spawn_lock_fd=spawn_lock_fd,
                expected_release_identity=release.digest,
                generation="d" * 32,
            )
        )
    finally:
        os.close(read_fd)
    assert result == 0
    create = calls["create"]
    assert create["state_root"] == service_root / "state"
    assert create["build_channel"] == "release"
    assert create["source_commit"] == release.source_commit
    assert create["evolution_registry"] is registry
    assert create["service_supervisor"] is service_supervisor
    project_store = object()
    assert create["run_control_factory"](project_store) is run_owner
    assert calls["run_owner"] == {
        "state_root": service_root / "state",
        "project_store": project_store,
        "service_supervisor": service_supervisor,
        "executable_registry": registry,
    }
    assert len(create["bearer_token"]) == 64
    server = calls["server"]
    assert calls["service_supervisor"] == {
        "launch_mode": launcher.ServiceLaunchMode.RELEASE,
        "service_root": service_root / "managed-services",
        "framework_lock": tmp_path / "framework-lock.json",
        "verified_registry": registry,
        "run_admission_url": (
            f"http://127.0.0.1:{server['socket'][1]}"
            "/internal/v1/run-admissions/verify"
        ),
    }
    assert server["app"] is app
    assert server["socket"][0] == "127.0.0.1"
    assert server["ready_payload"]["release_identity"] == release.digest
