from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Callable
from dataclasses import dataclass, field
from functools import partial
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import re
import secrets
import signal
import socket
import sys
import threading
from types import FrameType
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool

from desktop.server.app import create_desktop_app
from desktop.server.browser_host import (
    ManagedOpenSshHome,
    install_browser_host_routes,
)
from desktop.sidecar.release_app import (
    create_packaged_release_desktop_local_api_v2_app,
)
from desktop.sidecar.system_ssh_session import (
    AskpassHelperAuthority,
    SystemOpenSshHostTrust,
)
from desktop.sidecar.workspace_imports import WorkspaceImportError


DARWIN_DESKTOP_CONFIG_ROOT = Path("~/Library/Application Support/org.openevo.desktop")
LINUX_DESKTOP_CONFIG_ROOT = Path("~/.openevo/desktop")
DESKTOP_STATE_DIRECTORY = "state-v2"
NATIVE_INSTANCE_FRAME_MAX_BYTES = 512
NATIVE_SESSION_HEADER = "X-OpenEvo-Desktop-Session"
_NATIVE_SESSION_HEADER_BYTES = NATIVE_SESSION_HEADER.lower().encode("ascii")
NATIVE_HANDOFF_HEADER = "X-OpenEvo-Native-Handoff"
_NATIVE_HANDOFF_HEADER_BYTES = NATIVE_HANDOFF_HEADER.lower().encode("ascii")
NATIVE_CHALLENGE_HEADER = "X-OpenEvo-Native-Challenge"
_NATIVE_CHALLENGE_HEADER_BYTES = NATIVE_CHALLENGE_HEADER.lower().encode("ascii")
NATIVE_SIDECAR_PROTOCOL = "openevo-native-sidecar-v2"
_NATIVE_HEALTH_ROUTE = "/openevo-native/health"
_NATIVE_SESSION_PROBE_ROUTE = "/openevo-native/session"
_NATIVE_WORKSPACE_IMPORT_ROUTE = "/openevo-native/workspace-imports"
_NATIVE_WORKSPACE_CANCEL_ROUTE = "/openevo-native/workspace-imports/cancel"
_NATIVE_WORKSPACE_DISCARD_ROUTE = "/openevo-native/workspace-imports/discard"
_NATIVE_WORKSPACE_REQUEST_MAX_BYTES = 8192
_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{7,40}")
_PACKAGED_STARTUP_PHASES = frozenset(
    {
        "bundled_core_assets",
        "contract_app_v2",
        "core_adapter_v2",
        "core_bridge_v2",
        "core_bridge_store_v2",
        "core_runtime_v2",
        "event_broker_v2",
        "listener",
        "native_frame",
        "native_routes",
        "provider_store_v2",
        "restart_reconciliation_v2",
        "release_provider_v2",
        "remote_lifecycle_v2",
        "ssh_catalog_v2",
        "static_app",
        "server",
        "server_import",
        "shutdown",
        "workspace_store_v2",
    }
)
_PACKAGED_STARTUP_CODES = frozenset(
    {"execution_failed", *(f"{phase}_failed" for phase in _PACKAGED_STARTUP_PHASES)}
)


def resolve_desktop_config_root(
    desktop_config_root: Path | str | None = None,
    *,
    platform_name: str | None = None,
) -> Path:
    """Return the private Desktop config root without inspecting any state."""
    if desktop_config_root is not None:
        return Path(desktop_config_root).expanduser()

    resolved_platform = platform.system() if platform_name is None else platform_name
    if resolved_platform == "Darwin":
        return DARWIN_DESKTOP_CONFIG_ROOT.expanduser()
    if resolved_platform == "Linux":
        return LINUX_DESKTOP_CONFIG_ROOT.expanduser()
    raise ValueError(f"unsupported Desktop platform: {resolved_platform!r}")


def resolve_desktop_state_root(
    desktop_config_root: Path | str | None = None,
    *,
    platform_name: str | None = None,
) -> Path:
    """Return the current Desktop storage generation under its config root."""
    return (
        resolve_desktop_config_root(
            desktop_config_root,
            platform_name=platform_name,
        )
        / DESKTOP_STATE_DIRECTORY
    )


class PackagedLauncherStartupError(RuntimeError):
    """Carry one fixed, redacted packaged-launcher failure code."""

    def __init__(self, code: str) -> None:
        if code not in _PACKAGED_STARTUP_CODES:
            raise ValueError("invalid packaged startup diagnostic code")
        self.code = code
        super().__init__("OpenEvo Desktop packaged startup failed")


@dataclass(frozen=True)
class _NativeLauncherFrame:
    instance_id: str
    readiness_key: bytes = field(repr=False)
    session_token: str = field(repr=False)
    handoff_token: str = field(repr=False)


_NativeText = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=256,
        pattern=r"^[^\x00-\x20\x7f](?:[^\x00-\x1f\x7f]*[^\x00-\x20\x7f])?$",
    ),
]


class _NativeWorkspaceImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    schema_version: Literal["2"]
    kind: Literal["native_folder_snapshot"]
    action_id: Annotated[
        str,
        StringConstraints(
            strict=True,
            min_length=16,
            max_length=256,
            pattern=r"^[^\x00-\x20\x7f](?:[^\x00-\x1f\x7f]*[^\x00-\x20\x7f])?$",
        ),
    ]
    selected_path: Annotated[
        str,
        Field(repr=False),
        StringConstraints(strict=True, min_length=1, max_length=4096),
    ]
    selected_device: Annotated[int, Field(strict=True, ge=0, le=2**64 - 1)]
    selected_inode: Annotated[int, Field(strict=True, ge=1, le=2**64 - 1)]
    cancellation_token: Annotated[
        str,
        Field(repr=False),
        StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
    ]
    project_id: _NativeText | None = None


class _NativeWorkspaceDiscardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    schema_version: Literal["2"]
    action_id: Annotated[
        str,
        StringConstraints(
            strict=True,
            min_length=16,
            max_length=256,
            pattern=r"^[^\x00-\x20\x7f](?:[^\x00-\x1f\x7f]*[^\x00-\x20\x7f])?$",
        ),
    ]
    project_id: _NativeText | None = None


class _NativeWorkspaceCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    schema_version: Literal["2"]
    action_id: Annotated[
        str,
        StringConstraints(
            strict=True,
            min_length=16,
            max_length=256,
            pattern=r"^[^\x00-\x20\x7f](?:[^\x00-\x1f\x7f]*[^\x00-\x20\x7f])?$",
        ),
    ]
    cancellation_token: Annotated[
        str,
        Field(repr=False),
        StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
    ]


def _close_startup_app(app: FastAPI) -> None:
    provider = getattr(app.state, "desktop_release_provider", None)
    close = getattr(provider, "close", None)
    if not callable(close):
        return
    close()


def _request_startup_app_shutdown(app: FastAPI) -> None:
    provider = getattr(app.state, "desktop_release_provider", None)
    request_shutdown = getattr(provider, "request_shutdown", None)
    if callable(request_shutdown):
        request_shutdown()


def _close_listener(listener: socket.socket) -> None:
    listener.close()


def _close_server_resources(listener: socket.socket | None, app: FastAPI) -> None:
    failure: BaseException | None = None
    if listener is not None:
        try:
            _close_listener(listener)
        except BaseException as exc:
            failure = exc
    try:
        _close_startup_app(app)
    except BaseException as exc:
        if failure is None:
            failure = exc
    if failure is not None:
        raise failure


@contextmanager
def _defer_packaged_server_signal_replay(
    *,
    enabled: bool,
    request_exit: Callable[[], None],
) -> Iterator[None]:
    if not enabled or threading.current_thread() is not threading.main_thread():
        yield
        return

    original_handlers: list[tuple[int, signal.Handlers]] = []

    def defer_replayed_signal(_signum: int, _frame: FrameType | None) -> None:
        request_exit()

    try:
        for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
            original_handlers.append(
                (shutdown_signal, signal.signal(shutdown_signal, defer_replayed_signal))
            )
    except BaseException:
        for shutdown_signal, handler in reversed(original_handlers):
            signal.signal(shutdown_signal, handler)
        raise
    try:
        yield
    finally:
        for shutdown_signal, handler in reversed(original_handlers):
            signal.signal(shutdown_signal, handler)


def create_app(
    *,
    static_root: Path | str | None = None,
    desktop_config_root: Path | str | None = None,
    native_frame: _NativeLauncherFrame,
    source_commit: str,
    build_channel: Literal["release", "development", "test"],
    core_assets_root: Path | str | None = None,
    release_assets_root: Path | str | None = None,
    packaged_askpass_helper_path: Path | str | None = None,
    packaged_askpass_helper_sha256: str | None = None,
    packaged_askpass_helper_byte_size: int | None = None,
    openssh_home: Path | str | None = None,
    browser_endpoint: str | None = None,
    browser_bootstrap_token: str | None = None,
) -> FastAPI:
    _validate_source_commit(source_commit, build_channel=build_channel)
    owned_apps: list[FastAPI] = []
    try:
        return _create_app(
            static_root=static_root,
            desktop_config_root=desktop_config_root,
            native_frame=native_frame,
            source_commit=source_commit,
            build_channel=build_channel,
            core_assets_root=core_assets_root,
            release_assets_root=release_assets_root,
            packaged_askpass_helper_path=packaged_askpass_helper_path,
            packaged_askpass_helper_sha256=packaged_askpass_helper_sha256,
            packaged_askpass_helper_byte_size=packaged_askpass_helper_byte_size,
            openssh_home=openssh_home,
            browser_endpoint=browser_endpoint,
            browser_bootstrap_token=browser_bootstrap_token,
            owned_apps=owned_apps,
        )
    except PackagedLauncherStartupError:
        if owned_apps:
            try:
                _close_startup_app(owned_apps[0])
            except BaseException:
                pass
        raise
    except Exception as exc:
        if owned_apps:
            try:
                _close_startup_app(owned_apps[0])
            except BaseException:
                pass
        if build_channel == "release":
            raise PackagedLauncherStartupError("native_routes_failed") from exc
        raise


def _create_app(
    *,
    static_root: Path | str | None = None,
    desktop_config_root: Path | str | None = None,
    native_frame: _NativeLauncherFrame,
    source_commit: str,
    build_channel: Literal["release", "development", "test"],
    core_assets_root: Path | str | None = None,
    release_assets_root: Path | str | None = None,
    packaged_askpass_helper_path: Path | str | None = None,
    packaged_askpass_helper_sha256: str | None = None,
    packaged_askpass_helper_byte_size: int | None = None,
    openssh_home: Path | str | None = None,
    browser_endpoint: str | None = None,
    browser_bootstrap_token: str | None = None,
    owned_apps: list[FastAPI],
) -> FastAPI:
    startup_phase = "bundled_core_assets"

    def record_startup_phase(value: str) -> None:
        nonlocal startup_phase
        if value not in _PACKAGED_STARTUP_PHASES:
            raise ValueError("invalid packaged startup phase")
        startup_phase = value

    state_root = resolve_desktop_state_root(desktop_config_root)
    askpass_helper: AskpassHelperAuthority | None = None
    host_trust: SystemOpenSshHostTrust | None = None
    managed_ssh_home: ManagedOpenSshHome | None = None
    home = os.environ.get("HOME")
    try:
        if build_channel == "release" and release_assets_root is None and core_assets_root is None:
            raise ValueError("release assets root must be provided by the native host")
        if release_assets_root is not None and not Path(release_assets_root).is_absolute():
            raise ValueError("release assets root must be an absolute native-owned path")
        helper_identity = (
            packaged_askpass_helper_path,
            packaged_askpass_helper_sha256,
            packaged_askpass_helper_byte_size,
        )
        if any(value is not None for value in helper_identity) != all(
            value is not None for value in helper_identity
        ):
            raise ValueError("packaged askpass helper identity is incomplete")
        if packaged_askpass_helper_path is not None:
            startup_phase = "remote_lifecycle_v2"
            assert packaged_askpass_helper_sha256 is not None
            assert packaged_askpass_helper_byte_size is not None
            askpass_helper = AskpassHelperAuthority.open(
                packaged_askpass_helper_path,
                expected_sha256=packaged_askpass_helper_sha256,
                expected_byte_size=packaged_askpass_helper_byte_size,
            )
            if openssh_home is not None:
                managed_ssh_home = ManagedOpenSshHome(openssh_home)
                home = os.fspath(managed_ssh_home.root)
            if home is None:
                raise ValueError("system OpenSSH HOME is unavailable")
            host_trust = SystemOpenSshHostTrust(
                home=home,
                inherited_environment=os.environ,
            )
        if askpass_helper is None or host_trust is None:
            startup_phase = "remote_lifecycle_v2"
        app = create_packaged_release_desktop_local_api_v2_app(
            state_root=state_root,
            session_token=native_frame.session_token,
            instance_id=native_frame.instance_id,
            source_commit=source_commit,
            build_version="0.1.10",
            build_channel=build_channel,
            core_assets_root=core_assets_root,
            release_assets_root=release_assets_root,
            system_ssh_askpass_helper=askpass_helper,
            system_ssh_host_trust=host_trust,
            home=home,
            inherited_environment=os.environ,
            startup_phase=record_startup_phase if build_channel == "release" else None,
            close_on_shutdown=build_channel != "release",
        )
    except Exception as exc:
        if host_trust is not None:
            host_trust.close()
        if askpass_helper is not None:
            askpass_helper.close()
        if build_channel == "release":
            raise PackagedLauncherStartupError(f"{startup_phase}_failed") from exc
        raise
    owned_apps.append(app)
    expected_session_token = native_frame.session_token.encode("ascii")
    expected_handoff_token = native_frame.handoff_token.encode("ascii")

    @app.get(
        _NATIVE_HEALTH_ROUTE,
        include_in_schema=False,
    )
    def native_health(request: Request) -> Response:
        challenge = _native_challenge(request)
        if challenge is None:
            return Response(status_code=403)
        domain = (f"{NATIVE_SIDECAR_PROTOCOL}\0{native_frame.instance_id}\0{challenge}").encode(
            "ascii"
        )
        proof = hmac.new(
            native_frame.readiness_key,
            domain,
            hashlib.sha256,
        ).hexdigest()
        return JSONResponse(
            status_code=200,
            content={
                "service": "openevo-sidecar",
                "status": "ok",
                "protocol": NATIVE_SIDECAR_PROTOCOL,
                "instance_id": native_frame.instance_id,
                "instance_proof": proof,
            },
        )

    @app.get(
        _NATIVE_SESSION_PROBE_ROUTE,
        include_in_schema=False,
        status_code=204,
    )
    def native_session_probe(request: Request) -> Response:
        if not _native_credential_matches(
            request,
            header_name=_NATIVE_SESSION_HEADER_BYTES,
            expected=expected_session_token,
        ):
            return Response(status_code=403)
        return Response(status_code=204)

    @app.post(
        _NATIVE_WORKSPACE_IMPORT_ROUTE,
        include_in_schema=False,
        status_code=202,
    )
    async def native_workspace_import(request: Request) -> Response:
        if not _native_credential_matches(
            request,
            header_name=_NATIVE_HANDOFF_HEADER_BYTES,
            expected=expected_handoff_token,
        ):
            return _native_workspace_error(status_code=403)
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            return _native_workspace_error(status_code=415)
        try:
            encoded = await _read_bounded_native_body(
                request,
                max_bytes=_NATIVE_WORKSPACE_REQUEST_MAX_BYTES,
            )
            document = json.loads(
                encoded.decode("utf-8", errors="strict"),
                object_pairs_hook=_strict_json_object,
            )
            parsed = _NativeWorkspaceImportRequest.model_validate(document)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError):
            return _native_workspace_error(status_code=422)
        try:
            operation = await run_in_threadpool(
                partial(
                    app.state.desktop_release_provider.reserve_native_workspace_prepare,
                    action_id=parsed.action_id,
                    selected_path=parsed.selected_path,
                    selected_device=parsed.selected_device,
                    selected_inode=parsed.selected_inode,
                    cancellation_token=parsed.cancellation_token,
                    project_id=parsed.project_id,
                )
            )
        except (ValueError, RuntimeError, OSError):
            return _native_workspace_error(status_code=409)
        return JSONResponse(
            status_code=202,
            content=operation.model_dump(mode="json"),
        )

    @app.post(
        _NATIVE_WORKSPACE_CANCEL_ROUTE,
        include_in_schema=False,
        status_code=204,
    )
    async def native_workspace_cancel(request: Request) -> Response:
        if not _native_credential_matches(
            request,
            header_name=_NATIVE_HANDOFF_HEADER_BYTES,
            expected=expected_handoff_token,
        ):
            return _native_workspace_error(status_code=403)
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            return _native_workspace_error(status_code=415)
        try:
            encoded = await _read_bounded_native_body(
                request,
                max_bytes=_NATIVE_WORKSPACE_REQUEST_MAX_BYTES,
            )
            document = json.loads(
                encoded.decode("utf-8", errors="strict"),
                object_pairs_hook=_strict_json_object,
            )
            parsed = _NativeWorkspaceCancelRequest.model_validate(document)
            await run_in_threadpool(
                partial(
                    app.state.desktop_release_provider.cancel_native_workspace_prepare,
                    action_id=parsed.action_id,
                    cancellation_token=parsed.cancellation_token,
                )
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            ValidationError,
            RuntimeError,
        ):
            return _native_workspace_error(status_code=409)
        return Response(status_code=204)

    @app.post(
        _NATIVE_WORKSPACE_DISCARD_ROUTE,
        include_in_schema=False,
        status_code=204,
    )
    async def native_workspace_discard(request: Request) -> Response:
        if not _native_credential_matches(
            request,
            header_name=_NATIVE_HANDOFF_HEADER_BYTES,
            expected=expected_handoff_token,
        ):
            return _native_workspace_error(status_code=403)
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            return _native_workspace_error(status_code=415)
        try:
            encoded = await _read_bounded_native_body(
                request,
                max_bytes=_NATIVE_WORKSPACE_REQUEST_MAX_BYTES,
            )
            document = json.loads(
                encoded.decode("utf-8", errors="strict"),
                object_pairs_hook=_strict_json_object,
            )
            parsed = _NativeWorkspaceDiscardRequest.model_validate(document)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError):
            return _native_workspace_error(status_code=422)
        try:
            await run_in_threadpool(
                partial(
                    app.state.desktop_release_provider.discard_native_workspace_prepare,
                    action_id=parsed.action_id,
                    project_id=parsed.project_id,
                )
            )
        except (WorkspaceImportError, OSError):
            return _native_workspace_error(status_code=409)
        return Response(status_code=204)

    if (browser_endpoint is None) != (browser_bootstrap_token is None):
        raise ValueError("browser endpoint and bootstrap token must be provided together")
    if browser_endpoint is not None and browser_bootstrap_token is not None:
        if managed_ssh_home is None:
            raise ValueError("browser hosting requires a managed OpenSSH home")
        version = app.state.desktop_release_provider._version({})
        version_document = version.model_dump(mode="json")
        negotiated_contract = {
            "schema_version": "2",
            "major": version_document["preferred_major"],
            "mutation_major": version_document["mutation_major"],
            "openapi_sha256": version_document["openapi_sha256"],
            "event_schema_sha256": version_document["event_schema_sha256"],
            "release_version": version_document["release_version"],
            "build_id": version_document["build_id"],
            "source_commit": version_document["source_commit"],
            "build_channel": version_document["build_channel"],
            "provider_kind": version_document["provider_kind"],
            "feature_flags": version_document["feature_flags"],
            "feature_set_sha256": version_document["feature_set_sha256"],
            "required_core_api_major": version_document["required_core_api_major"],
            "mutation_compatible": version_document["mutation_compatible"],
        }
        install_browser_host_routes(
            app,
            endpoint=browser_endpoint,
            bootstrap_token=browser_bootstrap_token,
            session_token=native_frame.session_token,
            negotiated_contract=negotiated_contract,
            managed_ssh_home=managed_ssh_home,
        )

    try:
        if build_channel == "release":
            record_startup_phase("static_app")
        return create_desktop_app(app, static_root=static_root)
    except Exception as exc:
        if build_channel == "release":
            raise PackagedLauncherStartupError(f"{startup_phase}_failed") from exc
        raise


def _native_credential_matches(
    request: Request,
    *,
    header_name: bytes,
    expected: bytes,
) -> bool:
    candidates = [value for name, value in request.scope["headers"] if name == header_name]
    candidate = candidates[0] if len(candidates) == 1 else b""
    matches = secrets.compare_digest(candidate, expected)
    return len(candidates) == 1 and matches


def _native_challenge(request: Request) -> str | None:
    candidates = [
        value for name, value in request.scope["headers"] if name == _NATIVE_CHALLENGE_HEADER_BYTES
    ]
    if len(candidates) != 1:
        return None
    try:
        challenge = candidates[0].decode("ascii", errors="strict")
    except UnicodeDecodeError:
        return None
    if re.fullmatch(r"[0-9a-f]{64}", challenge) is None:
        return None
    return challenge


async def _read_bounded_native_body(request: Request, *, max_bytes: int) -> bytes:
    payload = bytearray()
    async for chunk in request.stream():
        if len(chunk) > max_bytes - len(payload):
            raise ValueError("native request exceeds its byte limit")
        payload.extend(chunk)
    if not payload:
        raise ValueError("native workspace request is empty")
    return bytes(payload)


def _native_workspace_error(*, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": "workspace_import_failed",
            "message": "OpenEvo Desktop could not prepare the selected research folder.",
        },
    )


def _read_native_instance_frame() -> _NativeLauncherFrame:
    encoded = sys.stdin.buffer.readline(NATIVE_INSTANCE_FRAME_MAX_BYTES + 1)
    if not encoded.endswith(b"\n") or len(encoded) > NATIVE_INSTANCE_FRAME_MAX_BYTES:
        raise ValueError("invalid native instance frame")
    if sys.stdin.buffer.read(1) != b"":
        raise ValueError("invalid native instance frame")
    try:
        text = encoded[:-1].decode("utf-8", errors="strict")
        payload = json.loads(text, object_pairs_hook=_strict_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid native instance frame") from exc
    if type(payload) is not dict or set(payload) != {
        "protocol",
        "instance_id",
        "readiness_key",
        "session_token",
        "handoff_token",
    }:
        raise ValueError("invalid native instance frame")
    protocol = payload["protocol"]
    instance_id = payload["instance_id"]
    readiness_key = payload["readiness_key"]
    session_token = payload["session_token"]
    handoff_token = payload["handoff_token"]
    if (
        type(protocol) is not str
        or protocol != NATIVE_SIDECAR_PROTOCOL
        or type(instance_id) is not str
        or re.fullmatch(r"[0-9a-f]{32}", instance_id) is None
        or type(readiness_key) is not str
        or re.fullmatch(r"[0-9a-f]{64}", readiness_key) is None
        or type(session_token) is not str
        or re.fullmatch(r"[0-9a-f]{64}", session_token) is None
        or type(handoff_token) is not str
        or re.fullmatch(r"[0-9a-f]{64}", handoff_token) is None
    ):
        raise ValueError("invalid native instance frame")
    return _NativeLauncherFrame(
        instance_id=instance_id,
        readiness_key=bytes.fromhex(readiness_key),
        session_token=session_token,
        handoff_token=handoff_token,
    )


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate native instance frame key")
        value[key] = item
    return value


def _validate_source_commit(
    source_commit: str,
    *,
    build_channel: Literal["release", "development", "test"],
) -> None:
    if type(source_commit) is not str or _SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise ValueError("source commit must be 7-40 lowercase hexadecimal characters")
    if build_channel == "release" and set(source_commit) == {"0"}:
        raise ValueError("release source commit must not be an all-zero placeholder")


def main(
    argv: list[str] | None = None,
    *,
    packaged_source_commit: str | None = None,
    packaged_askpass_helper_path: Path | str | None = None,
    packaged_askpass_helper_sha256: str | None = None,
    packaged_askpass_helper_byte_size: int | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run the OpenEvo Desktop sidecar.")
    parser.add_argument("--static-root", type=Path, default=None)
    parser.add_argument(
        "--desktop-config-root",
        type=Path,
        default=None,
    )
    parser.add_argument("--listener-fd", type=int, required=True)
    parser.add_argument("--native-instance-stdin", action="store_true", required=True)
    parser.add_argument("--core-assets-root", type=Path, default=None)
    parser.add_argument("--release-assets-root", type=Path, default=None)
    parser.add_argument(
        "--ssh-askpass-helper-path",
        type=Path,
        required=(
            packaged_source_commit is not None and packaged_askpass_helper_sha256 is not None
        ),
    )
    parser.add_argument(
        "--ssh-askpass-helper-sha256",
        default=packaged_askpass_helper_sha256,
    )
    parser.add_argument(
        "--ssh-askpass-helper-byte-size",
        type=int,
        default=packaged_askpass_helper_byte_size,
    )
    if packaged_source_commit is None:
        parser.add_argument("--source-commit", required=True)
        parser.add_argument(
            "--build-channel",
            choices=("development", "test"),
            required=True,
        )
    args = parser.parse_args(argv)

    if args.listener_fd < 3:
        parser.error("--listener-fd must identify an inherited non-standard descriptor")
    try:
        native_frame = _read_native_instance_frame()
    except Exception as exc:
        if packaged_source_commit is not None:
            raise PackagedLauncherStartupError("native_frame_failed") from exc
        raise
    if packaged_source_commit is None:
        source_commit = args.source_commit
        build_channel = args.build_channel
    else:
        source_commit = packaged_source_commit
        build_channel = "release"

    try:
        import uvicorn
    except Exception as exc:
        if packaged_source_commit is not None:
            raise PackagedLauncherStartupError("server_import_failed") from exc
        raise

    app = create_app(
        static_root=args.static_root,
        desktop_config_root=args.desktop_config_root,
        native_frame=native_frame,
        source_commit=source_commit,
        build_channel=build_channel,
        core_assets_root=args.core_assets_root,
        release_assets_root=args.release_assets_root,
        packaged_askpass_helper_path=(
            args.ssh_askpass_helper_path
            if args.ssh_askpass_helper_path is not None
            else packaged_askpass_helper_path
        ),
        packaged_askpass_helper_sha256=(args.ssh_askpass_helper_sha256),
        packaged_askpass_helper_byte_size=(args.ssh_askpass_helper_byte_size),
    )
    listener: socket.socket | None = None
    try:
        listener = socket.socket(fileno=args.listener_fd)
        listener.set_inheritable(False)
        listener.setblocking(False)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
        )
    except Exception as exc:
        try:
            _close_server_resources(listener, app)
        except BaseException:
            pass
        if packaged_source_commit is not None:
            raise PackagedLauncherStartupError("listener_failed") from exc
        raise
    try:
        server = uvicorn.Server(config)

        def request_exit() -> None:
            try:
                _request_startup_app_shutdown(app)
            finally:
                server.should_exit = True

        with _defer_packaged_server_signal_replay(
            enabled=packaged_source_commit is not None,
            request_exit=request_exit,
        ):
            try:
                server.run(sockets=[listener])
                if not server.started:
                    raise RuntimeError("Desktop sidecar server did not reach startup")
            except BaseException as exc:
                try:
                    _close_server_resources(listener, app)
                except BaseException:
                    pass
                if packaged_source_commit is not None and isinstance(exc, Exception):
                    raise PackagedLauncherStartupError("server_failed") from exc
                raise
            try:
                _close_server_resources(listener, app)
            except Exception as exc:
                if packaged_source_commit is not None:
                    raise PackagedLauncherStartupError("shutdown_failed") from exc
                raise
    except PackagedLauncherStartupError:
        raise
    except BaseException as exc:
        try:
            _close_server_resources(listener, app)
        except BaseException:
            pass
        if packaged_source_commit is not None and isinstance(exc, Exception):
            raise PackagedLauncherStartupError("server_failed") from exc
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
