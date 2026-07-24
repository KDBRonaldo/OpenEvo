#!/usr/bin/env python3
"""Launch a packaged OpenEvo Desktop sidecar and smoke static assets."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import posixpath
import re
import secrets
import signal
import shutil
import socket
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, Request

from pydantic import ValidationError


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from openevo_startup_diagnostics import (  # noqa: E402
    STARTUP_OUTPUT_LINE_MAX_BYTES,
    classify_stock_loader_line,
    unknown_output_fingerprint,
)

from desktop.sidecar.contracts.v2.models import (  # noqa: E402
    DesktopStateV2,
    DesktopVersionV2,
)
from openevo.deployment.ssh import (  # noqa: E402
    _SubprocessExitObserver,
    _confirm_owned_process_group_disappeared,
    _owned_process_group_id,
    _terminate_and_reap_subprocess,
)


EXPECTED_DESKTOP_METHOD_IDS = frozenset(
    {
        "agent_system_gepa_reflector",
        "skill_bundle_reflector",
        "text_memory_expel_reflector",
    }
)
CORE_OWNED_PROJECT_FIELDS = frozenset({"reflector_llm", "base_model"})
EXPECTED_DESKTOP_TARGET_IDS = frozenset({"agent_system", "skill_bundle", "text_memory"})
EXPECTED_ACTIVE_SELECTIONS = {
    "skill_bundle": "skill_bundle_reflector",
    "text_memory": "text_memory_reflector",
}
DESKTOP_SESSION_HEADER = "X-OpenEvo-Desktop-Session"
NATIVE_CHALLENGE_HEADER = "X-OpenEvo-Native-Challenge"
NATIVE_SIDECAR_PROTOCOL = "openevo-native-sidecar-v2"
NATIVE_LISTENER_FD_ENV = "OPENEVO_NATIVE_LISTENER_FD"
NATIVE_ARCHIVE_FD_ENV = "OPENEVO_NATIVE_EXECUTABLE_FD"
NATIVE_ARCHIVE_PATH_ENV = "OPENEVO_NATIVE_EXECUTABLE_PATH"
NATIVE_LISTENER_FD = 3
NATIVE_ARCHIVE_FD = 4
NATIVE_GUARD_MIN_FD = 64
NATIVE_SIDECAR_BASENAME = "openevo-desktop-sidecar"
NATIVE_ASKPASS_BASENAME = "openevo-ssh-askpass"
STARTUP_DIAGNOSTIC_SCAN_MAX_BYTES = 32 * 1024
STARTUP_DIAGNOSTIC_MAX_LINES = 8
_STARTUP_DIAGNOSTIC_PATTERN = re.compile(
    rb"OPENEVO_STARTUP_V1 stage=([a-z][a-z0-9_]*) "
    rb"code=([a-z][a-z0-9_]*)(?: errno=([1-9][0-9]{0,9}))?"
)
_STARTUP_DIAGNOSTIC_CODES = {
    "bootloader_resolver": frozenset(
        {
            "native_env_invalid",
            "native_path_unexpected",
            "native_path_invalid",
            "native_path_length_invalid",
            "native_path_not_canonical",
            "native_basename_invalid",
            "native_path_character_invalid",
            "native_path_resolve_failed",
            "native_identity_invalid",
            "resolved_path_length_invalid",
            "platform_unsupported",
            "handoff_prepare_failed",
            "native_env_incomplete",
        }
    ),
    "bootloader_archive": frozenset(
        {"native_fd_invalid", "platform_unsupported", "archive_open_failed"}
    ),
    "bootloader_handoff": frozenset(
        {
            "listener_fstat_failed",
            "archive_fstat_failed",
            "listener_type_invalid",
            "archive_type_invalid",
            "listener_accept_probe_failed",
            "listener_accept_size_invalid",
            "listener_not_accepting",
            "listener_info_probe_failed",
            "listener_identity_invalid",
            "listener_endpoint_probe_failed",
            "listener_endpoint_size_invalid",
            "listener_endpoint_invalid",
            "guard_state_invalid",
            "listener_guard_failed",
            "archive_guard_failed",
        }
    ),
    "bootloader_restore": frozenset(
        {"cloexec_clear_failed", "descriptor_restore_failed", "finish_failed"}
    ),
    "bootloader_exec": frozenset({"restore_failed"}),
    "bootloader_restart": frozenset({"restore_failed"}),
    "bootloader_child": frozenset({"handoff_finish_failed"}),
    "python_import": frozenset({"owned_subprocess_import_failed", "launcher_import_failed"}),
    "python_owned_subprocess": frozenset({"execution_failed"}),
    "python_handoff": frozenset({"listener_fd_invalid", "archive_fd_invalid"}),
    "python_metadata": frozenset({"load_failed"}),
    "python_launcher": frozenset(
        {
            "execution_failed",
            "bundled_core_assets_failed",
            "provider_store_v2_failed",
            "restart_reconciliation_v2_failed",
            "workspace_store_v2_failed",
            "ssh_catalog_v2_failed",
            "remote_lifecycle_v2_failed",
            "core_bridge_store_v2_failed",
            "event_broker_v2_failed",
            "core_adapter_v2_failed",
            "core_bridge_v2_failed",
            "core_runtime_v2_failed",
            "release_provider_v2_failed",
            "contract_app_v2_failed",
            "static_app_failed",
            "native_frame_failed",
            "native_routes_failed",
            "server_import_failed",
            "listener_failed",
            "server_failed",
            "shutdown_failed",
        }
    ),
}
_LOCAL_HTTP_OPENER = build_opener(ProxyHandler({}))


def _load_release_contract() -> tuple[str, str, str, tuple[str, ...]]:
    path = REPOSITORY_ROOT / "desktop" / "release-contract.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Desktop release contract is unavailable") from exc
    if type(payload) is not dict or set(payload) != {
        "accepted_openapi_digests",
        "allowed_provider_kinds",
        "required_feature_flags",
        "schema_version",
        "v019",
    }:
        raise RuntimeError("Desktop release contract does not use the closed schema")
    policy = payload.get("v019")
    digests = policy.get("accepted_desktop_openapi_digests") if type(policy) is dict else None
    event_digests = (
        policy.get("accepted_desktop_event_schema_digests")
        if type(policy) is dict
        else None
    )
    provider_kinds = policy.get("allowed_provider_kinds") if type(policy) is dict else None
    features = policy.get("required_desktop_feature_flags") if type(policy) is dict else None
    release_version = policy.get("release_version") if type(policy) is dict else None
    if (
        payload.get("schema_version") != "1"
        or type(digests) is not list
        or len(digests) != 1
        or type(digests[0]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", digests[0]) is None
        or type(event_digests) is not list
        or len(event_digests) != 1
        or type(event_digests[0]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", event_digests[0]) is None
        or release_version != "0.1.9"
        or provider_kinds != ["desktop_sidecar"]
        or type(features) is not list
        or not features
        or any(type(feature) is not str for feature in features)
        or len(features) != len(set(features))
        or features != sorted(features)
    ):
        raise RuntimeError("Desktop release contract is invalid")
    return digests[0], event_digests[0], release_version, tuple(features)


(
    EXPECTED_DESKTOP_OPENAPI_SHA256,
    EXPECTED_DESKTOP_EVENT_SCHEMA_SHA256,
    EXPECTED_DESKTOP_RELEASE_VERSION,
    REQUIRED_DESKTOP_FEATURE_FLAGS,
) = _load_release_contract()


class SmokeFailure(RuntimeError):
    """Raised when the packaged sidecar cannot serve the Desktop shell."""


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name not in {"href", "src"} or value is None:
                continue
            asset = _asset_reference(value)
            if asset is not None:
                self.assets.append(asset)


def _asset_reference(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    path = parsed.path
    if path.startswith("/assets/"):
        path = path[1:]
    elif not path.startswith("assets/"):
        return None
    normalized = posixpath.normpath(path)
    if normalized == "assets" or not normalized.startswith("assets/"):
        raise SmokeFailure(f"Invalid Desktop asset reference: {value}")
    return normalized


def _asset_references(index_html: str) -> list[str]:
    parser = _AssetParser()
    parser.feed(index_html)
    return sorted(set(parser.assets))


class _NativeCredentials:
    def __init__(
        self,
        *,
        instance_id: str,
        readiness_key: bytes,
        session_token: str,
        handoff_token: str,
    ) -> None:
        self.instance_id = instance_id
        self.readiness_key = readiness_key
        self.session_token = session_token
        self.handoff_token = handoff_token

    @classmethod
    def create(cls) -> "_NativeCredentials":
        return cls(
            instance_id=secrets.token_hex(16),
            readiness_key=secrets.token_bytes(32),
            session_token=secrets.token_hex(32),
            handoff_token=secrets.token_hex(32),
        )

    def frame(self) -> bytes:
        payload = {
            "protocol": NATIVE_SIDECAR_PROTOCOL,
            "instance_id": self.instance_id,
            "readiness_key": self.readiness_key.hex(),
            "session_token": self.session_token,
            "handoff_token": self.handoff_token,
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(encoded) + 1 > 512:
            raise SmokeFailure("native sidecar frame exceeded its byte limit")
        return encoded + b"\n"


def _duplicate_fd(fd: int) -> int:
    return int(fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, NATIVE_GUARD_MIN_FD))


@contextmanager
def _fixed_native_descriptors(listener_fd: int, archive_fd: int):
    listener_guard = _duplicate_fd(listener_fd)
    archive_guard = _duplicate_fd(archive_fd)
    saved: dict[int, int | None] = {}
    try:
        for target in (NATIVE_LISTENER_FD, NATIVE_ARCHIVE_FD):
            try:
                saved[target] = _duplicate_fd(target)
            except OSError as exc:
                if exc.errno != 9:
                    raise
                saved[target] = None
        os.dup2(listener_guard, NATIVE_LISTENER_FD, inheritable=True)
        os.dup2(archive_guard, NATIVE_ARCHIVE_FD, inheritable=True)
        yield
    finally:
        for target in (NATIVE_LISTENER_FD, NATIVE_ARCHIVE_FD):
            previous = saved.get(target)
            if previous is None:
                try:
                    os.close(target)
                except OSError:
                    pass
            else:
                os.dup2(previous, target, inheritable=False)
                os.close(previous)
        os.close(archive_guard)
        os.close(listener_guard)


def _abort_unobserved_sidecar(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _force_kill_anchored_process_group(
    process: subprocess.Popen[Any],
    *,
    process_group_id: int,
) -> None:
    if process.returncode is not None:
        return
    if (
        process.pid != process_group_id
        or process_group_id <= 1
        or process_group_id == os.getpgrp()
        or os.getpgid(process.pid) != process_group_id
    ):
        raise RuntimeError("native sidecar process-group authority changed")
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


def _launch_native_sidecar(
    sidecar: Path,
    *,
    config_root: Path,
) -> tuple[
    subprocess.Popen[bytes],
    str,
    _NativeCredentials,
    int,
    _SubprocessExitObserver,
]:
    helper_source: Path | None = None
    if sidecar.name.startswith(NATIVE_SIDECAR_BASENAME):
        target_suffix = sidecar.name.removeprefix(NATIVE_SIDECAR_BASENAME)
        helper_source = sidecar.with_name(f"{NATIVE_ASKPASS_BASENAME}{target_suffix}")
        try:
            helper_metadata = helper_source.lstat()
        except OSError as exc:
            raise SmokeFailure("packaged SSH askpass helper is unavailable") from exc
        if (
            not stat.S_ISREG(helper_metadata.st_mode)
            or stat.S_IMODE(helper_metadata.st_mode) != 0o755
            or helper_metadata.st_nlink != 1
            or not 0 < helper_metadata.st_size <= 16 * 1024 * 1024
        ):
            raise SmokeFailure("packaged SSH askpass helper metadata is invalid")
    launch_path = config_root.parent / NATIVE_SIDECAR_BASENAME
    shutil.copyfile(sidecar, launch_path)
    launch_path.chmod(0o500)
    helper_launch_path: Path | None = None
    if helper_source is not None:
        helper_launch_path = config_root.parent / NATIVE_ASKPASS_BASENAME
        shutil.copyfile(helper_source, helper_launch_path)
        helper_launch_path.chmod(0o755)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    archive_fd = os.open(launch_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    credentials = _NativeCredentials.create()
    child_env = dict(os.environ)
    for name in tuple(child_env):
        if name.startswith("_PYI_"):
            child_env.pop(name)
    child_env[NATIVE_LISTENER_FD_ENV] = str(NATIVE_LISTENER_FD)
    child_env[NATIVE_ARCHIVE_FD_ENV] = str(NATIVE_ARCHIVE_FD)
    child_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    execution_path = launch_path
    if sys.platform == "darwin":
        child_env[NATIVE_ARCHIVE_PATH_ENV] = str(launch_path)
    else:
        child_env.pop(NATIVE_ARCHIVE_PATH_ENV, None)
        execution_path = Path(f"/proc/self/fd/{NATIVE_ARCHIVE_FD}")
    command = [
        str(execution_path),
        "--listener-fd",
        str(NATIVE_LISTENER_FD),
        "--native-instance-stdin",
        "--release-assets-root",
        str((config_root.parent / "openevo-release-assets").absolute()),
        "--desktop-config-root",
        str(config_root),
    ]
    if helper_launch_path is not None:
        command.extend(("--ssh-askpass-helper-path", str(helper_launch_path)))
    try:
        with _fixed_native_descriptors(listener.fileno(), archive_fd):
            process = subprocess.Popen(
                command,
                executable=str(execution_path),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=child_env,
                pass_fds=(NATIVE_LISTENER_FD, NATIVE_ARCHIVE_FD),
                start_new_session=True,
            )
    finally:
        os.close(archive_fd)
        listener.close()
    if process.stdout is None:
        _abort_unobserved_sidecar(process)
        raise SmokeFailure("native sidecar output channel was not created")
    try:
        os.set_blocking(process.stdout.fileno(), False)
    except OSError:
        _abort_unobserved_sidecar(process)
        process.stdout.close()
        raise SmokeFailure("native sidecar output channel could not be bounded") from None
    if process.stdin is None:
        _abort_unobserved_sidecar(process)
        process.stdout.close()
        raise SmokeFailure("native sidecar frame channel was not created")
    try:
        process_group_id = _owned_process_group_id(process)
        exit_observer = _SubprocessExitObserver(process)
    except BaseException as exc:
        _abort_unobserved_sidecar(process)
        process.stdout.close()
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise SmokeFailure("native sidecar process authority could not be created") from None
    _send_native_frame(
        process,
        credentials,
        process_group_id=process_group_id,
        exit_observer=exit_observer,
    )
    return (
        process,
        f"http://127.0.0.1:{port}",
        credentials,
        process_group_id,
        exit_observer,
    )


def _send_native_frame(
    process: subprocess.Popen[bytes],
    credentials: _NativeCredentials,
    *,
    process_group_id: int,
    exit_observer: _SubprocessExitObserver,
) -> None:
    stream = process.stdin
    if stream is None:
        raise SmokeFailure("native sidecar frame channel was not created")
    frame = credentials.frame()
    try:
        if stream.write(frame) != len(frame):
            raise BrokenPipeError
        stream.close()
    except BaseException as exc:
        cancellation = exc if isinstance(exc, (KeyboardInterrupt, SystemExit)) else None
        try:
            stream.close()
        except BaseException as close_exc:
            if cancellation is None and isinstance(
                close_exc,
                (KeyboardInterrupt, SystemExit),
            ):
                cancellation = close_exc
        process.stdin = None
        if cancellation is None and isinstance(exc, Exception):
            try:
                failure = _process_failure(
                    process,
                    process_group_id=process_group_id,
                    exit_observer=exit_observer,
                )
            finally:
                if process.stdout is not None:
                    process.stdout.close()
            raise SmokeFailure(failure) from None
        try:
            _terminate(
                process,
                process_group_id=process_group_id,
                exit_observer=exit_observer,
            )
        finally:
            if process.stdout is not None:
                process.stdout.close()
        if cancellation is not None and cancellation is not exc:
            raise cancellation
        raise
    process.stdin = None


def _read_url(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
    timeout_seconds: float = 2.0,
) -> bytes:
    request = Request(url, headers=headers or {})
    try:
        with _LOCAL_HTTP_OPENER.open(request, timeout=timeout_seconds) as response:
            if response.status != expected_status:
                raise SmokeFailure(
                    f"{url} returned HTTP {response.status}; expected {expected_status}"
                )
            return response.read()
    except HTTPError as exc:
        if exc.code == expected_status:
            return exc.read()
        raise SmokeFailure(f"{url} returned HTTP {exc.code}; expected {expected_status}") from exc
    except (URLError, OSError) as exc:
        raise SmokeFailure(f"{url} was not reachable: {exc}") from exc


def _read_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    payload = json.loads(
        _read_url(
            url,
            headers=headers,
            expected_status=expected_status,
        ).decode("utf-8")
    )
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{url} did not return a JSON object")
    return payload


def _assert_capabilities(
    payload: dict[str, Any],
    *,
    execution_mode: str,
    expected_core_version: str,
) -> None:
    expected_generic_mode = (
        "subscription" if execution_mode == "codex_subscription_transcript" else "self_deployed"
    )
    if payload.get("schema_version") != "1":
        raise SmokeFailure("capabilities response has an unexpected schema version")
    if payload.get("core_version") != expected_core_version:
        raise SmokeFailure("capabilities response did not come from the exact Core wheel")
    registry_digest = payload.get("registry_digest")
    if (
        not isinstance(registry_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", registry_digest) is None
    ):
        raise SmokeFailure("capabilities response has an invalid registry digest")
    if payload.get("evaluated_profile") != {
        "execution_mode": expected_generic_mode,
        "capture_mode": "transcript",
        "harness_id": "codex",
        "harness_capabilities": [],
        "runtime_capabilities": [],
    }:
        raise SmokeFailure(f"capabilities profile mismatch for {execution_mode}")
    targets = payload.get("targets")
    if (
        not isinstance(targets, list)
        or not all(isinstance(target, dict) for target in targets)
        or {target.get("target_id") for target in targets} != EXPECTED_DESKTOP_TARGET_IDS
    ):
        raise SmokeFailure("capabilities response has an unexpected target set")
    methods = [
        method
        for target in targets
        if isinstance(target, dict)
        for method in target.get("methods", [])
        if isinstance(method, dict)
    ]
    if {method.get("method_id") for method in methods} != EXPECTED_DESKTOP_METHOD_IDS:
        raise SmokeFailure("capabilities response has an unexpected method set")
    for target in targets:
        if (
            target.get("effective_default_method_id") != target.get("configured_default_method_id")
            or target.get("configured_default_support", {}).get("overall") != "supported"
        ):
            raise SmokeFailure("capabilities response has an unsupported target default")
        for identity_key in (
            "implementation_identity_digest",
            "handler_identity_digest",
        ):
            identity = target.get(identity_key)
            if not isinstance(identity, str) or len(identity) != 64:
                raise SmokeFailure(f"capabilities target has invalid {identity_key}")
        accepted_methods = {
            method.get("method_id"): method
            for method in target.get("accepted_methods", [])
            if isinstance(method, dict)
        }
        active_selection = EXPECTED_ACTIVE_SELECTIONS.get(target.get("target_id"))
        if active_selection is not None and (
            active_selection not in accepted_methods
            or accepted_methods[active_selection].get("support", {}).get("overall") != "supported"
        ):
            raise SmokeFailure("capabilities response does not accept a current Science selection")
        if target.get("target_id") == "agent_system":
            resolvers = {
                resolver.get("selection_value"): resolver
                for resolver in target.get("selection_resolvers", [])
                if isinstance(resolver, dict)
            }
            auto = resolvers.get("auto")
            resolved = auto.get("resolved_methods", []) if isinstance(auto, dict) else []
            if {
                method.get("method_id")
                for method in resolved
                if isinstance(method, dict)
                and method.get("support", {}).get("overall") == "supported"
            } != {
                "agent_system_reflector",
                "agent_system_history_reflector",
            }:
                raise SmokeFailure("capabilities response does not support agent_system auto")
    for method in methods:
        _assert_project_method_contract(method)
        identity = method.get("implementation_identity_digest")
        if (
            not isinstance(identity, str)
            or len(identity) != 64
            or method.get("support", {}).get("overall") != "supported"
        ):
            raise SmokeFailure("capabilities response has an unsupported method")


def _assert_project_method_contract(method: dict[str, Any]) -> None:
    decoded: dict[str, dict[str, Any]] = {}
    for field_name in ("config_schema_json", "default_config_json"):
        encoded = method.get(field_name)
        try:
            value = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SmokeFailure(f"capabilities method has invalid {field_name}") from exc
        if not isinstance(value, dict):
            raise SmokeFailure(f"capabilities method has non-object {field_name}")
        decoded[field_name] = value

    properties = decoded["config_schema_json"].get("properties")
    leaked_fields = set(
        CORE_OWNED_PROJECT_FIELDS.intersection(properties if isinstance(properties, dict) else {})
    )
    leaked_fields.update(CORE_OWNED_PROJECT_FIELDS.intersection(decoded["default_config_json"]))
    if leaked_fields:
        raise SmokeFailure(
            "Core-owned field leaked into the Desktop project contract: "
            + ", ".join(sorted(leaked_fields))
        )


def _assert_release_version(payload: dict[str, Any]) -> None:
    try:
        version = DesktopVersionV2.model_validate_json(
            json.dumps(payload, separators=(",", ":"), sort_keys=True)
        )
    except ValidationError as exc:
        raise SmokeFailure("packaged sidecar returned an invalid release contract") from exc
    if version.openapi_sha256 != EXPECTED_DESKTOP_OPENAPI_SHA256:
        raise SmokeFailure("packaged sidecar returned an unreviewed OpenAPI digest")
    if (
        version.schema_version != "2"
        or version.api_name != "openevo-desktop-local-api"
        or version.provider_kind != "desktop_sidecar"
        or version.build_channel != "release"
        or version.preferred_major != 2
        or version.supported_majors != [2]
        or version.mutation_major != 2
        or version.event_schema_sha256 != EXPECTED_DESKTOP_EVENT_SCHEMA_SHA256
        or version.release_version != EXPECTED_DESKTOP_RELEASE_VERSION
        or tuple(version.feature_flags) != REQUIRED_DESKTOP_FEATURE_FLAGS
        or version.required_core_api_major != 2
        or not version.mutation_compatible
    ):
        raise SmokeFailure("packaged sidecar returned an invalid release contract")


def _assert_desktop_state(payload: dict[str, Any]) -> None:
    try:
        state = DesktopStateV2.model_validate_json(
            json.dumps(payload, separators=(",", ":"), sort_keys=True)
        )
    except ValidationError as exc:
        raise SmokeFailure("packaged sidecar returned an invalid Desktop state") from exc
    if (
        state.schema_version != "2"
        or state.active_profile_id is not None
        or state.active_project_id is not None
    ):
        raise SmokeFailure("packaged sidecar state does not bind the release contract")


def _terminate(
    process: subprocess.Popen[Any],
    *,
    process_group_id: int,
    exit_observer: _SubprocessExitObserver,
) -> None:
    authority_failure: BaseException | None = None
    try:
        if process.returncode is None:
            _terminate_and_reap_subprocess(
                process,
                process_group_id=process_group_id,
                exit_observer=exit_observer,
                on_group_cleanup_confirmed=lambda: None,
            )
        else:
            _confirm_owned_process_group_disappeared(
                process_group_id=process_group_id,
            )
    except BaseException as exc:
        authority_failure = exc
        try:
            _force_kill_anchored_process_group(
                process,
                process_group_id=process_group_id,
            )
        except BaseException as cleanup_exc:
            if isinstance(cleanup_exc, (KeyboardInterrupt, SystemExit)):
                raise cleanup_exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise exc
            raise SmokeFailure("native sidecar process-group cleanup failed") from None
    finally:
        exit_observer.close()
    if authority_failure is not None:
        if isinstance(authority_failure, (KeyboardInterrupt, SystemExit)):
            raise authority_failure
        raise SmokeFailure("native sidecar process-group authority failed closed") from None


def smoke_sidecar(
    sidecar: Path,
    *,
    timeout_seconds: float,
) -> None:
    if not sidecar.is_file():
        raise SmokeFailure(f"sidecar executable does not exist: {sidecar}")

    with TemporaryDirectory(prefix="openevo-sidecar-smoke-") as temporary_root:
        root = Path(temporary_root)
        (
            process,
            base_url,
            credentials,
            process_group_id,
            exit_observer,
        ) = _launch_native_sidecar(
            sidecar,
            config_root=root / "config",
        )
        try:
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                try:
                    exited = exit_observer.exited()
                except Exception:
                    raise SmokeFailure("native sidecar exit observation failed") from None
                if exited:
                    raise SmokeFailure(
                        _process_failure(
                            process,
                            process_group_id=process_group_id,
                            exit_observer=exit_observer,
                        )
                    )
                try:
                    challenge = secrets.token_hex(32)
                    health = _read_json(
                        f"{base_url}/openevo-native/health",
                        headers={NATIVE_CHALLENGE_HEADER: challenge},
                    )
                    domain = (
                        f"{NATIVE_SIDECAR_PROTOCOL}\0{credentials.instance_id}\0{challenge}"
                    ).encode("ascii")
                    expected_proof = hmac.new(
                        credentials.readiness_key,
                        domain,
                        hashlib.sha256,
                    ).hexdigest()
                    if health == {
                        "service": "openevo-sidecar",
                        "status": "ok",
                        "protocol": NATIVE_SIDECAR_PROTOCOL,
                        "instance_id": credentials.instance_id,
                        "instance_proof": expected_proof,
                    }:
                        break
                except SmokeFailure:
                    time.sleep(0.25)
            else:
                raise SmokeFailure(f"sidecar did not become healthy within {timeout_seconds}s")

            _assert_release_version(_read_json(f"{base_url}/version"))

            session_headers = {DESKTOP_SESSION_HEADER: credentials.session_token}
            _assert_desktop_state(
                _read_json(
                    f"{base_url}/desktop/v2/state",
                    headers=session_headers,
                )
            )
            _read_url(
                f"{base_url}/desktop/v2/state",
                expected_status=401,
            )
            _read_url(
                f"{base_url}/openevo-native/session",
                headers=session_headers,
                expected_status=204,
            )
            _read_url(
                f"{base_url}/openevo-native/session",
                expected_status=403,
            )
            _read_url(
                f"{base_url}/openevo-api/desktop/shell",
                expected_status=404,
            )
            _read_url(
                f"{base_url}/desktop/v1/state",
                headers=session_headers,
                expected_status=404,
            )

            index_html = _read_url(f"{base_url}/openevo").decode("utf-8")
            assets = _asset_references(index_html)
            if not assets:
                raise SmokeFailure("/openevo did not reference any packaged assets")
            for asset in assets:
                _read_url(f"{base_url}/{asset}")
        finally:
            assert process.stdout is not None
            try:
                _terminate(
                    process,
                    process_group_id=process_group_id,
                    exit_observer=exit_observer,
                )
            finally:
                process.stdout.close()


def _process_failure(
    process: subprocess.Popen[Any],
    *,
    process_group_id: int,
    exit_observer: _SubprocessExitObserver,
) -> str:
    authority_failed = False
    try:
        _terminate(
            process,
            process_group_id=process_group_id,
            exit_observer=exit_observer,
        )
    except SmokeFailure:
        authority_failed = True
    failure = _render_process_failure(process.returncode, process.stdout)
    if authority_failed:
        failure += "\nnative sidecar process-group authority also failed closed"
    return failure


def _render_process_failure(returncode: int | None, output) -> str:
    diagnostics = _read_startup_diagnostics(output) if output is not None else ()
    detail = (
        "startup diagnostics:\n" + "\n".join(diagnostics)
        if diagnostics
        else "no valid OPENEVO_STARTUP_V1 diagnostic was emitted"
    )
    return (
        "sidecar exited before proving native health "
        f"(exit {returncode}).\n{detail}"
    )


def _read_startup_diagnostics(process_output) -> tuple[str, ...]:
    payload = bytearray()
    while len(payload) <= STARTUP_DIAGNOSTIC_SCAN_MAX_BYTES:
        try:
            chunk = os.read(
                process_output.fileno(),
                STARTUP_DIAGNOSTIC_SCAN_MAX_BYTES + 1 - len(payload),
            )
        except BlockingIOError:
            break
        except OSError:
            return ()
        if not chunk:
            break
        payload.extend(chunk)
    scan_exhausted = len(payload) > STARTUP_DIAGNOSTIC_SCAN_MAX_BYTES
    bounded = payload[:STARTUP_DIAGNOSTIC_SCAN_MAX_BYTES]
    if scan_exhausted:
        final_newline = bounded.rfind(b"\n")
        complete = bounded[: final_newline + 1] if final_newline >= 0 else b""
        lines = complete.split(b"\n")
    else:
        lines = bounded.split(b"\n")
    diagnostics: list[str] = []
    unknown_lines: list[bytes] = []
    for raw_line in lines:
        if not raw_line:
            continue
        line = bytes(raw_line)
        match = _STARTUP_DIAGNOSTIC_PATTERN.fullmatch(line)
        if match is not None:
            stage = match.group(1).decode("ascii")
            code = match.group(2).decode("ascii")
            if code in _STARTUP_DIAGNOSTIC_CODES.get(stage, ()):
                if len(diagnostics) < STARTUP_DIAGNOSTIC_MAX_LINES:
                    diagnostics.append(line.decode("ascii"))
                continue
        classification = classify_stock_loader_line(line)
        if classification is not None:
            if len(diagnostics) < STARTUP_DIAGNOSTIC_MAX_LINES:
                diagnostics.append(
                    "OPENEVO_STARTUP_CLASSIFIED_V1 "
                    f"stage={classification.stage} code={classification.code}"
                )
            continue
        unknown_lines.append(line[:STARTUP_OUTPUT_LINE_MAX_BYTES])
    if scan_exhausted:
        unknown_lines.append(b"scan_exhausted")
    summary = unknown_output_fingerprint(unknown_lines)
    if summary is not None:
        count, fingerprint = summary
        unknown_diagnostic = (
            "OPENEVO_STARTUP_UNKNOWN_V1 category=unclassified "
            f"count={count} fingerprint=sha256:{fingerprint}"
        )
        if len(diagnostics) == STARTUP_DIAGNOSTIC_MAX_LINES:
            diagnostics[-1] = unknown_diagnostic
        else:
            diagnostics.append(unknown_diagnostic)
    return tuple(diagnostics)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sidecar", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)

    try:
        smoke_sidecar(
            args.sidecar,
            timeout_seconds=args.timeout_seconds,
        )
    except SmokeFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OpenEvo Desktop sidecar smoke passed: {args.sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
