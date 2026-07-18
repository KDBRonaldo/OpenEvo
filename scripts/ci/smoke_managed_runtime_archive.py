#!/usr/bin/env python3
"""Exercise the release archive state machine against a real Docker daemon."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Iterator

from openevo.deployment.managed_runtime_assets import _REMOTE_MANAGED_RUNTIME_SCRIPT
from openevo.runtime.managed import MANAGED_RUNTIME_ARCHIVE_RELEASE, verify_managed_runtime_archive


class SmokeError(RuntimeError):
    pass


def _closed_json(payload: str) -> object:
    def closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SmokeError("duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(payload, object_pairs_hook=closed_object)
    except (json.JSONDecodeError, RecursionError, UnicodeError) as exc:
        raise SmokeError("managed runtime smoke JSON is invalid") from exc


def _docker(
    real_docker: str, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [real_docker, *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _is_missing(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode != 0 and "no such image" in result.stderr.lower()


def _require_missing(real_docker: str, image: str) -> None:
    result = _docker(real_docker, "image", "inspect", image, check=False)
    if not _is_missing(result):
        raise SmokeError(f"Docker image must be absent: {image}")


def _remove_for_clean_gate(real_docker: str, image: str) -> None:
    result = _docker(real_docker, "image", "rm", image, check=False)
    if result.returncode != 0 and "no such image" not in result.stderr.lower():
        raise SmokeError(f"Docker cleanup failed for {image}: {result.stderr.strip()}")
    _require_missing(real_docker, image)


def _remote(
    home: Path,
    arguments: tuple[str, ...],
    *,
    path: str,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["PATH"] = path
    return subprocess.run(
        [sys.executable, "-I", "-c", _REMOTE_MANAGED_RUNTIME_SCRIPT, *arguments],
        check=check,
        capture_output=True,
        text=True,
        env=environment,
        timeout=timeout,
    )


def _prepare(home: Path, path: str) -> dict[str, object]:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    result = _remote(home, ("prepare", release.sha256, str(release.byte_size)), path=path)
    value = _closed_json(result.stdout)
    if not isinstance(value, dict):
        raise SmokeError("remote prepare response is invalid")
    return value


def _stage_archive(prepared: dict[str, object], archive: Path) -> None:
    incoming = Path(str(prepared["incoming_root"]))
    destination = incoming / MANAGED_RUNTIME_ARCHIVE_RELEASE.filename
    shutil.copyfile(archive, destination)
    destination.chmod(0o600)


def _finalize_arguments(prepared: dict[str, object]) -> tuple[str, ...]:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
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
        "120",
        *release.aliases,
    )


def _write_proxy(root: Path, real_docker: str, mode: str) -> Path:
    proxy = root / "docker"
    proxy.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, pathlib, subprocess, sys, time\n"
        f"real = {real_docker!r}\n"
        f"mode = {mode!r}\n"
        f"oci = {MANAGED_RUNTIME_ARCHIVE_RELEASE.oci_index_id!r}\n"
        f"alias = {MANAGED_RUNTIME_ARCHIVE_RELEASE.aliases[0]!r}\n"
        f"log = {str(root / 'docker-proxy.log')!r}\n"
        f"markers = pathlib.Path({str(root / 'docker-proxy-injections.log')!r})\n"
        "args = sys.argv[1:]\n"
        "with open(log, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(args, separators=(',', ':')) + '\\n')\n"
        "def mark(value):\n"
        "    with markers.open('a', encoding='utf-8') as stream: stream.write(value + '\\n')\n"
        "if args == ['tag', oci, alias]:\n"
        "    if mode == 'fail_tag':\n"
        "        mark('fail_tag')\n"
        "        print('injected tag failure', file=sys.stderr); raise SystemExit(1)\n"
        "    if mode in {'cancel_tag', 'fail_remove'}:\n"
        "        result = subprocess.run([real, *args])\n"
        "        if result.returncode: raise SystemExit(result.returncode)\n"
        "        if mode == 'cancel_tag':\n"
        "            mark('cancel_tag'); time.sleep(300)\n"
        "        mark('fail_remove_tag')\n"
        "        print('injected post-tag failure', file=sys.stderr); raise SystemExit(1)\n"
        "if mode == 'fail_remove' and args == ['image', 'rm', alias]:\n"
        "    mark('fail_remove_remove')\n"
        "    print('permission denied', file=sys.stderr); raise SystemExit(1)\n"
        "fds = ()\n"
        "if args[:2] == ['load', '--input'] and args[2].startswith('/proc/self/fd/'):\n"
        "    fds = (int(args[2].rsplit('/', 1)[1]),)\n"
        "raise SystemExit(subprocess.run([real, *args], pass_fds=fds).returncode)\n",
        encoding="utf-8",
    )
    proxy.chmod(0o700)
    return proxy


@contextmanager
def _mounted_fixed_docker_proxy(
    root: Path,
    real_docker: str,
    mode: str,
) -> Iterator[None]:
    proxy = _write_proxy(root, real_docker, mode)
    target = Path("/usr/bin/docker")
    mount = shutil.which("mount")
    umount = shutil.which("umount")
    if (
        os.geteuid() != 0
        or mount is None
        or umount is None
        or not target.is_file()
        or os.stat("/proc/self/ns/mnt").st_ino == os.stat("/proc/1/ns/mnt").st_ino
    ):
        raise SmokeError(
            "fixed Docker fault injection requires root in an isolated mount namespace"
        )
    subprocess.run(
        [mount, "--bind", os.fspath(proxy), os.fspath(target)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    failure: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            subprocess.run(
                [umount, os.fspath(target)],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = f"fixed Docker proxy unmount failed: {exc}"
            if failure is not None:
                failure.add_note(detail)
            else:
                raise SmokeError(detail) from exc


def _wait_for_alias(real_docker: str, alias: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        if _docker(real_docker, "image", "inspect", alias, check=False).returncode == 0:
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise SmokeError(
                "real Docker finalize exited before alias publication: "
                + (stderr or stdout or str(process.returncode)).strip()
            )
        time.sleep(0.1)
    raise SmokeError(f"timed out waiting for alias: {alias}")


def _terminate(process: subprocess.Popen[str]) -> int:
    returncode = process.poll()
    if returncode is not None:
        return returncode
    process.terminate()
    try:
        return process.wait(timeout=150)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            return process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise SmokeError("managed runtime finalize could not be stopped") from exc


def _assert_fault_injection(root: Path, mode: str) -> None:
    marker_path = root / "docker-proxy-injections.log"
    log_path = root / "docker-proxy.log"
    try:
        markers = marker_path.read_text(encoding="utf-8").splitlines()
        raw_calls = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SmokeError(f"{mode} Docker fault injection evidence is unavailable") from exc
    calls: list[tuple[str, ...]] = []
    for raw_call in raw_calls:
        value = _closed_json(raw_call)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise SmokeError(f"{mode} Docker proxy call log is invalid")
        calls.append(tuple(value))
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    tag_call = ("tag", release.oci_index_id, release.aliases[0])
    expected_markers = {
        "fail_tag": ["fail_tag"],
        "cancel_tag": ["cancel_tag"],
        "fail_remove": ["fail_remove_tag", "fail_remove_remove"],
    }[mode]
    if markers != expected_markers or tag_call not in calls:
        raise SmokeError(f"{mode} Docker fault injection did not reach its exact branch")
    if mode == "fail_remove" and ("image", "rm", release.aliases[0]) not in calls:
        raise SmokeError("fail_remove Docker fault injection did not intercept rollback")


def _assert_aliases(real_docker: str, *, present: bool) -> None:
    expected = MANAGED_RUNTIME_ARCHIVE_RELEASE.oci_index_id
    for alias in MANAGED_RUNTIME_ARCHIVE_RELEASE.aliases:
        result = _docker(real_docker, "image", "inspect", alias, check=False)
        if not present:
            if not _is_missing(result):
                raise SmokeError(f"rollback left alias: {alias}")
            continue
        if result.returncode != 0:
            raise SmokeError(f"published alias is unavailable: {alias}")
        payload = _closed_json(result.stdout)
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or payload[0].get("Id") != expected
            or payload[0].get("Config", {}).get("Labels", {}).get("io.openevo.managed-runtime")
            != "true"
        ):
            raise SmokeError(f"published alias identity is invalid: {alias}")


def _exercise_injected_finalize(
    *,
    home: Path,
    root: Path,
    arguments: tuple[str, ...],
    real_docker: str,
    mode: str,
) -> None:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    with _mounted_fixed_docker_proxy(root, real_docker, mode):
        if mode == "cancel_tag":
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", _REMOTE_MANAGED_RUNTIME_SCRIPT, *arguments],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                text=True,
            )
            try:
                _wait_for_alias(real_docker, release.aliases[0], process)
            except SmokeError as exc:
                log = root / "docker-proxy.log"
                detail = log.read_text(encoding="utf-8") if log.exists() else "<no proxy log>"
                raise SmokeError(f"{exc}; Docker calls:\n{detail}") from exc
            finally:
                returncode = _terminate(process)
            if returncode == 0:
                raise SmokeError("cancelled real Docker finalize reported success")
        else:
            failed = _remote(home, arguments, path=os.environ["PATH"], check=False)
            if failed.returncode == 0:
                raise SmokeError(f"injected {mode} finalize reported success")
    _assert_fault_injection(root, mode)


def _scenario(archive: Path, real_docker: str, mode: str) -> None:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    for alias in release.aliases:
        _remove_for_clean_gate(real_docker, alias)
    with tempfile.TemporaryDirectory(prefix=f"openevo-real-{mode}-") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir(mode=0o700)
        prepared = _prepare(home, os.environ["PATH"])
        _stage_archive(prepared, archive)
        arguments = _finalize_arguments(prepared)
        if mode == "success":
            receipt = _closed_json(_remote(home, arguments, path=os.environ["PATH"]).stdout)
            if (
                receipt.get("config_id") != release.config_id
                or receipt.get("oci_index_id") != release.oci_index_id
            ):
                raise SmokeError("real Docker receipt identity is invalid")
            _assert_aliases(real_docker, present=True)
            return
        _exercise_injected_finalize(
            home=home,
            root=root,
            arguments=arguments,
            real_docker=real_docker,
            mode=mode,
        )
        receipts = home / ".openevo" / "core" / "managed-runtime-receipts"
        if mode == "fail_remove":
            if not any(item.name.endswith(".cleanup.json") for item in receipts.iterdir()):
                raise SmokeError("failed Docker removal did not persist cleanup authority")
        elif list(receipts.iterdir()):
            raise SmokeError(f"{mode} rollback left receipt authority state")
        staging = Path(str(prepared["service_root"])) / "managed-runtime-staging"
        if list(staging.iterdir()):
            raise SmokeError(f"{mode} rollback left transfer state")
        recovered = _remote(
            home,
            (
                "probe",
                release.sha256,
                str(release.byte_size),
                release.platform,
                release.config_id,
                release.oci_index_id,
                *release.aliases,
            ),
            path=os.environ["PATH"],
        )
        if _closed_json(recovered.stdout) != {
            "schema_version": 2,
            "status": "load_required",
        }:
            raise SmokeError(f"{mode} cleanup recovery did not return load_required")
        if list(receipts.iterdir()):
            raise SmokeError(f"{mode} cleanup recovery left authority state")
        _assert_aliases(real_docker, present=False)


def _require_isolated_docker_authority(
    *,
    fixed_docker_fault_injection: bool,
    expected_socket_device: int,
    expected_socket_inode: int,
) -> str:
    docker = shutil.which("docker")
    if (
        docker != "/usr/bin/docker"
        or not fixed_docker_fault_injection
        or os.geteuid() != 0
        or os.environ.get("DOCKER_HOST") != "unix:///var/run/docker.sock"
        or expected_socket_device < 0
        or expected_socket_inode <= 0
        or os.stat("/proc/self/ns/mnt").st_ino == os.stat("/proc/1/ns/mnt").st_ino
    ):
        raise SmokeError("managed runtime smoke requires isolated fixed Docker fault injection")
    executable = os.stat(docker, follow_symlinks=False)
    python_link = os.lstat("/usr/bin/python3")
    python_executable = os.stat("/usr/bin/python3")
    python_parent = os.stat("/usr/bin", follow_symlinks=False)
    engine_socket = os.stat("/var/run/docker.sock", follow_symlinks=False)
    executable_mode = stat.S_IMODE(executable.st_mode)
    python_mode = stat.S_IMODE(python_executable.st_mode)
    python_parent_mode = stat.S_IMODE(python_parent.st_mode)
    socket_mode = stat.S_IMODE(engine_socket.st_mode)
    if (
        not stat.S_ISREG(executable.st_mode)
        or executable.st_uid != 0
        or executable.st_nlink < 1
        or not executable_mode & 0o111
        or executable_mode & 0o022
        or python_link.st_uid != 0
        or not stat.S_ISREG(python_executable.st_mode)
        or python_executable.st_uid != 0
        or python_executable.st_nlink < 1
        or not python_mode & 0o111
        or python_mode & 0o022
        or not stat.S_ISDIR(python_parent.st_mode)
        or python_parent.st_uid != 0
        or python_parent_mode & 0o022
        or not stat.S_ISSOCK(engine_socket.st_mode)
        or engine_socket.st_uid != 0
        or engine_socket.st_nlink != 1
        or socket_mode not in {0o600, 0o660}
        or (engine_socket.st_dev, engine_socket.st_ino)
        != (expected_socket_device, expected_socket_inode)
    ):
        raise SmokeError("isolated Docker executable or socket authority is invalid")
    return docker


def _cleanup_images(real_docker: str, images: tuple[str, ...]) -> list[str]:
    errors: list[str] = []
    for image in images:
        try:
            _remove_for_clean_gate(real_docker, image)
        except (OSError, SmokeError, subprocess.SubprocessError) as exc:
            errors.append(f"{image}: {exc}")
    return errors


def smoke(
    archive: Path,
    evidence_out: Path,
    *,
    fixed_docker_fault_injection: bool,
    expected_socket_device: int,
    expected_socket_inode: int,
) -> None:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    verify_managed_runtime_archive(archive, release=release)
    docker = _require_isolated_docker_authority(
        fixed_docker_fault_injection=fixed_docker_fault_injection,
        expected_socket_device=expected_socket_device,
        expected_socket_inode=expected_socket_inode,
    )
    images = (*release.aliases, release.oci_index_id)
    with tempfile.TemporaryDirectory(prefix="openevo-real-docker-authority-") as temporary:
        authority_root = Path(temporary)
        real_docker_copy = authority_root / "docker.real"
        shutil.copy2(docker, real_docker_copy)
        real_docker_copy.chmod(0o700)
        real_docker = os.fspath(real_docker_copy)
        server = _docker(real_docker, "version", "--format", "{{json .Server}}").stdout.strip()
        if not server:
            raise SmokeError("Docker daemon is unavailable")
        failure: BaseException | None = None
        try:
            initial_cleanup_errors = _cleanup_images(real_docker, images)
            if initial_cleanup_errors:
                raise SmokeError(
                    "isolated Docker clean gate failed: " + "; ".join(initial_cleanup_errors)
                )
            for mode in ("fail_tag", "cancel_tag", "fail_remove", "success"):
                _scenario(archive, real_docker, mode)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            cleanup_errors = _cleanup_images(real_docker, images)
            if cleanup_errors:
                detail = "isolated Docker final cleanup failed: " + "; ".join(cleanup_errors)
                if failure is not None:
                    failure.add_note(detail)
                else:
                    raise SmokeError(detail)
    evidence = {
        "archive": {
            "asset_api_digest": release.asset_api_digest,
            "byte_size": release.byte_size,
            "sha256": release.sha256,
        },
        "docker_server": _closed_json(server),
        "identities": {
            "config_id": release.config_id,
            "oci_index_id": release.oci_index_id,
        },
        "source": {
            "asset_id": release.asset_id,
            "asset_name": release.filename,
            "release_id": release.asset_release_id,
            "release_tag": release.asset_release_tag,
        },
        "scenarios": {
            "cancel_rollback": True,
            "tag_failure_rollback": True,
            "remove_failure_recovery": True,
            "success_explicit_aliases": True,
            "zero_alias_after_load": True,
        },
        "schema_version": 1,
    }
    evidence_out.write_text(
        json.dumps(evidence, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--evidence-out", type=Path, required=True)
    parser.add_argument("--fixed-docker-fault-injection", action="store_true")
    parser.add_argument("--expected-docker-socket-device", type=int, required=True)
    parser.add_argument("--expected-docker-socket-inode", type=int, required=True)
    args = parser.parse_args()
    try:
        smoke(
            args.archive,
            args.evidence_out,
            fixed_docker_fault_injection=args.fixed_docker_fault_injection,
            expected_socket_device=args.expected_docker_socket_device,
            expected_socket_inode=args.expected_docker_socket_inode,
        )
    except (OSError, SmokeError, subprocess.SubprocessError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        for note in getattr(exc, "__notes__", ()):
            print(f"cleanup note: {note}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
