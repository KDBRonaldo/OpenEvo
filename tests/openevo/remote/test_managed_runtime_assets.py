from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import stat
import sys
import time

from pydantic import SecretStr
import pytest

from openevo.deployment import managed_runtime_assets as assets
from openevo.runtime.managed import ManagedRuntimeArchiveRelease


FILENAME = "openevo-science-runtime-0.1.1-linux-amd64.tar.gz"
ALIASES = ("openevo/science-runtime:0.1.1",)
CONFIG_ID = "sha256:" + "1" * 64
OCI_INDEX_ID = "sha256:" + "2" * 64


def _release(payload: bytes) -> ManagedRuntimeArchiveRelease:
    return ManagedRuntimeArchiveRelease(
        asset_release_id=356072935,
        asset_release_tag="openevo-managed-runtime-assets-v0.1.1",
        asset_id=481361975,
        asset_api_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
        filename=FILENAME,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        platform="linux-amd64",
        config_id=CONFIG_ID,
        oci_index_id=OCI_INDEX_ID,
        aliases=ALIASES,
    )


def test_snapshot_streams_exact_private_archive_and_rejects_late_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"offline-managed-runtime"
    release = _release(payload)
    monkeypatch.setattr(assets, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    archive = tmp_path / FILENAME
    archive.write_bytes(payload)
    archive.chmod(0o600)

    with assets.snapshot_managed_runtime_archive(
        archive_path=str(archive),
        archive_sha256=release.sha256,
        archive_size=release.byte_size,
    ) as snapshot:
        assert snapshot.archive_path.read_bytes() == payload
        assert snapshot.archive_path.stat().st_mode & 0o777 == 0o400

    original = tmp_path / "original"

    def replace_path(source: Path, _source_fd: int) -> None:
        source.rename(original)
        source.write_bytes(payload)
        source.chmod(0o600)

    monkeypatch.setattr(assets, "_after_managed_runtime_snapshot_open", replace_path)
    with pytest.raises(assets.ManagedRuntimeArchiveSnapshotError):
        with assets.snapshot_managed_runtime_archive(
            archive_path=str(archive),
            archive_sha256=release.sha256,
            archive_size=release.byte_size,
        ):
            pass


def test_snapshot_rejects_archive_made_public_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"offline-managed-runtime"
    release = _release(payload)
    monkeypatch.setattr(assets, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    archive = tmp_path / FILENAME
    archive.write_bytes(payload)
    archive.chmod(0o600)

    def make_public(source: Path, _source_fd: int) -> None:
        source.chmod(0o644)

    monkeypatch.setattr(assets, "_after_managed_runtime_snapshot_open", make_public)
    with pytest.raises(assets.ManagedRuntimeArchiveSnapshotError):
        with assets.snapshot_managed_runtime_archive(
            archive_path=str(archive),
            archive_sha256=release.sha256,
            archive_size=release.byte_size,
        ):
            pass


def test_snapshot_does_not_wrap_consumer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"offline-managed-runtime"
    release = _release(payload)
    monkeypatch.setattr(assets, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    archive = tmp_path / FILENAME
    archive.write_bytes(payload)
    archive.chmod(0o600)

    with pytest.raises(RuntimeError, match="consumer failed"):
        with assets.snapshot_managed_runtime_archive(
            archive_path=str(archive),
            archive_sha256=release.sha256,
            archive_size=release.byte_size,
        ):
            raise RuntimeError("consumer failed")


@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "writable", "oversize"])
def test_snapshot_rejects_unsealed_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    payload = b"offline-managed-runtime"
    release = _release(payload)
    monkeypatch.setattr(assets, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    archive = tmp_path / FILENAME
    archive.write_bytes(payload)
    archive.chmod(0o600)
    if mutation == "symlink":
        target = tmp_path / "target"
        archive.rename(target)
        archive.symlink_to(target)
    elif mutation == "hardlink":
        os.link(archive, tmp_path / "second-link")
    elif mutation == "writable":
        archive.chmod(0o660)
    else:
        archive.write_bytes(payload + b"x")

    with pytest.raises(assets.ManagedRuntimeArchiveSnapshotError):
        with assets.snapshot_managed_runtime_archive(
            archive_path=str(archive),
            archive_sha256=release.sha256,
            archive_size=release.byte_size,
        ):
            pass


def test_probe_and_receipt_protocol_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _release(b"runtime")
    monkeypatch.setattr(assets, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    assert (
        assets.parse_managed_runtime_probe(
            SecretStr('{"schema_version":2,"status":"load_required"}')
        )
        is None
    )
    payload = {
        "aliases": list(release.aliases),
        "archive_sha256": release.sha256,
        "archive_size": release.byte_size,
        "config_id": release.config_id,
        "oci_index_id": release.oci_index_id,
        "platform": release.platform,
        "reused": True,
        "schema_version": 2,
        "status": "ready",
    }
    receipt = assets.parse_managed_runtime_probe(
        SecretStr(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    )
    assert receipt is not None and receipt.reused is True

    payload["host_path"] = "/private/runtime/archive"
    with pytest.raises(ValueError):
        assets.parse_managed_runtime_probe(
            SecretStr(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        )

    duplicate = '{"schema_version":2,"schema_version":2,"status":"load_required"}'
    with pytest.raises(ValueError):
        assets.parse_managed_runtime_probe(SecretStr(duplicate))

    payload.pop("host_path")
    receipt_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    assert assets.parse_managed_runtime_receipt(SecretStr(receipt_payload)).reused is True
    payload["unexpected"] = True
    with pytest.raises(ValueError):
        assets.parse_managed_runtime_receipt(
            SecretStr(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        )


def test_prepare_transfer_rejects_path_or_inode_replacement() -> None:
    payload = {
        "incoming_device": 1,
        "incoming_inode": 3,
        "incoming_root": "/home/alice/.openevo/core/managed-runtime-staging/incoming-" + "a" * 32,
        "schema_version": 1,
        "service_root": "/home/alice/.openevo/core",
        "staging_device": 1,
        "staging_inode": 2,
        "transfer_id": "a" * 32,
    }
    transfer = assets.parse_managed_runtime_prepare(
        SecretStr(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    )
    command = assets.build_managed_runtime_rsync_path(transfer)
    assert assets.MANAGED_RUNTIME_TRANSFER_LEASE in command
    assert str(transfer.incoming_inode) in command

    payload["incoming_root"] = "/tmp/replaced"
    with pytest.raises(ValueError):
        assets.parse_managed_runtime_prepare(
            SecretStr(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        )

    payload["incoming_root"] = (
        "/home/alice/.openevo/core/managed-runtime-staging/incoming-" + "a" * 32
    )
    payload["schema_version"] = True
    with pytest.raises(ValueError):
        assets.parse_managed_runtime_prepare(
            SecretStr(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        )


def test_daemon_runtime_commands_require_no_remote_python_or_rsync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _release(b"runtime")
    monkeypatch.setattr(assets, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    transfer = assets.ManagedRuntimeTransfer(
        service_root="/home/alice/.openevo/core",
        incoming_root=("/home/alice/.openevo/core/managed-runtime-staging/incoming-" + "a" * 32),
        transfer_id="a" * 32,
        staging_device=1,
        staging_inode=2,
        incoming_device=1,
        incoming_inode=3,
    )
    daemon = "/home/alice/.openevo/daemon/openevo-daemon-linux-x86_64"
    commands = (
        assets.build_daemon_managed_runtime_probe_command(
            daemon,
            archive_sha256=release.sha256,
            archive_size=release.byte_size,
            platform=release.platform,
            config_id=release.config_id,
            oci_index_id=release.oci_index_id,
            aliases=release.aliases,
        ),
        assets.build_daemon_managed_runtime_prepare_command(
            daemon,
            archive_sha256=release.sha256,
            archive_size=release.byte_size,
        ),
        assets.build_daemon_managed_runtime_receive_command(
            daemon,
            transfer,
            archive_sha256=release.sha256,
            archive_size=release.byte_size,
        ),
        assets.build_daemon_managed_runtime_finalize_command(
            daemon,
            transfer,
            archive_sha256=release.sha256,
            archive_size=release.byte_size,
            platform=release.platform,
            config_id=release.config_id,
            oci_index_id=release.oci_index_id,
            aliases=release.aliases,
            load_timeout_seconds=30,
        ),
        assets.build_daemon_managed_runtime_discard_command(
            daemon,
            transfer,
            archive_sha256=release.sha256,
            archive_size=release.byte_size,
        ),
    )
    assert all(command.startswith(daemon + " managed-runtime ") for command in commands)
    assert all("python" not in command and "rsync" not in command for command in commands)
    assert " receive " in commands[2]


def test_remote_rsync_lease_rejects_intermediate_service_symlink(tmp_path: Path) -> None:
    if not Path("/usr/bin/rsync").is_file():
        pytest.skip("system rsync is unavailable")
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin)
    prepared = _prepare_remote(home, fake_bin, release)
    service = Path(str(prepared["service_root"]))
    relocated = service.with_name("core-relocated")
    service.rename(relocated)
    service.symlink_to(relocated, target_is_directory=True)

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            assets._REMOTE_RSYNC_LEASE_SCRIPT,
            str(service),
            str(prepared["transfer_id"]),
            str(prepared["staging_device"]),
            str(prepared["staging_inode"]),
            str(prepared["incoming_device"]),
            str(prepared["incoming_inode"]),
            str(release.byte_size),
            "/usr/bin/rsync",
            "--version",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode != 0


def _remote_run(
    home: Path,
    fake_bin: Path,
    *arguments: str,
    check: bool = True,
    input_payload: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _test_remote_script(fake_bin), *arguments],
        check=check,
        capture_output=True,
        input=input_payload,
        text=input_payload is None,
        env=environment,
        timeout=10,
    )
    if input_payload is None:
        return completed
    return subprocess.CompletedProcess(
        args=completed.args,
        returncode=completed.returncode,
        stdout=completed.stdout.decode("utf-8"),
        stderr=completed.stderr.decode("utf-8"),
    )


def _test_remote_script(fake_bin: Path) -> str:
    socket_path = fake_bin / "docker.sock"
    if not socket_path.exists():
        engine_socket = socket.socket(socket.AF_UNIX)
        try:
            engine_socket.bind(os.fspath(socket_path))
        finally:
            engine_socket.close()
        socket_path.chmod(0o660)
    return (
        assets._REMOTE_MANAGED_RUNTIME_SCRIPT.replace(
            'DOCKER = "/usr/bin/docker"',
            f"DOCKER = {os.fspath(fake_bin / 'docker')!r}",
            1,
        )
        .replace(
            'DOCKER_SOCKET = "/var/run/docker.sock"',
            f"DOCKER_SOCKET = {os.fspath(socket_path)!r}",
            1,
        )
        .replace(
            '"DOCKER_HOST": "unix:///var/run/docker.sock"',
            f'"DOCKER_HOST": "unix://{os.fspath(socket_path)}"',
            1,
        )
        .replace("executable.st_uid != 0", "executable.st_uid != uid", 1)
        .replace("engine_socket.st_uid != 0", "engine_socket.st_uid != uid", 1)
    )


def _fake_docker(
    fake_bin: Path,
    *,
    sleep_on_load: bool = False,
    sleep_after_tag: bool = False,
    fail_remove: bool = False,
    inspect_error: bool = False,
) -> None:
    fake_bin.mkdir(exist_ok=True)
    script = fake_bin / "docker"
    script.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, pathlib, sys, time",
                "state = pathlib.Path(__file__).parent.parent / 'home/docker-state.json'",
                f"image = {OCI_INDEX_ID!r}",
                "args = sys.argv[1:]",
                "if args[:2] == ['image', 'inspect']:",
                (
                    "    print('permission denied while resolving: no such image', "
                    "file=sys.stderr); raise SystemExit(1)"
                    if inspect_error
                    else "    pass"
                ),
                "    if not state.exists(): print('Error: No such image', file=sys.stderr); raise SystemExit(1)",
                "    value = json.loads(state.read_text())",
                "    if not value['loaded'] or (args[2] != image and args[2] not in value['aliases']):",
                "        print('Error: No such image', file=sys.stderr); raise SystemExit(1)",
                "    print(json.dumps([{'Id': image, 'Config': {'Labels': {'io.openevo.managed-runtime': 'true'}}}]))",
                "elif args[:1] == ['load']:",
                f"    time.sleep({30 if sleep_on_load else 0})",
                "    pathlib.Path(args[2]).read_bytes()",
                "    value = {'aliases': [], 'events': ['load:[]'], 'loaded': True}",
                "    state.write_text(json.dumps(value))",
                "elif args[:1] == ['tag']:",
                "    value = json.loads(state.read_text())",
                "    value['aliases'].append(args[2])",
                "    value['events'].append('tag:' + args[2])",
                "    state.write_text(json.dumps(value))",
                (
                    f"    if args[2] == {ALIASES[0]!r}: time.sleep(30)"
                    if sleep_after_tag
                    else "    pass"
                ),
                "elif args[:2] == ['image', 'rm']:",
                (
                    "    print('permission denied', file=sys.stderr); raise SystemExit(1)"
                    if fail_remove
                    else "    pass"
                ),
                "    if not state.exists(): print('Error: No such image', file=sys.stderr); raise SystemExit(1)",
                "    value = json.loads(state.read_text())",
                "    if args[2] not in value['aliases']:",
                "        print('Error: No such image', file=sys.stderr); raise SystemExit(1)",
                "    value['aliases'] = [alias for alias in value['aliases'] if alias != args[2]]",
                "    value['events'].append('rm:' + args[2])",
                "    state.write_text(json.dumps(value))",
                "else: raise SystemExit(2)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script.chmod(0o700)


def _prepare_remote(home: Path, fake_bin: Path, release: ManagedRuntimeArchiveRelease) -> dict:
    result = _remote_run(home, fake_bin, "prepare", release.sha256, str(release.byte_size))
    return json.loads(result.stdout)


def _probe_remote(
    home: Path,
    fake_bin: Path,
    release: ManagedRuntimeArchiveRelease,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _remote_run(
        home,
        fake_bin,
        "probe",
        release.sha256,
        str(release.byte_size),
        release.platform,
        release.config_id,
        release.oci_index_id,
        *release.aliases,
        check=check,
    )


def _finalize_arguments(
    prepared: dict[str, object],
    release: ManagedRuntimeArchiveRelease,
) -> tuple[str, ...]:
    return (
        "finalize",
        str(prepared["service_root"]),
        str(prepared["transfer_id"]),
        str(prepared["staging_device"]),
        str(prepared["staging_inode"]),
        str(prepared["incoming_device"]),
        str(prepared["incoming_inode"]),
        release.sha256,
        str(release.byte_size),
        release.platform,
        release.config_id,
        release.oci_index_id,
        "5",
        *release.aliases,
    )


def _receive_arguments(
    prepared: dict[str, object],
    release: ManagedRuntimeArchiveRelease,
) -> tuple[str, ...]:
    return (
        "receive",
        str(prepared["service_root"]),
        str(prepared["transfer_id"]),
        str(prepared["staging_device"]),
        str(prepared["staging_inode"]),
        str(prepared["incoming_device"]),
        str(prepared["incoming_inode"]),
        release.sha256,
        str(release.byte_size),
    )


def test_remote_probe_ignores_polluted_path_docker(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    polluted_bin = tmp_path / "polluted-bin"
    polluted_bin.mkdir()
    marker = tmp_path / "polluted-docker-ran"
    polluted_docker = polluted_bin / "docker"
    polluted_docker.write_text(
        f"#!/bin/sh\ntouch {os.fspath(marker)!r}\nexit 99\n",
        encoding="utf-8",
    )
    polluted_docker.chmod(0o755)
    environment = os.environ.copy()
    environment["HOME"] = os.fspath(home)
    environment["PATH"] = os.fspath(polluted_bin)
    definitions, separator, unused = assets._REMOTE_MANAGED_RUNTIME_SCRIPT.partition(
        "\naction = sys.argv[1]"
    )
    assert separator
    del unused
    probe_script = definitions + '\nrun_docker(["--version"], capture=True)\n'

    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            probe_script,
        ],
        check=True,
        capture_output=True,
        env=environment,
        timeout=10,
    )

    assert not marker.exists()


def test_remote_docker_process_receives_only_fixed_engine_environment(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    observed = tmp_path / "docker-environment.json"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os",
                f"open({os.fspath(observed)!r}, 'w').write(json.dumps(dict(os.environ)))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    script = _test_remote_script(fake_bin)
    definitions, separator, unused = script.partition("\naction = sys.argv[1]")
    assert separator
    del unused
    environment = os.environ.copy()
    environment.update(
        {
            "DOCKER_CONFIG": "/tmp/attacker-config",
            "DOCKER_CONTEXT": "attacker",
            "DOCKER_HOST": "tcp://attacker.invalid:2375",
            "HOME": "/tmp/attacker-home",
            "PATH": "/tmp/attacker-bin",
        }
    )

    subprocess.run(
        [sys.executable, "-I", "-c", definitions + '\nrun_docker(["--version"])\n'],
        check=True,
        capture_output=True,
        env=environment,
        timeout=10,
    )

    assert json.loads(observed.read_text(encoding="utf-8")) == {
        "DOCKER_CONFIG": "/proc/self",
        "DOCKER_HOST": f"unix://{fake_bin / 'docker.sock'}",
        "HOME": "/proc/self",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }


def test_remote_docker_rejects_unsafe_engine_socket_mode_before_exec(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "docker-ran"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"#!/bin/sh\ntouch {os.fspath(marker)!r}\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    script = _test_remote_script(fake_bin)
    (fake_bin / "docker.sock").chmod(0o666)
    definitions, separator, unused = script.partition("\naction = sys.argv[1]")
    assert separator
    del unused

    result = subprocess.run(
        [sys.executable, "-I", "-c", definitions + '\nrun_docker(["--version"])\n'],
        check=False,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert not marker.exists()


def test_remote_receive_streams_exact_archive_without_rsync(tmp_path: Path) -> None:
    payload = b"runtime"
    release = _release(payload)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin)
    prepared = _prepare_remote(home, fake_bin, release)

    result = _remote_run(
        home,
        fake_bin,
        *_receive_arguments(prepared, release),
        input_payload=payload,
    )

    assets.parse_managed_runtime_receive(SecretStr(result.stdout))
    archive = Path(str(prepared["incoming_root"])) / release.filename
    assert archive.read_bytes() == payload
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600


@pytest.mark.parametrize("payload", [b"short", b"runtime-extra"])
def test_remote_receive_rejects_partial_or_oversize_stream(
    tmp_path: Path,
    payload: bytes,
) -> None:
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin)
    prepared = _prepare_remote(home, fake_bin, release)

    result = _remote_run(
        home,
        fake_bin,
        *_receive_arguments(prepared, release),
        input_payload=payload,
        check=False,
    )

    assert result.returncode != 0
    assert not (Path(str(prepared["incoming_root"])) / release.filename).exists()


def test_remote_finalize_loads_exact_image_publishes_aliases_and_cleans_stage(
    tmp_path: Path,
) -> None:
    payload = b"runtime"
    release = _release(payload)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin)
    prepared = _prepare_remote(home, fake_bin, release)
    incoming = Path(str(prepared["incoming_root"]))
    archive = incoming / release.filename
    archive.write_bytes(payload)
    archive.chmod(0o600)

    result = _remote_run(home, fake_bin, *_finalize_arguments(prepared, release))
    receipt = json.loads(result.stdout)

    assert receipt["config_id"] == release.config_id
    assert receipt["oci_index_id"] == release.oci_index_id
    assert receipt["aliases"] == list(release.aliases)
    assert not incoming.exists()
    state = json.loads((home / "docker-state.json").read_text(encoding="utf-8"))
    assert state["aliases"] == list(release.aliases)
    assert state["events"][0] == "load:[]"


def test_remote_finalize_rejects_incoming_path_replacement_without_deleting_it(
    tmp_path: Path,
) -> None:
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin)
    prepared = _prepare_remote(home, fake_bin, release)
    incoming = Path(str(prepared["incoming_root"]))
    original = incoming.with_name(incoming.name + "-original")
    incoming.rename(original)
    incoming.mkdir(mode=0o700)
    (incoming / assets.MANAGED_RUNTIME_TRANSFER_LEASE).write_bytes(b"")
    (incoming / assets.MANAGED_RUNTIME_TRANSFER_LEASE).chmod(0o600)

    result = _remote_run(
        home,
        fake_bin,
        *_finalize_arguments(prepared, release),
        check=False,
    )

    assert result.returncode != 0
    assert incoming.is_dir()
    assert original.is_dir()
    assert not (home / "docker-state.json").exists()


def test_remote_finalize_cancellation_terminates_load_and_cleans_stage(
    tmp_path: Path,
) -> None:
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin, sleep_on_load=True)
    prepared = _prepare_remote(home, fake_bin, release)
    incoming = Path(str(prepared["incoming_root"]))
    archive = incoming / release.filename
    archive.write_bytes(b"runtime")
    archive.chmod(0o600)
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
    environment["OPENEVO_FAKE_DOCKER_STATE"] = str(home / "docker-state.json")
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            _test_remote_script(fake_bin),
            *_finalize_arguments(prepared, release),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    time.sleep(0.25)
    process.terminate()
    process.communicate(timeout=5)

    assert process.returncode != 0
    assert not incoming.exists()
    assert not (home / "docker-state.json").exists()

    _fake_docker(fake_bin)
    retry = _prepare_remote(home, fake_bin, release)
    retry_incoming = Path(str(retry["incoming_root"]))
    retry_archive = retry_incoming / release.filename
    retry_archive.write_bytes(b"runtime")
    retry_archive.chmod(0o600)
    receipt = json.loads(_remote_run(home, fake_bin, *_finalize_arguments(retry, release)).stdout)
    assert receipt["oci_index_id"] == release.oci_index_id
    assert not retry_incoming.exists()


def test_remote_finalize_cancellation_rolls_back_aliases_before_receipt(
    tmp_path: Path,
) -> None:
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin, sleep_after_tag=True)
    prepared = _prepare_remote(home, fake_bin, release)
    incoming = Path(str(prepared["incoming_root"]))
    archive = incoming / release.filename
    archive.write_bytes(b"runtime")
    archive.chmod(0o600)
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
    state_path = home / "docker-state.json"
    environment["OPENEVO_FAKE_DOCKER_STATE"] = str(state_path)
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            _test_remote_script(fake_bin),
            *_finalize_arguments(prepared, release),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            if json.loads(state_path.read_text(encoding="utf-8"))["aliases"] == [ALIASES[0]]:
                break
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.01)
    else:
        process.kill()
        process.communicate(timeout=5)
        raise AssertionError("finalize did not publish the managed Science alias")

    process.terminate()
    process.communicate(timeout=5)

    assert process.returncode != 0
    assert json.loads(state_path.read_text(encoding="utf-8"))["aliases"] == []
    assert not incoming.exists()
    receipts = home / ".openevo" / "core" / "managed-runtime-receipts"
    assert list(receipts.iterdir()) == []


def test_remote_prepare_response_loss_is_reconciled_before_retry(tmp_path: Path) -> None:
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin)

    lost = _prepare_remote(home, fake_bin, release)
    lost_incoming = Path(str(lost["incoming_root"]))
    assert lost_incoming.is_dir()

    retry = _prepare_remote(home, fake_bin, release)

    assert not lost_incoming.exists()
    assert Path(str(retry["incoming_root"])).is_dir()


def test_remote_probe_recovers_interrupted_receipt_publication(tmp_path: Path) -> None:
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin)
    _prepare_remote(home, fake_bin, release)
    receipts = home / ".openevo" / "core" / "managed-runtime-receipts"
    candidate = receipts / (".receipt-" + "a" * 32)
    final = receipts / (release.sha256 + ".json")
    payload = {
        "aliases": list(release.aliases),
        "archive_sha256": release.sha256,
        "archive_size": release.byte_size,
        "config_id": release.config_id,
        "oci_index_id": release.oci_index_id,
        "platform": release.platform,
        "reused": False,
        "schema_version": 2,
        "status": "ready",
    }
    candidate.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    candidate.chmod(0o600)
    os.link(candidate, final)

    result = _probe_remote(home, fake_bin, release)

    assert json.loads(result.stdout)["status"] == "load_required"
    assert not candidate.exists()
    assert final.stat().st_nlink == 1


def test_remote_probe_does_not_treat_daemon_error_as_missing_image(tmp_path: Path) -> None:
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin, inspect_error=True)

    result = _probe_remote(home, fake_bin, release, check=False)

    assert result.returncode != 0


def test_remote_rollback_persists_cleanup_authority_until_real_remove_succeeds(
    tmp_path: Path,
) -> None:
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin, sleep_after_tag=True, fail_remove=True)
    prepared = _prepare_remote(home, fake_bin, release)
    incoming = Path(str(prepared["incoming_root"]))
    archive = incoming / release.filename
    archive.write_bytes(b"runtime")
    archive.chmod(0o600)
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
    environment["OPENEVO_FAKE_DOCKER_STATE"] = str(home / "docker-state.json")
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            _test_remote_script(fake_bin),
            *_finalize_arguments(prepared, release),
        ],
        env=environment,
    )
    state_path = home / "docker-state.json"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            if json.loads(state_path.read_text())["aliases"] == [ALIASES[0]]:
                break
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.01)
    else:
        process.kill()
        process.wait(timeout=5)
        raise AssertionError("finalize did not publish the first alias")
    process.terminate()
    process.wait(timeout=5)

    receipts = home / ".openevo" / "core" / "managed-runtime-receipts"
    assert any(path.name.endswith(".cleanup.json") for path in receipts.iterdir())
    assert json.loads(state_path.read_text())["aliases"] == [ALIASES[0]]

    _fake_docker(fake_bin)
    recovered = _probe_remote(home, fake_bin, release)
    assert json.loads(recovered.stdout)["status"] == "load_required"
    assert json.loads(state_path.read_text())["aliases"] == []
    assert list(receipts.iterdir()) == []


def test_remote_probe_recovers_partial_upload_before_startup_retry(tmp_path: Path) -> None:
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin)
    prepared = _prepare_remote(home, fake_bin, release)
    incoming = Path(str(prepared["incoming_root"]))
    archive = incoming / release.filename
    archive.write_bytes(b"part")
    archive.chmod(0o600)

    result = _probe_remote(home, fake_bin, release)

    assert json.loads(result.stdout)["status"] == "load_required"
    assert not incoming.exists()


def test_remote_reconciliation_skips_held_lease_then_recovers_it(
    tmp_path: Path,
) -> None:
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin)
    prepared = _prepare_remote(home, fake_bin, release)
    incoming = Path(str(prepared["incoming_root"]))
    lease_fd = os.open(incoming / assets.MANAGED_RUNTIME_TRANSFER_LEASE, os.O_RDONLY)
    try:
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        retry = _prepare_remote(home, fake_bin, release)
        retry_incoming = Path(str(retry["incoming_root"]))
        assert incoming.is_dir()
        assert retry_incoming.is_dir()
    finally:
        os.close(lease_fd)

    _probe_remote(home, fake_bin, release)
    assert not incoming.exists()
    assert not retry_incoming.exists()


def test_remote_reconciliation_rejects_unknown_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin)
    prepared = _prepare_remote(home, fake_bin, release)
    incoming = Path(str(prepared["incoming_root"]))
    incoming.rename(incoming.with_name(incoming.name + "-saved"))
    outside = tmp_path / "outside"
    outside.mkdir()
    incoming.symlink_to(outside, target_is_directory=True)

    result = _probe_remote(home, fake_bin, release, check=False)

    assert result.returncode != 0
    assert incoming.is_symlink()
    assert outside.is_dir()


def test_remote_reconciliation_enforces_global_held_transfer_budget(
    tmp_path: Path,
) -> None:
    release = _release(b"runtime")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    _fake_docker(fake_bin)
    lease_fds: list[int] = []
    try:
        for _ in range(assets.MANAGED_RUNTIME_STAGING_MAX_TRANSFERS):
            prepared = _prepare_remote(home, fake_bin, release)
            incoming = Path(str(prepared["incoming_root"]))
            lease_fd = os.open(incoming / assets.MANAGED_RUNTIME_TRANSFER_LEASE, os.O_RDONLY)
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lease_fds.append(lease_fd)

        result = _remote_run(
            home,
            fake_bin,
            "prepare",
            release.sha256,
            str(release.byte_size),
            check=False,
        )
        assert result.returncode != 0
    finally:
        for lease_fd in lease_fds:
            os.close(lease_fd)
