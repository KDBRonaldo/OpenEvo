#!/usr/bin/env python3
"""Exercise the release archive state machine against a real Docker daemon."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

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


def _proxy_path(root: Path, real_docker: str, mode: str) -> str:
    proxy = root / "docker"
    proxy.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys, time\n"
        f"real = {real_docker!r}\n"
        f"mode = {mode!r}\n"
        f"oci = {MANAGED_RUNTIME_ARCHIVE_RELEASE.oci_index_id!r}\n"
        f"alias = {MANAGED_RUNTIME_ARCHIVE_RELEASE.aliases[0]!r}\n"
        f"log = {str(root / 'docker-proxy.log')!r}\n"
        "args = sys.argv[1:]\n"
        "with open(log, 'a', encoding='utf-8') as stream: stream.write(repr(args) + '\\n')\n"
        "if args == ['tag', oci, alias]:\n"
        "    if mode == 'fail_tag':\n"
        "        print('injected tag failure', file=sys.stderr); raise SystemExit(1)\n"
        "    if mode in {'cancel_tag', 'fail_remove'}:\n"
        "        result = subprocess.run([real, *args])\n"
        "        if result.returncode: raise SystemExit(result.returncode)\n"
        "        if mode == 'cancel_tag': time.sleep(120)\n"
        "        print('injected post-tag failure', file=sys.stderr); raise SystemExit(1)\n"
        "if mode == 'fail_remove' and args == ['image', 'rm', alias]:\n"
        "    print('permission denied', file=sys.stderr); raise SystemExit(1)\n"
        "fds = ()\n"
        "if args[:2] == ['load', '--input'] and args[2].startswith('/proc/self/fd/'):\n"
        "    fds = (int(args[2].rsplit('/', 1)[1]),)\n"
        "raise SystemExit(subprocess.run([real, *args], pass_fds=fds).returncode)\n",
        encoding="utf-8",
    )
    proxy.chmod(0o700)
    return str(root) + os.pathsep + os.environ["PATH"]


def _wait_for_alias(real_docker: str, alias: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
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


def _scenario(archive: Path, real_docker: str, mode: str) -> None:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    for alias in release.aliases:
        _remove_for_clean_gate(real_docker, alias)
    with tempfile.TemporaryDirectory(prefix=f"openevo-real-{mode}-") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir(mode=0o700)
        path = os.environ["PATH"]
        if mode != "success":
            path = _proxy_path(root, real_docker, mode)
        prepared = _prepare(home, path)
        _stage_archive(prepared, archive)
        arguments = _finalize_arguments(prepared)
        if mode == "success":
            receipt = _closed_json(_remote(home, arguments, path=path).stdout)
            if (
                receipt.get("config_id") != release.config_id
                or receipt.get("oci_index_id") != release.oci_index_id
            ):
                raise SmokeError("real Docker receipt identity is invalid")
            _assert_aliases(real_docker, present=True)
            return
        if mode == "cancel_tag":
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["PATH"] = path
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", _REMOTE_MANAGED_RUNTIME_SCRIPT, *arguments],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _wait_for_alias(real_docker, release.aliases[0], process)
            except SmokeError as exc:
                log = root / "docker-proxy.log"
                detail = log.read_text(encoding="utf-8") if log.exists() else "<no proxy log>"
                raise SmokeError(f"{exc}; Docker calls:\n{detail}") from exc
            process.terminate()
            if process.wait(timeout=30) == 0:
                raise SmokeError("cancelled real Docker finalize reported success")
        else:
            failed = _remote(home, arguments, path=path, check=False)
            if failed.returncode == 0:
                raise SmokeError(f"injected {mode} finalize reported success")
        receipts = home / ".openevo" / "core" / "managed-runtime-receipts"
        if mode == "fail_remove":
            if not any(item.name.endswith(".cleanup.json") for item in receipts.iterdir()):
                raise SmokeError("failed Docker removal did not persist cleanup authority")
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
                raise SmokeError("real Docker cleanup recovery did not return load_required")
            if list(receipts.iterdir()):
                raise SmokeError("real Docker cleanup recovery left authority state")
        _assert_aliases(real_docker, present=False)


def smoke(archive: Path, evidence_out: Path) -> None:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    verify_managed_runtime_archive(archive, release=release)
    real_docker = shutil.which("docker")
    if real_docker is None:
        raise SmokeError("Docker CLI is unavailable")
    server = _docker(real_docker, "version", "--format", "{{json .Server}}").stdout.strip()
    if not server:
        raise SmokeError("Docker daemon is unavailable")
    for image in (*release.aliases, release.oci_index_id):
        _remove_for_clean_gate(real_docker, image)
    try:
        for mode in ("fail_tag", "cancel_tag", "fail_remove", "success"):
            _scenario(archive, real_docker, mode)
    finally:
        for image in (*release.aliases, release.oci_index_id):
            _remove_for_clean_gate(real_docker, image)
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
    args = parser.parse_args()
    try:
        smoke(args.archive, args.evidence_out)
    except (OSError, SmokeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
