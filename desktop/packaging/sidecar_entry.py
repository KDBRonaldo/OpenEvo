from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys

BUILD_METADATA_RELATIVE_PATH = Path("desktop/packaging/sidecar-build-metadata.json")
BUILD_METADATA_MAX_BYTES = 2048
NATIVE_EXECUTABLE_PATH_ENV = "OPENEVO_NATIVE_EXECUTABLE_PATH"
NATIVE_LISTENER_FD_ENV = "OPENEVO_NATIVE_LISTENER_FD"
NATIVE_EXECUTABLE_FD_ENV = "OPENEVO_NATIVE_EXECUTABLE_FD"
_PYTHON_STARTUP_DIAGNOSTICS = frozenset(
    {
        ("python_import", "owned_subprocess_import_failed"),
        ("python_owned_subprocess", "execution_failed"),
        ("python_import", "launcher_import_failed"),
        ("python_handoff", "listener_fd_invalid"),
        ("python_handoff", "archive_fd_invalid"),
        ("python_metadata", "load_failed"),
        ("python_launcher", "execution_failed"),
        ("python_launcher", "bundled_core_assets_failed"),
        ("python_launcher", "provider_store_v2_failed"),
        ("python_launcher", "restart_reconciliation_v2_failed"),
        ("python_launcher", "workspace_store_v2_failed"),
        ("python_launcher", "ssh_catalog_v2_failed"),
        ("python_launcher", "remote_lifecycle_v2_failed"),
        ("python_launcher", "core_bridge_store_v2_failed"),
        ("python_launcher", "event_broker_v2_failed"),
        ("python_launcher", "core_adapter_v2_failed"),
        ("python_launcher", "core_bridge_v2_failed"),
        ("python_launcher", "core_runtime_v2_failed"),
        ("python_launcher", "release_provider_v2_failed"),
        ("python_launcher", "contract_app_v2_failed"),
        ("python_launcher", "static_app_failed"),
        ("python_launcher", "native_frame_failed"),
        ("python_launcher", "native_routes_failed"),
        ("python_launcher", "server_import_failed"),
        ("python_launcher", "listener_failed"),
        ("python_launcher", "server_failed"),
        ("python_launcher", "shutdown_failed"),
    }
)


@dataclass(frozen=True)
class _PackagedAskpassHelper:
    architecture: str
    byte_size: int
    filename: str
    mode: str
    sha256: str
    signature: str
    target_triple: str


@dataclass(frozen=True)
class _PackagedBuildMetadata:
    source_commit: str
    ssh_askpass_helper: _PackagedAskpassHelper


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate packaged sidecar build metadata key")
        value[key] = item
    return value


def _load_packaged_build_metadata() -> _PackagedBuildMetadata:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    path = bundle_root / BUILD_METADATA_RELATIVE_PATH
    payload = path.read_bytes()
    if len(payload) > BUILD_METADATA_MAX_BYTES:
        raise ValueError("invalid packaged sidecar build metadata")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid packaged sidecar build metadata") from exc
    if type(value) is not dict or set(value) != {
        "schema_version",
        "source_commit",
        "ssh_askpass_helper",
    }:
        raise ValueError("invalid packaged sidecar build metadata")
    source_commit = value["source_commit"]
    helper = value["ssh_askpass_helper"]
    if (
        value["schema_version"] != "2"
        or type(source_commit) is not str
        or re.fullmatch(r"[0-9a-f]{7,40}", source_commit) is None
        or set(source_commit) == {"0"}
        or type(helper) is not dict
        or set(helper)
        != {
            "architecture",
            "byte_size",
            "filename",
            "mode",
            "sha256",
            "signature",
            "target_triple",
        }
    ):
        raise ValueError("invalid packaged sidecar build metadata")
    architecture = helper["architecture"]
    byte_size = helper["byte_size"]
    target_triple = helper["target_triple"]
    if (
        helper["filename"] != "openevo-ssh-askpass"
        or type(byte_size) is not int
        or not 0 < byte_size <= 16 * 1024 * 1024
        or type(helper["sha256"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", helper["sha256"]) is None
        or set(helper["sha256"]) == {"0"}
        or helper["mode"] != "0755"
        or type(architecture) is not str
        or architecture not in {"arm64", "x86_64"}
        or type(target_triple) is not str
    ):
        raise ValueError("invalid packaged sidecar build metadata")
    platform_identity = (architecture, target_triple, helper["signature"])
    if (
        (
            sys.platform == "darwin"
            and platform_identity
            not in {
                ("arm64", "aarch64-apple-darwin", "adhoc"),
                ("x86_64", "x86_64-apple-darwin", "adhoc"),
            }
        )
        or (
            sys.platform == "linux"
            and platform_identity != ("x86_64", "x86_64-unknown-linux-gnu", "none")
        )
        or sys.platform not in {"darwin", "linux"}
    ):
        raise ValueError("invalid packaged sidecar build metadata")
    return _PackagedBuildMetadata(
        source_commit=source_commit,
        ssh_askpass_helper=_PackagedAskpassHelper(**helper),
    )


def _emit_startup_diagnostic(stage: str, code: str) -> None:
    if (stage, code) not in _PYTHON_STARTUP_DIAGNOSTICS:
        raise ValueError("invalid Python startup diagnostic")
    print(f"OPENEVO_STARTUP_V1 stage={stage} code={code}", file=sys.stderr, flush=True)


def _startup_main() -> int:
    try:
        from openevo.deployment.system_executables import (
            OWNED_SUBPROCESS_BIRTH_ARGUMENT,
            SYSTEM_OPENSSH_OWNER_ARGUMENT,
            run_packaged_owned_subprocess_birth,
            run_packaged_system_openssh_owner,
        )
    except (Exception, SystemExit):
        _emit_startup_diagnostic("python_import", "owned_subprocess_import_failed")
        return 1

    if OWNED_SUBPROCESS_BIRTH_ARGUMENT in sys.argv:
        try:
            run_packaged_owned_subprocess_birth(sys.argv)
        except (Exception, SystemExit):
            _emit_startup_diagnostic("python_owned_subprocess", "execution_failed")
            return 1
        return 126
    if SYSTEM_OPENSSH_OWNER_ARGUMENT in sys.argv:
        try:
            run_packaged_system_openssh_owner(sys.argv)
        except (Exception, SystemExit):
            _emit_startup_diagnostic("python_owned_subprocess", "execution_failed")
            return 1
        return 126
    try:
        from desktop.server.launcher import PackagedLauncherStartupError, main
    except (Exception, SystemExit):
        _emit_startup_diagnostic("python_import", "launcher_import_failed")
        return 1

    if os.environ.pop(NATIVE_LISTENER_FD_ENV, None) != "3":
        _emit_startup_diagnostic("python_handoff", "listener_fd_invalid")
        return 1
    if os.environ.pop(NATIVE_EXECUTABLE_FD_ENV, None) != "4":
        _emit_startup_diagnostic("python_handoff", "archive_fd_invalid")
        return 1
    os.environ.pop(NATIVE_EXECUTABLE_PATH_ENV, None)
    try:
        metadata = _load_packaged_build_metadata()
    except (Exception, SystemExit):
        _emit_startup_diagnostic("python_metadata", "load_failed")
        return 1
    try:
        return main(
            packaged_source_commit=metadata.source_commit,
            packaged_askpass_helper_sha256=metadata.ssh_askpass_helper.sha256,
            packaged_askpass_helper_byte_size=metadata.ssh_askpass_helper.byte_size,
        )
    except PackagedLauncherStartupError as exc:
        _emit_startup_diagnostic("python_launcher", exc.code)
        return 1
    except (Exception, SystemExit):
        _emit_startup_diagnostic("python_launcher", "execution_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(_startup_main())
