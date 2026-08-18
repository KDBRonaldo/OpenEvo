from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from openevo.runtime import docker_host as docker_host_module
from openevo.runtime.docker_host import (
    DOCKER_HOST_ENDPOINT,
    DockerSocketAuthority,
    DockerHostPathError,
    DockerHostPathSpec,
    HeldDockerSessionRoot,
    discover_docker_host_path,
    docker_cli_environment,
    docker_self_inspect_argv,
    verify_docker_host_path,
)


_HOSTNAME = "1" * 12
_CONTAINER_ID = _HOSTNAME + ("2" * 52)


def test_docker_executable_path_prefers_usr_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[str] = []

    def fake_stat(path: str, *, follow_symlinks: bool) -> SimpleNamespace:
        observed.append(path)
        assert follow_symlinks is False
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o755)

    monkeypatch.setattr(docker_host_module.os, "stat", fake_stat)

    assert docker_host_module._select_docker_executable_path() == "/usr/bin/docker"
    assert observed == ["/usr/bin/docker"]


def test_docker_executable_path_accepts_usr_local_bin_when_usr_bin_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_stat(path: str, *, follow_symlinks: bool) -> SimpleNamespace:
        assert follow_symlinks is False
        if path == "/usr/bin/docker":
            raise FileNotFoundError(path)
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o755)

    monkeypatch.setattr(docker_host_module.os, "stat", fake_stat)

    assert docker_host_module._select_docker_executable_path() == "/usr/local/bin/docker"


def test_docker_executable_path_does_not_follow_usr_bin_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_stat(path: str, *, follow_symlinks: bool) -> SimpleNamespace:
        assert follow_symlinks is False
        mode = stat.S_IFLNK | 0o777 if path == "/usr/bin/docker" else stat.S_IFREG | 0o755
        return SimpleNamespace(st_mode=mode)

    monkeypatch.setattr(docker_host_module.os, "stat", fake_stat)

    assert docker_host_module._select_docker_executable_path() == "/usr/local/bin/docker"


def _inspect_payload(
    mounts: list[dict[str, object]],
    *,
    container_id: str = _CONTAINER_ID,
    hostname: str = _HOSTNAME,
    running: bool = True,
) -> bytes:
    return json.dumps(
        {
            "id": container_id,
            "hostname": hostname,
            "running": running,
            "mounts": mounts,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _bind_mount(
    destination: Path,
    *,
    source: str = "/srv/openevo-data",
    writable: bool = True,
) -> dict[str, object]:
    return {
        "Type": "bind",
        "Source": source,
        "Destination": os.fspath(destination),
        "RW": writable,
    }


def _discover(destination: Path) -> tuple[DockerHostPathSpec, bytes]:
    payload = _inspect_payload([_bind_mount(destination)])
    return (
        discover_docker_host_path(
            payload,
            namespace="core-release",
            hostname=_HOSTNAME,
            minimum_available_bytes=0,
        ),
        payload,
    )


def test_discovery_pins_private_runtime_root_and_translates_only_descendants(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "data"
    destination.mkdir()

    authority, payload = _discover(destination)

    expected_root = destination / f".openevo-runtime-{os.geteuid()}" / "core-release"
    assert authority.container_id == _CONTAINER_ID
    assert authority.mount_destination == os.fspath(destination)
    assert authority.mount_source == "/srv/openevo-data"
    assert authority.runtime_container_root == os.fspath(expected_root)
    assert authority.runtime_daemon_root == (
        f"/srv/openevo-data/.openevo-runtime-{os.geteuid()}/core-release"
    )
    assert expected_root.stat().st_mode & 0o777 == 0o700
    assert (expected_root / "sessions").stat().st_mode & 0o777 == 0o700
    assert (
        authority.translate(expected_root / "sessions" / "task-a")
        == Path(authority.runtime_daemon_root) / "sessions" / "task-a"
    )
    verify_docker_host_path(authority, payload, hostname=_HOSTNAME)

    with pytest.raises(DockerHostPathError, match="outside"):
        authority.translate(destination / "unmanaged")


def test_discovery_accepts_preprovisioned_private_root_below_read_only_mount(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "shared-data"
    destination.mkdir()
    private_parent = destination / f".openevo-runtime-{os.geteuid()}"
    private_parent.mkdir(mode=0o700)
    destination.chmod(0o555)

    try:
        authority, payload = _discover(destination)

        assert authority.runtime_container_root == os.fspath(private_parent / "core-release")
        assert Path(authority.runtime_container_root).stat().st_mode & 0o777 == 0o700
        verify_docker_host_path(authority, payload, hostname=_HOSTNAME)
    finally:
        destination.chmod(0o700)


def test_discovery_rejects_two_eligible_bind_roots(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    payload = _inspect_payload(
        [
            _bind_mount(first, source="/srv/first"),
            _bind_mount(second, source="/srv/second"),
        ]
    )

    with pytest.raises(DockerHostPathError, match="ambiguous"):
        discover_docker_host_path(
            payload,
            namespace="core-release",
            hostname=_HOSTNAME,
            minimum_available_bytes=0,
        )

    assert not (first / f".openevo-runtime-{os.geteuid()}").exists()
    assert not (second / f".openevo-runtime-{os.geteuid()}").exists()


@pytest.mark.parametrize("nested", [False, True])
def test_discovery_rejects_duplicate_or_nested_bind_evidence(
    tmp_path: Path,
    *,
    nested: bool,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    other = root / "nested" if nested else root
    if nested:
        other.mkdir()
    payload = _inspect_payload(
        [
            _bind_mount(root),
            _bind_mount(
                other, source="/srv/openevo-data/nested" if nested else "/srv/openevo-data"
            ),
        ]
    )

    with pytest.raises(DockerHostPathError, match="ambiguous"):
        discover_docker_host_path(
            payload,
            namespace="core-release",
            hostname=_HOSTNAME,
            minimum_available_bytes=0,
        )


def test_held_sessions_authority_rejects_symlink_replacement_without_escape(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "data"
    destination.mkdir()
    authority, _ = _discover(destination)
    held = HeldDockerSessionRoot.open(authority)
    sessions = Path(authority.runtime_container_root) / "sessions"
    displaced = sessions.with_name("sessions-displaced")
    external = tmp_path / "external"
    external.mkdir()
    sessions.rename(displaced)
    sessions.symlink_to(external, target_is_directory=True)

    try:
        with pytest.raises(DockerHostPathError, match="authority changed"):
            held.create_private_directory(
                "session",
                child_directories=("artifacts",),
            )
    finally:
        held.close()

    assert list(external.iterdir()) == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            _inspect_payload(
                [
                    {
                        "Type": "volume",
                        "Source": "/srv/openevo-data",
                        "Destination": "/data",
                        "RW": True,
                    }
                ]
            ),
            "no writable",
        ),
        (
            _inspect_payload(
                [
                    {
                        "Type": "bind",
                        "Source": "/srv/openevo-data",
                        "Destination": "/data",
                        "RW": False,
                    }
                ]
            ),
            "no writable",
        ),
        (
            _inspect_payload([], running=False),
            "identity",
        ),
    ],
)
def test_discovery_fails_closed_without_release_profile_evidence(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    del tmp_path
    with pytest.raises(DockerHostPathError, match=message):
        discover_docker_host_path(
            payload,
            namespace="core-release",
            hostname=_HOSTNAME,
            minimum_available_bytes=0,
        )


def test_verification_rejects_mount_and_runtime_root_replacement(tmp_path: Path) -> None:
    destination = tmp_path / "data"
    destination.mkdir()
    authority, payload = _discover(destination)

    changed_source = _inspect_payload([_bind_mount(destination, source="/srv/replaced")])
    with pytest.raises(DockerHostPathError, match="mount changed"):
        verify_docker_host_path(authority, changed_source, hostname=_HOSTNAME)

    runtime_root = Path(authority.runtime_container_root)
    displaced = runtime_root.with_name(runtime_root.name + "-old")
    runtime_root.rename(displaced)
    runtime_root.mkdir(mode=0o700)
    with pytest.raises(DockerHostPathError, match="identity changed"):
        verify_docker_host_path(authority, payload, hostname=_HOSTNAME)


def test_closed_authority_rejects_unknown_fields_and_digest_tampering(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "data"
    destination.mkdir()
    authority, _ = _discover(destination)
    payload = authority.model_dump(mode="json")

    with pytest.raises(ValidationError, match="Extra inputs"):
        DockerHostPathSpec.model_validate({**payload, "host_path": "/secret"})
    with pytest.raises(ValidationError, match="digest"):
        DockerHostPathSpec.model_validate({**payload, "identity_digest": "f" * 64})


def test_self_inspect_command_requires_container_identity_hostname() -> None:
    assert docker_self_inspect_argv(_HOSTNAME)[-1] == _HOSTNAME
    with pytest.raises(DockerHostPathError, match="container-ID hostname"):
        docker_self_inspect_argv("gpu-server")


def test_host_path_admission_requires_hostname_to_equal_id_prefix(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    with pytest.raises(DockerHostPathError, match="self-container identity"):
        discover_docker_host_path(
            _inspect_payload(
                [_bind_mount(data_root)],
                hostname="a" * 12,
                container_id=("b" * 12) + ("c" * 52),
            ),
            namespace="core-release",
            hostname="a" * 12,
            minimum_available_bytes=0,
        )


@pytest.mark.parametrize("length", [13, 64])
def test_self_inspect_rejects_container_id_prefix_hostname(length: int) -> None:
    with pytest.raises(DockerHostPathError, match="container-ID hostname"):
        docker_self_inspect_argv("a" * length)


@pytest.mark.parametrize("length", [13, 64])
def test_host_path_admission_rejects_long_container_id_prefix_hostname(
    tmp_path: Path,
    length: int,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    hostname = "a" * length
    with pytest.raises(DockerHostPathError, match="self-container identity"):
        discover_docker_host_path(
            _inspect_payload(
                [_bind_mount(data_root)],
                hostname=hostname,
                container_id=hostname + ("b" * (64 - length)),
            ),
            namespace="core-release",
            hostname=hostname,
            minimum_available_bytes=0,
        )


def _bind_unix_socket(path: Path, *, mode: int = 0o660) -> None:
    engine_socket = socket.socket(socket.AF_UNIX)
    try:
        engine_socket.bind(os.fspath(path))
    finally:
        engine_socket.close()
    path.chmod(mode)


def test_docker_cli_environment_is_complete_and_ignores_user_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://attacker.invalid:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "attacker")
    monkeypatch.setenv("DOCKER_CONFIG", "/tmp/attacker-config")
    monkeypatch.setenv("HOME", "/tmp/attacker-home")

    assert docker_cli_environment() == {
        "DOCKER_CONFIG": "/proc/self",
        "DOCKER_HOST": DOCKER_HOST_ENDPOINT,
        "HOME": "/proc/self",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    assert "DOCKER_CONTEXT" not in docker_cli_environment()


@pytest.mark.skipif(os.geteuid() != 0, reason="release socket authority is root-owned")
def test_docker_socket_authority_accepts_root_docker_mode_and_rejects_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "docker.sock"
    _bind_unix_socket(socket_path)
    monkeypatch.setattr(
        docker_host_module,
        "DOCKER_SOCKET_PATH",
        os.fspath(socket_path),
    )
    authority = DockerSocketAuthority.open()
    original_identity = authority.identity
    displaced = tmp_path / "docker.displaced.sock"
    socket_path.rename(displaced)
    _bind_unix_socket(socket_path)

    assert authority.identity == original_identity
    with pytest.raises(DockerHostPathError, match="socket authority changed"):
        authority.verify()


@pytest.mark.skipif(os.geteuid() != 0, reason="release socket authority is root-owned")
@pytest.mark.parametrize("kind", ["regular", "world_writable"])
def test_docker_socket_authority_rejects_invalid_type_or_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    socket_path = tmp_path / "docker.sock"
    if kind == "regular":
        socket_path.write_text("not a socket", encoding="utf-8")
        socket_path.chmod(0o660)
    else:
        _bind_unix_socket(socket_path, mode=0o666)
    monkeypatch.setattr(
        docker_host_module,
        "DOCKER_SOCKET_PATH",
        os.fspath(socket_path),
    )

    with pytest.raises(DockerHostPathError, match="socket identity is invalid"):
        DockerSocketAuthority.open()
