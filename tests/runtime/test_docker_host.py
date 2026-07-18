from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from openevo.runtime.docker_host import (
    DockerHostPathError,
    DockerHostPathSpec,
    HeldDockerSessionRoot,
    discover_docker_host_path,
    docker_self_inspect_argv,
    verify_docker_host_path,
)


_HOSTNAME = "1" * 12
_CONTAINER_ID = _HOSTNAME + ("2" * 52)


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
