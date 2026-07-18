#!/usr/bin/env python3
"""Manage the maintainer-only Docker release-host fixture.

This is E2E infrastructure, not a product CLI. It creates one SSH-reachable
Linux x86_64 container with a writable host bind mount and access to the host
Docker socket.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence


SCHEMA_VERSION = 2
FIXTURE_KIND = "openevo-docker-release-host-v1"
RELEASE_HOST_PROFILE = "docker_user_container_v1"
DEFAULT_NAME = "openevo-release-host-fixture"
DEFAULT_IMAGE = (
    "docker.io/library/ubuntu@"
    "sha256:52df9b1ee71626e0088f7d400d5c6b5f7bb916f8f0c82b474289a4ece6cf3faf"
)
EXPECTED_IMAGE_OS = "linux"
EXPECTED_IMAGE_ARCHITECTURE = "amd64"
EXPECTED_IMAGE_DISTRIBUTION = "ubuntu"
EXPECTED_IMAGE_VERSION = "24.04"
EXPECTED_IMAGE_CODENAME = "noble"
SUPPORTED_DOCKER_SERVER_VERSIONS = ("29.3.0",)
SUPPORTED_DOCKER_SERVER_API_VERSIONS = ("1.54",)
SSH_USER = "openevo"
DOCKER_SOCKET = Path("/var/run/docker.sock")
DEFAULT_UID = os.getuid() if os.getuid() > 0 else 1000
DEFAULT_GID = os.getgid() if os.getgid() > 0 else 1000
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024
MAX_JSON_OUTPUT_BYTES = 4096
MAX_PUBLIC_KEY_BYTES = 16 * 1024
MAX_PATH_BYTES = 1024
MIN_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 900.0
CLEANUP_TIMEOUT_SECONDS = 30.0
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
DOCKER_VERSION_PATTERN = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")
DOCKER_API_VERSION_PATTERN = re.compile(r"^([0-9]+)\.([0-9]+)$")
PUBLIC_KEY_TYPES = frozenset(
    {
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ecdsa-sha2-nistp256@openssh.com",
        "sk-ssh-ed25519@openssh.com",
        "ssh-ed25519",
        "ssh-rsa",
    }
)

LABEL_KIND = "io.openevo.fixture.kind"
LABEL_SCHEMA = "io.openevo.fixture.schema"
LABEL_DATA_ROOT = "io.openevo.fixture.data-root-sha256"
LABEL_DOCKER_DATA_ROOT = "io.openevo.fixture.docker-data-root-sha256"
LABEL_PUBLIC_KEY = "io.openevo.fixture.public-key-sha256"
LABEL_IMAGE = "io.openevo.fixture.image-sha256"
LABEL_UID = "io.openevo.fixture.uid"
LABEL_GID = "io.openevo.fixture.gid"
LABEL_SOCKET_GID = "io.openevo.fixture.socket-gid"
LABEL_SSH_PORT = "io.openevo.fixture.ssh-port"
LABEL_PROVENANCE = "io.openevo.fixture.provenance-sha256"

CONTAINER_COMMAND = """\
while [ ! -f /var/lib/openevo-release-host-fixture ]; do
    sleep 1
done
install -d -m 0755 /run/sshd
exec /usr/sbin/sshd -D -e
"""

SETUP_SCRIPT = r"""#!/bin/sh
set -eu

fixture_uid="$1"
fixture_gid="$2"
socket_gid="$3"
data_root="$4"
public_key_sha256="$5"
data_root_sha256="$6"
key_stage=/tmp/openevo-release-host-authorized-key

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    ca-certificates curl docker.io openssh-server

primary_group="$(getent group "$fixture_gid" | cut -d: -f1 || true)"
if [ -z "$primary_group" ]; then
    primary_group=openevo
    groupadd --gid "$fixture_gid" "$primary_group"
fi

if getent passwd openevo >/dev/null; then
    usermod --uid "$fixture_uid" --gid "$fixture_gid" \
        --home /home/openevo --shell /bin/bash openevo
else
    occupied_user="$(getent passwd "$fixture_uid" | cut -d: -f1 || true)"
    if [ -n "$occupied_user" ]; then
        usermod --login openevo --gid "$fixture_gid" --home /home/openevo \
            --move-home --shell /bin/bash "$occupied_user"
    else
        useradd --create-home --uid "$fixture_uid" --gid "$fixture_gid" \
            --home-dir /home/openevo --shell /bin/bash openevo
    fi
fi

socket_group="$(getent group "$socket_gid" | cut -d: -f1 || true)"
if [ -z "$socket_group" ]; then
    socket_group=openevo-docker
    groupadd --gid "$socket_gid" "$socket_group"
fi
usermod --append --groups "$socket_group" openevo
usermod --password '' openevo

install -d -m 0700 -o openevo -g "$fixture_gid" /home/openevo/.ssh
install -m 0600 -o openevo -g "$fixture_gid" \
    "$key_stage" /home/openevo/.ssh/authorized_keys
rm -f "$key_stage"
chown -R openevo:"$fixture_gid" /home/openevo

install -d -m 0755 /run/sshd /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-openevo-release-host.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
AllowUsers openevo
EOF
ssh-keygen -A

{
    printf 'schema_version=2\n'
    printf 'public_key_sha256=%s\n' "$public_key_sha256"
    printf 'data_root_sha256=%s\n' "$data_root_sha256"
} > /var/lib/openevo-release-host-fixture
chmod 0600 /var/lib/openevo-release-host-fixture

test -w "$data_root"
"""

VERIFY_SCRIPT = r"""set -eu
data_root="$1"
expected_key_sha256="$2"
expected_uid="$3"
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
test "$(id -u)" = "$expected_uid"
test -w "$data_root"
test "$(sha256sum "$HOME/.ssh/authorized_keys" | cut -d' ' -f1)" = \
    "$expected_key_sha256"
test "$(curl --fail --silent --show-error --max-time 5 \
    --unix-socket /var/run/docker.sock http://localhost/_ping)" = OK
test "$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')" = linux/amd64
. /etc/os-release
test "$ID" = ubuntu
test "$VERSION_ID" = 24.04
test "$VERSION_CODENAME" = noble
printf '%s\n%s\n%s\n' "$ID" "$VERSION_ID" "$VERSION_CODENAME"
"""


class FixtureError(RuntimeError):
    """A closed error whose fields are safe to include in fixture JSON."""

    def __init__(
        self,
        stage: str,
        code: str,
        *,
        cleanup_attempted: bool = False,
        cleanup_succeeded: bool = False,
    ) -> None:
        super().__init__(f"{stage}: {code}")
        self.stage = stage
        self.code = code
        self.cleanup_attempted = cleanup_attempted
        self.cleanup_succeeded = cleanup_succeeded


class ClosedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise FixtureError("arguments", "invalid_arguments")


@dataclass(frozen=True)
class PublicKey:
    content: bytes
    sha256: str


@dataclass(frozen=True)
class DockerServerIdentity:
    version: str
    api_version: str
    os: str
    architecture: str

    def evidence(self) -> dict[str, Any]:
        return {
            "observed": {
                "api_version": self.api_version,
                "architecture": self.architecture,
                "os": self.os,
                "version": self.version,
            },
            "supported": {
                "api_versions": list(SUPPORTED_DOCKER_SERVER_API_VERSIONS),
                "architecture": [EXPECTED_IMAGE_ARCHITECTURE],
                "os": [EXPECTED_IMAGE_OS],
                "versions": list(SUPPORTED_DOCKER_SERVER_VERSIONS),
            },
        }


@dataclass(frozen=True)
class FixtureConfig:
    name: str
    image: str
    data_root: Path
    public_key: PublicKey
    uid: int
    gid: int
    socket_gid: int
    ssh_port: int
    provenance_sha256: str
    data_root_fresh: bool
    docker_data_root: Path | None = None

    @property
    def resolved_docker_data_root(self) -> Path:
        return self.data_root if self.docker_data_root is None else self.docker_data_root

    @property
    def labels(self) -> dict[str, str]:
        return {
            LABEL_KIND: FIXTURE_KIND,
            LABEL_SCHEMA: str(SCHEMA_VERSION),
            LABEL_DATA_ROOT: _sha256_text(str(self.data_root)),
            LABEL_DOCKER_DATA_ROOT: _sha256_text(str(self.resolved_docker_data_root)),
            LABEL_PUBLIC_KEY: self.public_key.sha256,
            LABEL_IMAGE: _sha256_text(self.image),
            LABEL_UID: str(self.uid),
            LABEL_GID: str(self.gid),
            LABEL_SOCKET_GID: str(self.socket_gid),
            LABEL_SSH_PORT: str(self.ssh_port),
            LABEL_PROVENANCE: self.provenance_sha256,
        }


class DockerCLI:
    def __init__(self, executable: str, deadline: float) -> None:
        self.executable = executable
        self.deadline = deadline

    def run(
        self,
        *arguments: str,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise FixtureError("docker", "operation_timed_out")
        try:
            result = subprocess.run(
                [self.executable, *arguments],
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise FixtureError("docker", "operation_timed_out") from exc
        except OSError as exc:
            raise FixtureError("docker", "docker_unavailable") from exc
        if (
            len(result.stdout) > MAX_COMMAND_OUTPUT_BYTES
            or len(result.stderr) > MAX_COMMAND_OUTPUT_BYTES
        ):
            raise FixtureError("docker", "command_output_too_large")
        if check and result.returncode != 0:
            raise FixtureError("docker", "command_failed")
        return result


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_version(
    value: Any,
    *,
    pattern: re.Pattern[str],
    stage: str,
    code: str,
) -> tuple[int, ...]:
    if not isinstance(value, str):
        raise FixtureError(stage, code)
    match = pattern.fullmatch(value)
    if match is None:
        raise FixtureError(stage, code)
    return tuple(int(part) for part in match.groups())


def _bounded_text(value: bytes, *, stage: str) -> str:
    if len(value) > MAX_COMMAND_OUTPUT_BYTES:
        raise FixtureError(stage, "command_output_too_large")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FixtureError(stage, "invalid_command_output") from exc


def _parse_json(value: bytes, *, stage: str) -> Any:
    text = _bounded_text(value, stage=stage)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise FixtureError(stage, "invalid_json_output") from exc


def _validate_name(value: str) -> str:
    if not NAME_PATTERN.fullmatch(value):
        raise FixtureError("arguments", "invalid_container_name")
    return value


def _validate_image(value: str) -> str:
    if value != DEFAULT_IMAGE:
        raise FixtureError("arguments", "unsupported_image")
    return value


def _validate_id(value: int, *, field: str) -> int:
    if value <= 0 or value > 2**31 - 1:
        raise FixtureError("arguments", f"invalid_{field}")
    return value


def _validate_port(value: int) -> int:
    if value < 0 or value > 65535:
        raise FixtureError("arguments", "invalid_ssh_port")
    return value


def _validate_timeout(value: float) -> float:
    if not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
        raise FixtureError("arguments", "invalid_timeout")
    return value


def _load_public_key(path: Path) -> PublicKey:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise FixtureError("public_key", "public_key_unreadable") from exc
    if not content or len(content) > MAX_PUBLIC_KEY_BYTES or b"\x00" in content:
        raise FixtureError("public_key", "invalid_public_key")
    lines = content.splitlines()
    if len(lines) != 1:
        raise FixtureError("public_key", "invalid_public_key")
    try:
        line = lines[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise FixtureError("public_key", "invalid_public_key") from exc
    parts = line.split(maxsplit=2)
    if len(parts) not in {2, 3} or parts[0] not in PUBLIC_KEY_TYPES:
        raise FixtureError("public_key", "invalid_public_key")
    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FixtureError("public_key", "invalid_public_key") from exc
    encoded_type = parts[0].encode("ascii")
    if (
        len(decoded) < len(encoded_type) + 4
        or int.from_bytes(decoded[:4], "big") != len(encoded_type)
        or decoded[4 : 4 + len(encoded_type)] != encoded_type
    ):
        raise FixtureError("public_key", "invalid_public_key")
    normalized = lines[0] + b"\n"
    return PublicKey(content=normalized, sha256=hashlib.sha256(normalized).hexdigest())


def _prepare_data_root(
    value: str,
    *,
    uid: int,
    gid: int,
    existing_provenance_sha256: str | None = None,
) -> tuple[Path, str, bool]:
    if len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise FixtureError("data_root", "invalid_data_root")
    path = Path(value)
    if (
        not path.is_absolute()
        or path == Path("/")
        or "," in value
        or any(ord(character) < 32 for character in value)
    ):
        raise FixtureError("data_root", "invalid_data_root")
    try:
        canonical = path.resolve(strict=False)
    except OSError as exc:
        raise FixtureError("data_root", "invalid_data_root") from exc
    if canonical != path:
        raise FixtureError("data_root", "data_root_not_canonical")
    try:
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.is_dir():
            raise FixtureError("data_root", "data_root_not_directory")
        if not existed and os.geteuid() == 0:
            os.chown(path, uid, gid)
        nonempty = any(path.iterdir())
        if existing_provenance_sha256 is not None:
            if not SHA256_PATTERN.fullmatch(existing_provenance_sha256):
                raise FixtureError("data_root", "invalid_data_root_provenance")
            provenance_sha256 = existing_provenance_sha256
            fresh = False
        else:
            if nonempty:
                raise FixtureError("data_root", "data_root_not_fresh")
            provenance_sha256 = secrets.token_hex(32)
            fresh = True
        descriptor, probe_name = tempfile.mkstemp(
            dir=path,
            prefix=".openevo-fixture-write-probe-",
        )
        os.close(descriptor)
        Path(probe_name).unlink()
    except FixtureError:
        raise
    except OSError as exc:
        raise FixtureError("data_root", "data_root_not_writable") from exc
    return path, provenance_sha256, fresh


def _validate_docker_data_root(value: str) -> Path:
    if len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise FixtureError("docker_data_root", "invalid_docker_data_root")
    path = Path(value)
    if (
        not path.is_absolute()
        or path == Path("/")
        or path != Path(os.path.normpath(value))
        or "," in value
        or any(ord(character) < 32 for character in value)
    ):
        raise FixtureError("docker_data_root", "invalid_docker_data_root")
    return path


def _require_local_linux_host() -> int:
    if sys.platform != "linux" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise FixtureError("host", "linux_x86_64_required")
    try:
        metadata = DOCKER_SOCKET.stat()
    except OSError as exc:
        raise FixtureError("host", "docker_socket_unavailable") from exc
    if not stat.S_ISSOCK(metadata.st_mode):
        raise FixtureError("host", "docker_socket_unavailable")
    return metadata.st_gid


def _verify_docker_host(docker: DockerCLI) -> DockerServerIdentity:
    configured_host = os.environ.get("DOCKER_HOST")
    if configured_host not in {None, "", "unix:///var/run/docker.sock"}:
        raise FixtureError("docker_host", "remote_docker_host_forbidden")
    endpoint = _parse_json(
        docker.run(
            "context",
            "inspect",
            "--format",
            "{{json .Endpoints.docker.Host}}",
        ).stdout,
        stage="docker_host",
    )
    if endpoint != "unix:///var/run/docker.sock":
        raise FixtureError("docker_host", "remote_docker_host_forbidden")
    info = _parse_json(
        docker.run(
            "version",
            "--format",
            '{"api_version":{{json .Server.APIVersion}},'
            '"architecture":{{json .Server.Arch}},'
            '"os":{{json .Server.Os}},'
            '"version":{{json .Server.Version}}}',
        ).stdout,
        stage="docker_host",
    )
    if (
        type(info) is not dict
        or set(info) != {"api_version", "architecture", "os", "version"}
        or not all(isinstance(value, str) for value in info.values())
    ):
        raise FixtureError("docker_host", "invalid_server_identity")
    _parse_version(
        info["version"],
        pattern=DOCKER_VERSION_PATTERN,
        stage="docker_host",
        code="unsupported_server_version",
    )
    _parse_version(
        info["api_version"],
        pattern=DOCKER_API_VERSION_PATTERN,
        stage="docker_host",
        code="unsupported_server_api_version",
    )
    if info["version"] not in SUPPORTED_DOCKER_SERVER_VERSIONS:
        raise FixtureError("docker_host", "unsupported_server_version")
    if info["api_version"] not in SUPPORTED_DOCKER_SERVER_API_VERSIONS:
        raise FixtureError("docker_host", "unsupported_server_api_version")
    if info["os"] != EXPECTED_IMAGE_OS:
        raise FixtureError("docker_host", "unsupported_server_os")
    if info["architecture"] != EXPECTED_IMAGE_ARCHITECTURE:
        raise FixtureError("docker_host", "unsupported_server_architecture")
    return DockerServerIdentity(
        version=info["version"],
        api_version=info["api_version"],
        os=info["os"],
        architecture=info["architecture"],
    )


def _inspect_optional(docker: DockerCLI, name: str) -> dict[str, Any] | None:
    result = docker.run("container", "inspect", name, check=False)
    if result.returncode != 0:
        names = _bounded_text(
            docker.run(
                "container",
                "ls",
                "--all",
                "--filter",
                f"name=^/{name}$",
                "--format",
                "{{.Names}}",
            ).stdout,
            stage="inspect",
        ).splitlines()
        if name not in names:
            return None
        raise FixtureError("inspect", "container_inspect_failed")
    value = _parse_json(result.stdout, stage="inspect")
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        raise FixtureError("inspect", "invalid_container_inspect")
    return value[0]


def _fixture_labels(inspect: dict[str, Any]) -> dict[str, str]:
    config = inspect.get("Config")
    labels = config.get("Labels") if type(config) is dict else None
    if type(labels) is not dict or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        raise FixtureError("inspect", "invalid_container_inspect")
    return labels


def _require_fixture(inspect: dict[str, Any]) -> dict[str, str]:
    labels = _fixture_labels(inspect)
    required_labels = {
        LABEL_KIND,
        LABEL_SCHEMA,
        LABEL_DATA_ROOT,
        LABEL_DOCKER_DATA_ROOT,
        LABEL_PUBLIC_KEY,
        LABEL_IMAGE,
        LABEL_UID,
        LABEL_GID,
        LABEL_SOCKET_GID,
        LABEL_SSH_PORT,
        LABEL_PROVENANCE,
    }
    if (
        labels.get(LABEL_KIND) != FIXTURE_KIND
        or labels.get(LABEL_SCHEMA) != str(SCHEMA_VERSION)
        or not required_labels.issubset(labels)
        or any(
            not SHA256_PATTERN.fullmatch(labels[key])
            for key in (
                LABEL_DATA_ROOT,
                LABEL_DOCKER_DATA_ROOT,
                LABEL_PUBLIC_KEY,
                LABEL_IMAGE,
                LABEL_PROVENANCE,
            )
        )
    ):
        raise FixtureError("inspect", "container_name_conflict")
    try:
        uid = int(labels[LABEL_UID])
        gid = int(labels[LABEL_GID])
        socket_gid = int(labels[LABEL_SOCKET_GID])
        ssh_port = int(labels[LABEL_SSH_PORT])
    except ValueError as exc:
        raise FixtureError("inspect", "container_name_conflict") from exc
    if uid <= 0 or gid <= 0 or socket_gid < 0 or not 0 <= ssh_port <= 65535:
        raise FixtureError("inspect", "container_name_conflict")
    return labels


def _validate_mounts(
    inspect: dict[str, Any],
    labels: dict[str, str],
) -> tuple[Path, Path]:
    mounts = inspect.get("Mounts")
    if type(mounts) is not list:
        raise FixtureError("inspect", "invalid_container_inspect")
    bind_mounts: list[dict[str, Any]] = []
    socket_mount: dict[str, Any] | None = None
    for mount in mounts:
        if type(mount) is not dict or mount.get("Type") != "bind":
            continue
        if mount.get("Destination") == str(DOCKER_SOCKET):
            socket_mount = mount
        else:
            bind_mounts.append(mount)
    if (
        socket_mount is None
        or socket_mount.get("Source") != str(DOCKER_SOCKET)
        or socket_mount.get("RW") is not True
        or len(bind_mounts) != 1
    ):
        raise FixtureError("verify", "mount_contract_mismatch")
    data_mount = bind_mounts[0]
    source = data_mount.get("Source")
    destination = data_mount.get("Destination")
    if (
        not isinstance(source, str)
        or not isinstance(destination, str)
        or data_mount.get("RW") is not True
        or _sha256_text(destination) != labels.get(LABEL_DATA_ROOT)
        or _sha256_text(source) != labels.get(LABEL_DOCKER_DATA_ROOT)
    ):
        raise FixtureError("verify", "mount_contract_mismatch")
    return Path(destination), Path(source)


def _validate_container_inspect(
    inspect: dict[str, Any],
) -> tuple[dict[str, str], Path, Path, int, str, str]:
    labels = _require_fixture(inspect)
    config = inspect.get("Config")
    host_config = inspect.get("HostConfig")
    container_id = inspect.get("Id")
    image_id = inspect.get("Image")
    if (
        type(config) is not dict
        or not isinstance(config.get("Image"), str)
        or _sha256_text(config["Image"]) != labels[LABEL_IMAGE]
        or config["Image"] != DEFAULT_IMAGE
        or not isinstance(container_id, str)
        or not SHA256_PATTERN.fullmatch(container_id)
        or config.get("Hostname") != container_id[:12]
        or not isinstance(image_id, str)
        or not IMAGE_ID_PATTERN.fullmatch(image_id)
        or inspect.get("Platform") != EXPECTED_IMAGE_OS
        or config.get("Cmd") != ["sh", "-ceu", CONTAINER_COMMAND]
        or type(host_config) is not dict
        or host_config.get("NanoCpus") != 4_000_000_000
        or host_config.get("Memory") != 4 * 1024 * 1024 * 1024
        or host_config.get("PidsLimit") != 2048
        or host_config.get("RestartPolicy") != {"MaximumRetryCount": 0, "Name": "no"}
    ):
        raise FixtureError("verify", "container_configuration_mismatch")
    state = inspect.get("State")
    if type(state) is not dict or state.get("Running") is not True:
        raise FixtureError("verify", "container_not_running")
    data_root, docker_data_root = _validate_mounts(inspect, labels)
    network = inspect.get("NetworkSettings")
    ports = network.get("Ports") if type(network) is dict else None
    bindings = ports.get("22/tcp") if type(ports) is dict else None
    if type(bindings) is not list or len(bindings) != 1 or type(bindings[0]) is not dict:
        raise FixtureError("verify", "ssh_port_contract_mismatch")
    binding = bindings[0]
    try:
        port = int(binding.get("HostPort", ""))
    except (TypeError, ValueError) as exc:
        raise FixtureError("verify", "ssh_port_contract_mismatch") from exc
    if binding.get("HostIp") != "127.0.0.1" or not 1 <= port <= 65535:
        raise FixtureError("verify", "ssh_port_contract_mismatch")
    return labels, data_root, docker_data_root, port, container_id[:12], image_id


def _validate_image_inspect(
    docker: DockerCLI,
    *,
    expected_image_id: str,
) -> dict[str, str]:
    value = _parse_json(
        docker.run("image", "inspect", DEFAULT_IMAGE).stdout,
        stage="image",
    )
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        raise FixtureError("image", "invalid_image_inspect")
    image = value[0]
    if (
        image.get("Id") != expected_image_id
        or image.get("Os") != EXPECTED_IMAGE_OS
        or image.get("Architecture") != EXPECTED_IMAGE_ARCHITECTURE
        or image.get("Variant") not in {None, ""}
    ):
        raise FixtureError("image", "unsupported_image_identity")
    return {
        "architecture": EXPECTED_IMAGE_ARCHITECTURE,
        "content_id": expected_image_id,
        "manifest_reference": DEFAULT_IMAGE,
        "os": EXPECTED_IMAGE_OS,
    }


def _run_arguments(config: FixtureConfig) -> list[str]:
    arguments = [
        "run",
        "--detach",
        "--init",
        "--name",
        config.name,
        "--platform",
        "linux/amd64",
        "--pull",
        "missing",
        "--restart",
        "no",
        "--cpus",
        "4",
        "--memory",
        "4g",
        "--pids-limit",
        "2048",
        "--publish",
        f"127.0.0.1:{config.ssh_port or ''}:22",
        "--mount",
        f"type=bind,src={config.resolved_docker_data_root},dst={config.data_root}",
        "--mount",
        f"type=bind,src={DOCKER_SOCKET},dst={DOCKER_SOCKET}",
    ]
    for key, value in sorted(config.labels.items()):
        arguments.extend(("--label", f"{key}={value}"))
    arguments.extend((config.image, "sh", "-ceu", CONTAINER_COMMAND))
    return arguments


def _install_fixture(docker: DockerCLI, config: FixtureConfig) -> None:
    docker.run(
        "exec",
        "--interactive",
        config.name,
        "sh",
        "-ceu",
        "umask 077; cat > /tmp/openevo-release-host-authorized-key",
        input_bytes=config.public_key.content,
    )
    docker.run(
        "exec",
        "--interactive",
        config.name,
        "sh",
        "-s",
        "--",
        str(config.uid),
        str(config.gid),
        str(config.socket_gid),
        str(config.data_root),
        config.public_key.sha256,
        config.labels[LABEL_DATA_ROOT],
        input_bytes=SETUP_SCRIPT.encode("utf-8"),
    )


def _verify_inside(
    docker: DockerCLI,
    *,
    name: str,
    labels: dict[str, str],
    data_root: Path,
) -> dict[str, str]:
    required = (LABEL_PUBLIC_KEY, LABEL_UID)
    if any(label not in labels for label in required):
        raise FixtureError("verify", "fixture_labels_incomplete")
    result = docker.run(
        "exec",
        "--user",
        SSH_USER,
        name,
        "sh",
        "-ceu",
        VERIFY_SCRIPT,
        "--",
        str(data_root),
        labels[LABEL_PUBLIC_KEY],
        labels[LABEL_UID],
    )
    lines = _bounded_text(result.stdout, stage="verify").splitlines()
    if lines != [
        EXPECTED_IMAGE_DISTRIBUTION,
        EXPECTED_IMAGE_VERSION,
        EXPECTED_IMAGE_CODENAME,
    ]:
        raise FixtureError("verify", "unsupported_guest_os")
    return {
        "codename": lines[2],
        "distribution": lines[0],
        "version": lines[1],
    }


def _probe_ssh_banner(host: str, port: int, deadline: float) -> None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FixtureError("ssh", "ssh_not_reachable")
        try:
            with socket.create_connection(
                (host, port),
                timeout=min(2.0, remaining),
            ) as connection:
                connection.settimeout(min(2.0, remaining))
                banner = connection.recv(256)
            if banner.startswith(b"SSH-2.0-"):
                return
        except (OSError, TimeoutError):
            pass
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def _ready_result(
    action: str,
    name: str,
    port: int,
    *,
    container_hostname: str,
    docker_server: DockerServerIdentity,
    image: dict[str, str],
    guest_os: dict[str, str],
    reused: bool,
    translated_data_root: bool = False,
    ssh_host: str = "127.0.0.1",
) -> dict[str, Any]:
    return {
        "action": action,
        "fixture": {
            "container_name": name,
            "container_hostname": container_hostname,
            "data_root_admission": ("same_fixture_provenance" if reused else "fresh_empty"),
            "docker_data_root_translated": translated_data_root,
            "docker_socket_accessible": True,
            "guest_os": guest_os,
            "image": image,
            "platform": {"architecture": "x86_64", "system": "linux"},
            "ssh": {
                "host": ssh_host,
                "port": port,
                "reachable": True,
                "user": SSH_USER,
            },
        },
        "outcome": "ready",
        "release_host_profile": RELEASE_HOST_PROFILE,
        "reused": reused,
        "schema_version": SCHEMA_VERSION,
        "docker_server": docker_server.evidence(),
    }


def _bridge_address(inspect: dict[str, Any]) -> str:
    network = inspect.get("NetworkSettings")
    networks = network.get("Networks") if type(network) is dict else None
    if type(networks) is not dict or not 1 <= len(networks) <= 16:
        raise FixtureError("ssh", "ssh_not_reachable")
    addresses: list[str] = []
    for value in networks.values():
        address = value.get("IPAddress") if type(value) is dict else None
        if not isinstance(address, str):
            continue
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            parsed.version == 4
            and not parsed.is_loopback
            and not parsed.is_link_local
            and not parsed.is_multicast
            and not parsed.is_unspecified
        ):
            addresses.append(address)
    if len(set(addresses)) != 1:
        raise FixtureError("ssh", "ssh_not_reachable")
    return addresses[0]


def _check_fixture(
    docker: DockerCLI,
    name: str,
    *,
    action: str,
    docker_server: DockerServerIdentity,
    reused: bool,
) -> dict[str, Any]:
    inspect = _inspect_optional(docker, name)
    if inspect is None:
        raise FixtureError("inspect", "fixture_not_found")
    (
        labels,
        data_root,
        docker_data_root,
        port,
        container_hostname,
        image_id,
    ) = _validate_container_inspect(inspect)
    image = _validate_image_inspect(docker, expected_image_id=image_id)
    guest_os = _verify_inside(docker, name=name, labels=labels, data_root=data_root)
    ssh_host = "127.0.0.1"
    ssh_port = port
    try:
        _probe_ssh_banner(
            ssh_host,
            ssh_port,
            min(docker.deadline, time.monotonic() + 3.0),
        )
    except FixtureError:
        ssh_host = _bridge_address(inspect)
        ssh_port = 22
        _probe_ssh_banner(ssh_host, ssh_port, docker.deadline)
    return _ready_result(
        action,
        name,
        ssh_port,
        container_hostname=container_hostname,
        docker_server=docker_server,
        image=image,
        guest_os=guest_os,
        reused=reused,
        translated_data_root=docker_data_root != data_root,
        ssh_host=ssh_host,
    )


def _remove_fixture(docker: DockerCLI, name: str) -> None:
    docker.run("container", "rm", "--force", "--volumes", name)


def _cleanup_creation_attempt(
    docker: DockerCLI,
    config: FixtureConfig,
    *,
    run_completed: bool,
) -> bool:
    try:
        if not run_completed:
            inspect = _inspect_optional(docker, config.name)
            if inspect is None:
                return True
            labels = _require_fixture(inspect)
            if any(labels.get(key) != value for key, value in config.labels.items()):
                return False
        _remove_fixture(docker, config.name)
        return True
    except (FixtureError, KeyboardInterrupt):
        return False


def create_fixture(
    docker: DockerCLI,
    config: FixtureConfig,
    *,
    docker_server: DockerServerIdentity,
) -> dict[str, Any]:
    existing = _inspect_optional(docker, config.name)
    if existing is not None:
        labels = _require_fixture(existing)
        if any(labels.get(key) != value for key, value in config.labels.items()):
            raise FixtureError("create", "fixture_configuration_conflict")
        state = existing.get("State")
        if type(state) is not dict:
            raise FixtureError("inspect", "invalid_container_inspect")
        if state.get("Running") is not True:
            docker.run("container", "start", config.name)
        return _check_fixture(
            docker,
            config.name,
            action="create",
            docker_server=docker_server,
            reused=True,
        )

    creation_attempted = False
    run_completed = False
    try:
        if not config.data_root_fresh:
            raise FixtureError("data_root", "stale_data_root_provenance")
        try:
            if any(config.data_root.iterdir()):
                raise FixtureError("data_root", "data_root_not_fresh")
        except FixtureError:
            raise
        except OSError as exc:
            raise FixtureError("data_root", "data_root_not_writable") from exc
        creation_attempted = True
        run_result = docker.run(*_run_arguments(config), check=False)
        if run_result.returncode != 0:
            if b"bind source path does not exist" in run_result.stderr:
                raise FixtureError("data_root", "data_root_not_visible_to_daemon")
            raise FixtureError("docker", "command_failed")
        run_completed = True
        _install_fixture(docker, config)
        return _check_fixture(
            docker,
            config.name,
            action="create",
            docker_server=docker_server,
            reused=False,
        )
    except BaseException as exc:
        if not creation_attempted:
            raise
        cleanup = DockerCLI(
            docker.executable,
            time.monotonic() + CLEANUP_TIMEOUT_SECONDS,
        )
        cleanup_succeeded = _cleanup_creation_attempt(
            cleanup,
            config,
            run_completed=run_completed,
        )
        if isinstance(exc, KeyboardInterrupt):
            raise FixtureError(
                "create",
                "interrupted",
                cleanup_attempted=True,
                cleanup_succeeded=cleanup_succeeded,
            ) from exc
        if isinstance(exc, FixtureError):
            raise FixtureError(
                exc.stage,
                exc.code,
                cleanup_attempted=True,
                cleanup_succeeded=cleanup_succeeded,
            ) from exc
        raise


def destroy_fixture(
    docker: DockerCLI,
    name: str,
    *,
    docker_server: DockerServerIdentity,
) -> dict[str, Any]:
    inspect = _inspect_optional(docker, name)
    if inspect is None:
        return {
            "action": "destroy",
            "container_name": name,
            "docker_server": docker_server.evidence(),
            "outcome": "absent",
            "release_host_profile": RELEASE_HOST_PROFILE,
            "schema_version": SCHEMA_VERSION,
        }
    _require_fixture(inspect)
    _remove_fixture(docker, name)
    if _inspect_optional(docker, name) is not None:
        raise FixtureError("destroy", "fixture_still_present")
    return {
        "action": "destroy",
        "container_name": name,
        "docker_server": docker_server.evidence(),
        "outcome": "destroyed",
        "release_host_profile": RELEASE_HOST_PROFILE,
        "schema_version": SCHEMA_VERSION,
    }


def _parser() -> argparse.ArgumentParser:
    parser = ClosedArgumentParser(description=__doc__)
    parser.add_argument("--docker", default="docker", help="Docker CLI executable")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="Whole-operation deadline (5-900 seconds)",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    create = subparsers.add_parser("create", help="Create or verify the fixture")
    create.add_argument("--name", default=DEFAULT_NAME)
    create.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="Immutable release-profile image reference (overrides are unsupported)",
    )
    create.add_argument("--public-key", type=Path, required=True)
    create.add_argument("--data-root", required=True)
    create.add_argument(
        "--docker-data-root",
        help=(
            "Docker-daemon-visible bind source; defaults to --data-root when "
            "the fixture manager runs directly on the Docker host"
        ),
    )
    create.add_argument("--ssh-port", type=int, default=0)
    create.add_argument("--uid", type=int, default=DEFAULT_UID)
    create.add_argument("--gid", type=int, default=DEFAULT_GID)

    check = subparsers.add_parser("check", help="Check an existing fixture")
    check.add_argument("--name", default=DEFAULT_NAME)

    destroy = subparsers.add_parser("destroy", help="Destroy the fixture")
    destroy.add_argument("--name", default=DEFAULT_NAME)
    return parser


def _emit(value: dict[str, Any]) -> None:
    encoded = (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("ascii")
    if len(encoded) > MAX_JSON_OUTPUT_BYTES:
        raise FixtureError("output", "json_output_too_large")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _failure_result(action: str, exc: FixtureError) -> dict[str, Any]:
    result: dict[str, Any] = {
        "action": action,
        "failure": {"code": exc.code, "stage": exc.stage},
        "outcome": "failed",
        "schema_version": SCHEMA_VERSION,
    }
    if exc.cleanup_attempted:
        result["cleanup"] = {
            "attempted": True,
            "succeeded": exc.cleanup_succeeded,
        }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    action = next(
        (argument for argument in raw_arguments if argument in {"create", "check", "destroy"}),
        "unknown",
    )
    try:
        args = _parser().parse_args(raw_arguments)
        action = args.action
        timeout = _validate_timeout(args.timeout_seconds)
        _validate_name(args.name)
        executable = shutil.which(args.docker)
        if executable is None:
            raise FixtureError("docker", "docker_unavailable")
        socket_gid = _require_local_linux_host()
        docker = DockerCLI(executable, time.monotonic() + timeout)
        docker_server = _verify_docker_host(docker)

        if action == "create":
            uid = _validate_id(args.uid, field="uid")
            gid = _validate_id(args.gid, field="gid")
            image = _validate_image(args.image)
            public_key = _load_public_key(args.public_key)
            existing = _inspect_optional(docker, args.name)
            existing_provenance_sha256 = None
            if existing is not None:
                existing_provenance_sha256 = _require_fixture(existing)[LABEL_PROVENANCE]
            data_root, provenance_sha256, data_root_fresh = _prepare_data_root(
                args.data_root,
                uid=uid,
                gid=gid,
                existing_provenance_sha256=existing_provenance_sha256,
            )
            config = FixtureConfig(
                name=args.name,
                image=image,
                data_root=data_root,
                public_key=public_key,
                uid=uid,
                gid=gid,
                socket_gid=socket_gid,
                ssh_port=_validate_port(args.ssh_port),
                provenance_sha256=provenance_sha256,
                data_root_fresh=data_root_fresh,
                docker_data_root=_validate_docker_data_root(
                    args.docker_data_root or os.fspath(data_root)
                ),
            )
            result = create_fixture(
                docker,
                config,
                docker_server=docker_server,
            )
        elif action == "check":
            result = _check_fixture(
                docker,
                args.name,
                action="check",
                docker_server=docker_server,
                reused=True,
            )
        else:
            result = destroy_fixture(
                docker,
                args.name,
                docker_server=docker_server,
            )
        _emit(result)
        return 0
    except FixtureError as exc:
        _emit(_failure_result(action, exc))
        return 1
    except KeyboardInterrupt:
        _emit(_failure_result(action, FixtureError(action, "interrupted")))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
