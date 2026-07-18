from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "e2e" / "docker_release_host_fixture.py"
)


def _load_fixture() -> ModuleType:
    spec = importlib.util.spec_from_file_location("docker_release_host_fixture", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fixture = _load_fixture()
PROVENANCE_SHA256 = "b" * 64
CONTAINER_ID = "c" * 64
IMAGE_ID = "sha256:" + "d" * 64
DOCKER_SERVER = fixture.DockerServerIdentity(
    version="29.3.0",
    api_version="1.54",
    os="linux",
    architecture="amd64",
)


def _public_key_bytes(comment: str = "maintainer@example") -> bytes:
    key_type = b"ssh-ed25519"
    wire = len(key_type).to_bytes(4, "big") + key_type + b"bounded-test-key-material"
    return key_type + b" " + base64.b64encode(wire) + b" " + comment.encode() + b"\n"


def _labels(
    data_root: Path,
    docker_data_root: Path | None = None,
) -> dict[str, str]:
    docker_data_root = data_root if docker_data_root is None else docker_data_root
    return {
        fixture.LABEL_KIND: fixture.FIXTURE_KIND,
        fixture.LABEL_SCHEMA: str(fixture.SCHEMA_VERSION),
        fixture.LABEL_DATA_ROOT: fixture._sha256_text(str(data_root)),
        fixture.LABEL_DOCKER_DATA_ROOT: fixture._sha256_text(str(docker_data_root)),
        fixture.LABEL_PUBLIC_KEY: "a" * 64,
        fixture.LABEL_IMAGE: fixture._sha256_text(fixture.DEFAULT_IMAGE),
        fixture.LABEL_UID: "1000",
        fixture.LABEL_GID: "1000",
        fixture.LABEL_SOCKET_GID: "999",
        fixture.LABEL_SSH_PORT: "0",
        fixture.LABEL_PROVENANCE: PROVENANCE_SHA256,
    }


def _inspect(
    data_root: Path,
    *,
    docker_data_root: Path | None = None,
    running: bool = True,
    host_ip: str = "127.0.0.1",
) -> dict[str, object]:
    docker_data_root = data_root if docker_data_root is None else docker_data_root
    return {
        "Config": {
            "Cmd": ["sh", "-ceu", fixture.CONTAINER_COMMAND],
            "Hostname": CONTAINER_ID[:12],
            "Image": fixture.DEFAULT_IMAGE,
            "Labels": _labels(data_root, docker_data_root),
        },
        "Id": CONTAINER_ID,
        "Image": IMAGE_ID,
        "Platform": "linux",
        "HostConfig": {
            "Memory": 4 * 1024 * 1024 * 1024,
            "NanoCpus": 4_000_000_000,
            "PidsLimit": 2048,
            "RestartPolicy": {"MaximumRetryCount": 0, "Name": "no"},
        },
        "Mounts": [
            {
                "Destination": str(data_root),
                "RW": True,
                "Source": str(docker_data_root),
                "Type": "bind",
            },
            {
                "Destination": str(fixture.DOCKER_SOCKET),
                "RW": True,
                "Source": str(fixture.DOCKER_SOCKET),
                "Type": "bind",
            },
        ],
        "NetworkSettings": {
            "Networks": {"bridge": {"IPAddress": "172.17.0.11"}},
            "Ports": {"22/tcp": [{"HostIp": host_ip, "HostPort": "49152"}]},
        },
        "State": {"Running": running},
    }


def _config(
    data_root: Path,
    *,
    docker_data_root: Path | None = None,
    ssh_port: int = 0,
    fresh: bool = True,
) -> object:
    return fixture.FixtureConfig(
        name="fixture-test",
        image=fixture.DEFAULT_IMAGE,
        data_root=data_root,
        public_key=fixture.PublicKey(
            content=_public_key_bytes(),
            sha256="a" * 64,
        ),
        uid=1000,
        gid=1000,
        socket_gid=999,
        ssh_port=ssh_port,
        provenance_sha256=PROVENANCE_SHA256,
        data_root_fresh=fresh,
        docker_data_root=docker_data_root,
    )


def test_public_key_validation_accepts_one_public_key_and_normalizes_newline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "maintainer.pub"
    path.write_bytes(_public_key_bytes().rstrip(b"\n"))

    loaded = fixture._load_public_key(path)

    assert loaded.content == _public_key_bytes()
    assert loaded.sha256 == hashlib.sha256(_public_key_bytes()).hexdigest()


def test_public_key_comment_may_contain_spaces(tmp_path: Path) -> None:
    path = tmp_path / "maintainer.pub"
    path.write_bytes(_public_key_bytes("maintainer release host key"))

    assert fixture._load_public_key(path).content == path.read_bytes()


def test_root_maintainer_defaults_still_create_a_non_root_login() -> None:
    assert fixture.DEFAULT_UID > 0
    assert fixture.DEFAULT_GID > 0


def test_release_image_is_one_exact_linux_amd64_manifest() -> None:
    assert fixture.DEFAULT_IMAGE.startswith("docker.io/library/ubuntu@sha256:")
    assert fixture._validate_image(fixture.DEFAULT_IMAGE) == fixture.DEFAULT_IMAGE

    with pytest.raises(fixture.FixtureError, match="unsupported_image"):
        fixture._validate_image("ubuntu:24.04")


def test_prepare_data_root_requires_empty_root_or_closed_provenance(
    tmp_path: Path,
) -> None:
    fresh_root = tmp_path / "fresh"

    root, provenance_sha256, fresh = fixture._prepare_data_root(
        str(fresh_root),
        uid=1000,
        gid=1000,
    )

    assert root == fresh_root
    assert len(provenance_sha256) == 64
    assert fresh is True

    stale_root = tmp_path / "stale"
    stale_root.mkdir()
    (stale_root / "old-task-output").write_text("old", encoding="utf-8")
    with pytest.raises(fixture.FixtureError, match="data_root_not_fresh"):
        fixture._prepare_data_root(str(stale_root), uid=1000, gid=1000)


def test_prepare_data_root_recovers_only_closed_provenance(tmp_path: Path) -> None:
    (tmp_path / "task-output").write_text("kept", encoding="utf-8")

    root, provenance_sha256, fresh = fixture._prepare_data_root(
        str(tmp_path),
        uid=1000,
        gid=1000,
        existing_provenance_sha256=PROVENANCE_SHA256,
    )

    assert root == tmp_path
    assert provenance_sha256 == PROVENANCE_SHA256
    assert fresh is False


@pytest.mark.parametrize(
    "content",
    [
        b"-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n",
        _public_key_bytes() + _public_key_bytes("second"),
        b"ssh-ed25519 not-base64\n",
        b"command=whoami ssh-ed25519 AAAA\n",
    ],
)
def test_public_key_validation_rejects_non_public_key_input(
    tmp_path: Path,
    content: bytes,
) -> None:
    path = tmp_path / "invalid-key"
    path.write_bytes(content)

    with pytest.raises(fixture.FixtureError, match="invalid_public_key"):
        fixture._load_public_key(path)


def test_run_arguments_bind_the_data_root_and_localhost_only(tmp_path: Path) -> None:
    config = _config(tmp_path)

    arguments = fixture._run_arguments(config)
    joined = "\0".join(arguments)

    assert f"type=bind,src={tmp_path},dst={tmp_path}" in arguments
    assert "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock" in arguments
    assert "127.0.0.1::22" in arguments
    assert "--platform\0linux/amd64" in joined
    assert "\0".join(("--cpus", "4", "--memory", "4g", "--pids-limit", "2048")) in joined
    assert "--hostname" not in arguments
    assert _public_key_bytes().decode() not in joined


def test_run_arguments_support_a_docker_daemon_path_translation(tmp_path: Path) -> None:
    data_root = tmp_path / "container-data"
    docker_data_root = Path("/srv/openevo-data")
    config = _config(data_root, docker_data_root=docker_data_root)

    arguments = fixture._run_arguments(config)

    assert f"type=bind,src={docker_data_root},dst={data_root}" in arguments
    labels, destination, source, _, _, _ = fixture._validate_container_inspect(
        _inspect(data_root, docker_data_root=docker_data_root)
    )
    assert destination == data_root
    assert source == docker_data_root
    assert labels[fixture.LABEL_DATA_ROOT] == fixture._sha256_text(str(data_root))
    assert labels[fixture.LABEL_DOCKER_DATA_ROOT] == fixture._sha256_text(str(docker_data_root))


def test_inspect_validation_proves_mount_and_ssh_contract(tmp_path: Path) -> None:
    labels, data_root, docker_data_root, port, hostname, image_id = (
        fixture._validate_container_inspect(_inspect(tmp_path))
    )

    assert labels[LABEL_KIND := fixture.LABEL_KIND] == fixture.FIXTURE_KIND
    assert LABEL_KIND == "io.openevo.fixture.kind"
    assert data_root == tmp_path
    assert docker_data_root == tmp_path
    assert port == 49152
    assert hostname == CONTAINER_ID[:12]
    assert image_id == IMAGE_ID


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (lambda root: _inspect(root, running=False), "container_not_running"),
        (
            lambda root: _inspect(root, host_ip="0.0.0.0"),
            "ssh_port_contract_mismatch",
        ),
    ],
)
def test_inspect_validation_fails_closed(
    tmp_path: Path,
    value,
    code: str,
) -> None:
    with pytest.raises(fixture.FixtureError, match=code):
        fixture._validate_container_inspect(value(tmp_path))


def test_inspect_validation_rejects_different_container_data_path(tmp_path: Path) -> None:
    value = _inspect(tmp_path)
    value["Mounts"][0]["Destination"] = "/different/path"  # type: ignore[index]

    with pytest.raises(fixture.FixtureError, match="mount_contract_mismatch"):
        fixture._validate_container_inspect(value)


def test_inspect_validation_rejects_unbounded_container_resources(tmp_path: Path) -> None:
    value = _inspect(tmp_path)
    value["HostConfig"]["Memory"] = 0  # type: ignore[index]

    with pytest.raises(fixture.FixtureError, match="container_configuration_mismatch"):
        fixture._validate_container_inspect(value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["Config"].__setitem__("Hostname", "fixture-test"),
        lambda value: value.__setitem__("Platform", "windows"),
        lambda value: value["Config"].__setitem__("Image", "ubuntu:24.04"),
    ],
)
def test_inspect_validation_rejects_non_profile_identity(
    tmp_path: Path,
    mutate,
) -> None:
    value = _inspect(tmp_path)
    mutate(value)

    with pytest.raises(fixture.FixtureError, match="container_configuration_mismatch"):
        fixture._validate_container_inspect(value)


class _FakeDocker:
    def __init__(
        self,
        inspections: list[dict[str, object] | None],
        *,
        fail_setup: bool = False,
    ) -> None:
        self.executable = "/usr/bin/docker"
        self.deadline = time.monotonic() + 60
        self.inspections = inspections
        self.fail_setup = fail_setup
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []

    def run(
        self,
        *arguments: str,
        input_bytes: bytes | None = None,
        check: bool = True,
    ):
        del check
        self.calls.append((arguments, input_bytes))
        if arguments[:2] == ("container", "inspect"):
            value = self.inspections.pop(0)
            if value is None:
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([value]).encode(),
                stderr=b"",
            )
        if arguments[:2] == ("container", "ls"):
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if arguments[:2] == ("image", "inspect"):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Architecture": "amd64",
                            "Id": IMAGE_ID,
                            "Os": "linux",
                            "Variant": "",
                        }
                    ]
                ).encode(),
                stderr=b"",
            )
        if arguments[:2] == ("exec", "--interactive") and self.fail_setup:
            raise fixture.FixtureError("docker", "command_failed")
        if arguments[:2] == ("exec", "--user"):
            return SimpleNamespace(
                returncode=0,
                stdout=b"ubuntu\n24.04\nnoble\n",
                stderr=b"",
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


class _FakeDockerHost:
    def __init__(
        self,
        *,
        version: str = "29.3.0",
        api_version: str = "1.54",
        os_name: str = "linux",
        architecture: str = "amd64",
    ) -> None:
        self.identity = {
            "api_version": api_version,
            "architecture": architecture,
            "os": os_name,
            "version": version,
        }

    def run(self, *arguments: str):
        if arguments[:2] == ("context", "inspect"):
            output = b'"unix:///var/run/docker.sock"\n'
        elif arguments[:2] == ("version", "--format"):
            output = json.dumps(self.identity).encode()
        else:
            raise AssertionError(arguments)
        return SimpleNamespace(returncode=0, stdout=output, stderr=b"")


def test_docker_server_identity_is_closed_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    identity = fixture._verify_docker_host(_FakeDockerHost())

    assert identity == DOCKER_SERVER
    assert identity.evidence()["supported"] == {
        "api_versions": ["1.54"],
        "architecture": ["amd64"],
        "os": ["linux"],
        "versions": ["29.3.0"],
    }


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"version": "30.0.0"}, "unsupported_server_version"),
        ({"version": "23.0.6"}, "unsupported_server_version"),
        ({"version": "29.2.1"}, "unsupported_server_version"),
        ({"api_version": "1.55"}, "unsupported_server_api_version"),
        ({"api_version": "1.42"}, "unsupported_server_api_version"),
        ({"api_version": "1.53"}, "unsupported_server_api_version"),
        ({"os_name": "windows"}, "unsupported_server_os"),
        ({"architecture": "arm64"}, "unsupported_server_architecture"),
    ],
)
def test_docker_server_identity_fails_closed_outside_profile(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
    code: str,
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    with pytest.raises(fixture.FixtureError, match=code):
        fixture._verify_docker_host(_FakeDockerHost(**overrides))


def test_repeated_create_reuses_the_exact_running_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, fresh=False)
    docker = _FakeDocker([_inspect(tmp_path), _inspect(tmp_path)])
    monkeypatch.setattr(fixture, "_probe_ssh_banner", lambda *_args: None)

    result = fixture.create_fixture(docker, config, docker_server=DOCKER_SERVER)

    assert result["outcome"] == "ready"
    assert result["reused"] is True
    assert result["fixture"]["data_root_admission"] == "same_fixture_provenance"
    assert not any(arguments[0] == "run" for arguments, _ in docker.calls)
    assert not any(arguments[:2] == ("container", "rm") for arguments, _ in docker.calls)


def test_create_rejects_stale_provenance_without_the_same_fixture(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, fresh=False)
    docker = _FakeDocker([None])

    with pytest.raises(fixture.FixtureError, match="stale_data_root_provenance"):
        fixture.create_fixture(docker, config, docker_server=DOCKER_SERVER)

    assert not any(arguments[0] == "run" for arguments, _ in docker.calls)


def test_repeated_create_rejects_mismatched_live_fixture_provenance(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, fresh=False)
    value = _inspect(tmp_path)
    value["Config"]["Labels"][fixture.LABEL_PROVENANCE] = "e" * 64  # type: ignore[index]
    docker = _FakeDocker([value])

    with pytest.raises(fixture.FixtureError, match="fixture_configuration_conflict"):
        fixture.create_fixture(docker, config, docker_server=DOCKER_SERVER)

    assert not any(arguments[:2] == ("container", "start") for arguments, _ in docker.calls)


def test_nested_manager_falls_back_to_the_fixture_bridge_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, fresh=False)
    docker = _FakeDocker([_inspect(tmp_path), _inspect(tmp_path)])
    probes: list[tuple[str, int]] = []

    def probe(host: str, port: int, _deadline: float) -> None:
        probes.append((host, port))
        if host == "127.0.0.1":
            raise fixture.FixtureError("ssh", "ssh_not_reachable")

    monkeypatch.setattr(fixture, "_probe_ssh_banner", probe)

    result = fixture.create_fixture(docker, config, docker_server=DOCKER_SERVER)

    assert probes == [("127.0.0.1", 49152), ("172.17.0.11", 22)]
    assert result["fixture"]["ssh"] == {
        "host": "172.17.0.11",
        "port": 22,
        "reachable": True,
        "user": "openevo",
    }


def test_repeated_create_restarts_a_stopped_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, fresh=False)
    docker = _FakeDocker([_inspect(tmp_path, running=False), _inspect(tmp_path, running=True)])
    monkeypatch.setattr(fixture, "_probe_ssh_banner", lambda *_args: None)

    result = fixture.create_fixture(docker, config, docker_server=DOCKER_SERVER)

    assert result["outcome"] == "ready"
    assert any(
        arguments[:3] == ("container", "start", "fixture-test") for arguments, _ in docker.calls
    )


def test_create_failure_cleans_only_the_new_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    docker = _FakeDocker([None], fail_setup=True)
    monkeypatch.setattr(fixture, "DockerCLI", lambda *_args: docker)

    with pytest.raises(fixture.FixtureError) as captured:
        fixture.create_fixture(docker, config, docker_server=DOCKER_SERVER)

    assert captured.value.cleanup_attempted is True
    assert captured.value.cleanup_succeeded is True
    assert any(
        arguments[:4] == ("container", "rm", "--force", "--volumes")
        for arguments, _input in docker.calls
    )
    run_call = next(call for call in docker.calls if call[0][0] == "run")
    assert all(_public_key_bytes() not in argument.encode() for argument in run_call[0])


def test_timed_out_run_cleans_only_an_exactly_labelled_created_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    docker = _FakeDocker([None, _inspect(tmp_path)])
    original_run = docker.run

    def fail_run(*arguments: str, **kwargs):
        if arguments and arguments[0] == "run":
            raise fixture.FixtureError("docker", "operation_timed_out")
        return original_run(*arguments, **kwargs)

    docker.run = fail_run
    monkeypatch.setattr(fixture, "DockerCLI", lambda *_args: docker)

    with pytest.raises(fixture.FixtureError) as captured:
        fixture.create_fixture(docker, config, docker_server=DOCKER_SERVER)

    assert captured.value.cleanup_attempted is True
    assert captured.value.cleanup_succeeded is True
    assert any(arguments[:2] == ("container", "rm") for arguments, _ in docker.calls)


def test_existing_non_fixture_container_is_never_removed(tmp_path: Path) -> None:
    value = _inspect(tmp_path)
    value["Config"] = {"Labels": {}}
    docker = _FakeDocker([value])
    config = _config(tmp_path)

    with pytest.raises(fixture.FixtureError, match="container_name_conflict"):
        fixture.create_fixture(docker, config, docker_server=DOCKER_SERVER)

    assert not any(arguments[:2] == ("container", "rm") for arguments, _ in docker.calls)


def test_repeated_create_rejects_a_different_requested_ssh_port(tmp_path: Path) -> None:
    docker = _FakeDocker([_inspect(tmp_path)])
    config = _config(tmp_path, ssh_port=2222, fresh=False)

    with pytest.raises(fixture.FixtureError, match="fixture_configuration_conflict"):
        fixture.create_fixture(docker, config, docker_server=DOCKER_SERVER)

    assert not any(arguments[:2] == ("container", "rm") for arguments, _ in docker.calls)


def test_destroy_is_idempotent_when_fixture_is_absent() -> None:
    docker = _FakeDocker([None])

    result = fixture.destroy_fixture(
        docker,
        "fixture-test",
        docker_server=DOCKER_SERVER,
    )

    assert result == {
        "action": "destroy",
        "container_name": "fixture-test",
        "docker_server": DOCKER_SERVER.evidence(),
        "outcome": "absent",
        "release_host_profile": "docker_user_container_v1",
        "schema_version": 2,
    }


def test_destroy_removes_only_a_labelled_fixture(tmp_path: Path) -> None:
    docker = _FakeDocker([_inspect(tmp_path), None])

    result = fixture.destroy_fixture(
        docker,
        "fixture-test",
        docker_server=DOCKER_SERVER,
    )

    assert result["outcome"] == "destroyed"
    assert any(
        arguments[:4] == ("container", "rm", "--force", "--volumes")
        for arguments, _ in docker.calls
    )


def test_setup_installs_and_verifies_docker_cli() -> None:
    assert "docker.io" in fixture.SETUP_SCRIPT
    assert "docker version --format" in fixture.VERIFY_SCRIPT
    assert "http://localhost/_ping" in fixture.VERIFY_SCRIPT


def test_json_output_is_bounded_and_does_not_contain_key_or_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
    result = fixture._ready_result(
        "create",
        "fixture-test",
        49152,
        container_hostname=CONTAINER_ID[:12],
        docker_server=DOCKER_SERVER,
        guest_os={
            "codename": "noble",
            "distribution": "ubuntu",
            "version": "24.04",
        },
        image={
            "architecture": "amd64",
            "content_id": IMAGE_ID,
            "manifest_reference": fixture.DEFAULT_IMAGE,
            "os": "linux",
        },
        reused=False,
    )

    fixture._emit(result)

    encoded = output.getvalue()
    assert result["fixture"]["data_root_admission"] == "fresh_empty"
    assert result["release_host_profile"] == "docker_user_container_v1"
    assert len(encoded) <= fixture.MAX_JSON_OUTPUT_BYTES
    assert str(tmp_path).encode() not in encoded
    assert _public_key_bytes().rstrip() not in encoded
    assert json.loads(encoded) == result


def test_failure_json_uses_only_closed_error_fields() -> None:
    error = fixture.FixtureError(
        "create",
        "command_failed",
        cleanup_attempted=True,
        cleanup_succeeded=False,
    )

    assert fixture._failure_result("create", error) == {
        "action": "create",
        "cleanup": {"attempted": True, "succeeded": False},
        "failure": {"code": "command_failed", "stage": "create"},
        "outcome": "failed",
        "schema_version": 2,
    }


def test_argument_failure_is_one_bounded_json_record() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "create"],
        check=False,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 1
    assert result.stderr == b""
    assert len(result.stdout) <= fixture.MAX_JSON_OUTPUT_BYTES
    assert json.loads(result.stdout) == {
        "action": "create",
        "failure": {"code": "invalid_arguments", "stage": "arguments"},
        "outcome": "failed",
        "schema_version": 2,
    }
