from __future__ import annotations

import argparse
import base64
from collections import deque
from typing import Annotated, Callable
from dataclasses import dataclass, field
import json
from hashlib import sha256
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool

from desktop.server.app import create_desktop_app
from desktop.sidecar.contracts.v1 import ProjectSourceV1, WorkspaceImportRefV1
from desktop.sidecar.native_workspace import (
    NativeWorkspaceArchiveCancelled,
    NativeWorkspaceArchiveError,
    prepare_native_workspace,
)
from desktop.sidecar.native_credentials import NativeCredentialError
from desktop.sidecar.provider_store import ProviderStoreError, ResourceNotFoundError
from desktop.sidecar.release_app import create_release_desktop_local_api_app
from desktop.sidecar.release_provider import NATIVE_SIDECAR_PROTOCOL
from desktop.sidecar.release_runtime import bundled_core_asset_root
from desktop.sidecar.workspace_identity import (
    native_import_id_for_action,
    ownership_for_native_import,
)
from desktop.sidecar.workspace_imports import (
    WorkspaceImportCancelled,
    WorkspaceImportError,
    WorkspaceImportStore,
)


DEFAULT_DESKTOP_CONFIG_ROOT = Path("~/.openevo/desktop")
LOCAL_API_STATE_DIRECTORY = "local-api-v1"
NATIVE_INSTANCE_FRAME_MAX_BYTES = 512
NATIVE_SESSION_HEADER = "X-OpenEvo-Desktop-Session"
_NATIVE_SESSION_HEADER_BYTES = NATIVE_SESSION_HEADER.lower().encode("ascii")
NATIVE_HANDOFF_HEADER = "X-OpenEvo-Native-Handoff"
_NATIVE_HANDOFF_HEADER_BYTES = NATIVE_HANDOFF_HEADER.lower().encode("ascii")
_NATIVE_SESSION_PROBE_ROUTE = "/openevo-native/session"
_NATIVE_WORKSPACE_IMPORT_ROUTE = "/openevo-native/workspace-imports"
_NATIVE_WORKSPACE_CANCEL_ROUTE = "/openevo-native/workspace-imports/cancel"
_NATIVE_WORKSPACE_DISCARD_ROUTE = "/openevo-native/workspace-imports/discard"
_NATIVE_CREDENTIAL_ROUTE = "/openevo-native/credentials"
_NATIVE_WORKSPACE_REQUEST_MAX_BYTES = 8192
_NATIVE_CREDENTIAL_REQUEST_MAX_BYTES = 1_450_000
_MAX_NATIVE_WORKSPACE_OPERATIONS = 64
_MAX_NATIVE_CREDENTIAL_DELIVERIES = 512
_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{7,40}")


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
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"]
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
    selected_path: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4096)]
    selected_device: Annotated[int, Field(strict=True, ge=0, le=2**64 - 1)]
    selected_inode: Annotated[int, Field(strict=True, ge=1, le=2**64 - 1)]
    cancellation_token: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
    ]
    project_id: _NativeText | None = None


class _NativeWorkspaceImportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    source: ProjectSourceV1
    lease_token: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
    ]


class _NativeWorkspaceDiscardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"]
    action_id: Annotated[
        str,
        StringConstraints(
            strict=True,
            min_length=16,
            max_length=256,
            pattern=r"^[^\x00-\x20\x7f](?:[^\x00-\x1f\x7f]*[^\x00-\x20\x7f])?$",
        ),
    ]
    import_ref: WorkspaceImportRefV1
    lease_token: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
    ]
    project_id: _NativeText | None = None


class _NativeWorkspaceCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"]
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
        StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
    ]


class _NativeCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"]
    delivery_id: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$"),
    ]
    profile_id: _NativeText
    expected_etag: Annotated[
        str,
        StringConstraints(strict=True, pattern=r'^"[0-9a-f]{64}"$'),
    ] | None = None
    operation: Literal["replace", "delete"]
    authentication_kind: Literal[
        "native_private_key",
        "native_password",
    ]
    slot_kind: Literal[
        "ssh_password",
        "ssh_private_key",
        "ssh_private_key_passphrase",
    ] | None = None
    password_b64: Annotated[str, StringConstraints(strict=True, max_length=21_848)] | None = None
    private_key_b64: Annotated[
        str,
        StringConstraints(strict=True, max_length=1_398_104),
    ] | None = None
    passphrase_b64: Annotated[
        str,
        StringConstraints(strict=True, max_length=21_848),
    ] | None = None


@dataclass(frozen=True)
class _NativeWorkspaceOperation:
    action_id: str
    cancellation_token: str = field(repr=False)
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)


class _NativeWorkspaceOperations:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, _NativeWorkspaceOperation] = {}
        self._cancelled_before_begin: dict[str, str] = {}

    def begin(self, action_id: str, cancellation_token: str) -> _NativeWorkspaceOperation:
        operation = _NativeWorkspaceOperation(
            action_id=action_id,
            cancellation_token=cancellation_token,
        )
        with self._lock:
            if action_id in self._active:
                raise ValueError("native workspace action is already active")
            if len(self._active) >= _MAX_NATIVE_WORKSPACE_OPERATIONS:
                raise ValueError("native workspace operation capacity exceeded")
            cancelled_token = self._cancelled_before_begin.pop(action_id, None)
            if cancelled_token is not None and secrets.compare_digest(
                cancelled_token,
                cancellation_token,
            ):
                operation.cancelled.set()
            self._active[action_id] = operation
        return operation

    def cancel(self, action_id: str, cancellation_token: str) -> None:
        with self._lock:
            operation = self._active.get(action_id)
            if operation is None:
                if len(self._cancelled_before_begin) >= _MAX_NATIVE_WORKSPACE_OPERATIONS:
                    oldest = next(iter(self._cancelled_before_begin))
                    del self._cancelled_before_begin[oldest]
                self._cancelled_before_begin[action_id] = cancellation_token
                return
            if not secrets.compare_digest(
                operation.cancellation_token,
                cancellation_token,
            ):
                raise ValueError("native workspace cancellation identity conflicts")
            operation.cancelled.set()

    def finish(self, operation: _NativeWorkspaceOperation) -> None:
        with self._lock:
            if self._active.get(operation.action_id) is operation:
                del self._active[operation.action_id]


class _NativeCredentialDeliveries:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._responses: dict[str, tuple[str, dict[str, object]]] = {}
        self._order: deque[str] = deque()

    def replay(self, delivery_id: str, digest: str) -> dict[str, object] | None:
        with self._lock:
            existing = self._responses.get(delivery_id)
            if existing is None:
                return None
            if not secrets.compare_digest(existing[0], digest):
                raise ValueError("native credential delivery identity conflicts")
            return existing[1]

    def remember(self, delivery_id: str, digest: str, response: dict[str, object]) -> None:
        with self._lock:
            existing = self._responses.get(delivery_id)
            if existing is not None:
                if not secrets.compare_digest(existing[0], digest):
                    raise ValueError("native credential delivery identity conflicts")
                return
            while len(self._responses) >= _MAX_NATIVE_CREDENTIAL_DELIVERIES:
                oldest = self._order.popleft()
                self._responses.pop(oldest, None)
            self._responses[delivery_id] = (digest, response)
            self._order.append(delivery_id)


def create_app(
    *,
    static_root: Path | str | None = None,
    desktop_config_root: Path | str | None = None,
    native_frame: _NativeLauncherFrame,
    source_commit: str,
    build_channel: Literal["release", "development", "test"],
    core_assets_root: Path | str | None = None,
) -> FastAPI:
    _validate_source_commit(source_commit, build_channel=build_channel)
    config_root = (
        Path(desktop_config_root).expanduser()
        if desktop_config_root is not None
        else DEFAULT_DESKTOP_CONFIG_ROOT.expanduser()
    )
    if build_channel == "release" and core_assets_root is None:
        core_assets_root = bundled_core_asset_root()
    app = create_release_desktop_local_api_app(
        state_root=config_root / LOCAL_API_STATE_DIRECTORY,
        session_token=native_frame.session_token,
        instance_id=native_frame.instance_id,
        readiness_key=native_frame.readiness_key,
        source_commit=source_commit,
        build_channel=build_channel,
        core_assets_root=core_assets_root,
    )
    expected_session_token = native_frame.session_token.encode("ascii")
    expected_handoff_token = native_frame.handoff_token.encode("ascii")
    workspace_import_store = app.state.desktop_release_provider.workspace_import_store
    workspace_operations = _NativeWorkspaceOperations()
    credential_deliveries = _NativeCredentialDeliveries()
    native_credentials = app.state.native_credentials

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
        status_code=201,
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
            operation = workspace_operations.begin(
                parsed.action_id,
                parsed.cancellation_token,
            )
        except ValueError:
            return _native_workspace_error(status_code=409)
        try:
            try:
                pending = await run_in_threadpool(
                    _ingest_native_workspace,
                    workspace_import_store,
                    parsed,
                    config_root / LOCAL_API_STATE_DIRECTORY,
                    operation.cancelled.is_set,
                )
            except (NativeWorkspaceArchiveCancelled, WorkspaceImportCancelled):
                return _native_workspace_cancelled()
            except (NativeWorkspaceArchiveError, WorkspaceImportError, OSError):
                return _native_workspace_error(status_code=409)
            if operation.cancelled.is_set():
                try:
                    await run_in_threadpool(
                        app.state.desktop_release_provider.discard_pending_workspace_import,
                        pending.source.import_ref,
                        project_id=parsed.project_id,
                        lease_token=pending.lease_token,
                    )
                except (WorkspaceImportError, OSError):
                    return _native_workspace_error(status_code=409)
                return _native_workspace_cancelled()
            return JSONResponse(status_code=201, content=pending.model_dump(mode="json"))
        finally:
            workspace_operations.finish(operation)

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
            workspace_operations.cancel(
                parsed.action_id,
                parsed.cancellation_token,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError):
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
                app.state.desktop_release_provider.discard_pending_workspace_import,
                parsed.import_ref,
                project_id=parsed.project_id,
                lease_token=parsed.lease_token,
            )
        except (WorkspaceImportError, OSError):
            return _native_workspace_error(status_code=409)
        return Response(status_code=204)

    @app.post(
        _NATIVE_CREDENTIAL_ROUTE,
        include_in_schema=False,
        status_code=200,
    )
    async def native_credential_delivery(request: Request) -> Response:
        if not _native_credential_matches(
            request,
            header_name=_NATIVE_HANDOFF_HEADER_BYTES,
            expected=expected_handoff_token,
        ):
            return _native_credential_error(status_code=403)
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            return _native_credential_error(status_code=415)
        password: bytearray | None = None
        private_key: bytearray | None = None
        passphrase: bytearray | None = None
        try:
            encoded = await _read_bounded_native_body(
                request,
                max_bytes=_NATIVE_CREDENTIAL_REQUEST_MAX_BYTES,
            )
            document = json.loads(
                encoded.decode("utf-8", errors="strict"),
                object_pairs_hook=_strict_json_object,
            )
            parsed = _NativeCredentialRequest.model_validate(document)
            delivery_digest = sha256(encoded).hexdigest()
            replayed = credential_deliveries.replay(parsed.delivery_id, delivery_digest)
            if replayed is not None:
                return JSONResponse(status_code=200, content=replayed)
            try:
                profile = app.state.desktop_release_provider.native_credential_profile(
                    parsed.profile_id
                )
            except ResourceNotFoundError:
                return _native_credential_error(status_code=404)
            if profile.authentication_kind != parsed.authentication_kind:
                return _native_credential_error(status_code=410)
            if parsed.expected_etag is not None and profile.etag != parsed.expected_etag:
                return _native_credential_error(status_code=409)
            if parsed.operation == "replace":
                if parsed.slot_kind is not None:
                    raise ValueError("native credential replacement must be profile-complete")
                password = _decode_native_secret(parsed.password_b64)
                private_key = _decode_native_secret(parsed.private_key_b64)
                passphrase = _decode_native_secret(parsed.passphrase_b64)
                statuses = native_credentials.replace(
                    parsed.profile_id,
                    authentication_kind=parsed.authentication_kind,
                    password=password,
                    private_key=private_key,
                    passphrase=passphrase,
                )
                password = private_key = passphrase = None
            else:
                if parsed.slot_kind is None or any(
                    value is not None
                    for value in (
                        parsed.password_b64,
                        parsed.private_key_b64,
                        parsed.passphrase_b64,
                    )
                ):
                    raise ValueError("native credential deletion must identify one slot")
                statuses = native_credentials.delete_slot(
                    parsed.profile_id,
                    parsed.slot_kind,
                )
                if not statuses:
                    statuses = native_credentials.statuses_for(
                        parsed.profile_id,
                        parsed.authentication_kind,
                    )
            updated = app.state.desktop_release_provider.set_native_credential_slots(
                parsed.profile_id,
                statuses,
                expected_etag=parsed.expected_etag,
            )
            response = updated.model_dump(mode="json")
            credential_deliveries.remember(
                parsed.delivery_id,
                delivery_digest,
                response,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            ValidationError,
            NativeCredentialError,
            ProviderStoreError,
            OSError,
        ):
            _zero_native_secret(password)
            _zero_native_secret(private_key)
            _zero_native_secret(passphrase)
            return _native_credential_error(status_code=409)
        return JSONResponse(status_code=200, content=response)

    return create_desktop_app(app, static_root=static_root)


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


def _native_workspace_cancelled() -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "code": "workspace_import_cancelled",
            "message": "OpenEvo Desktop cancelled the selected research folder import.",
        },
    )


def _native_credential_error(*, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": "native_credential_failed",
            "message": "OpenEvo Desktop could not update the selected SSH credential.",
        },
    )


def _decode_native_secret(value: str | None) -> bytearray | None:
    if value is None:
        return None
    try:
        return bytearray(base64.b64decode(value, validate=True))
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("native credential encoding is invalid") from exc


def _zero_native_secret(value: bytearray | None) -> None:
    if value is not None:
        value[:] = b"\x00" * len(value)


def _ingest_native_workspace(
    store: WorkspaceImportStore,
    request: _NativeWorkspaceImportRequest,
    temporary_root: Path,
    cancel_check: Callable[[], bool],
) -> _NativeWorkspaceImportResponse:
    import_id = native_import_id_for_action(request.action_id)
    with prepare_native_workspace(
        request.selected_path,
        import_id=import_id,
        temporary_root=temporary_root,
        expected_device=request.selected_device,
        expected_inode=request.selected_inode,
        cancel_check=cancel_check,
    ) as prepared:
        ownership = ownership_for_native_import(
            prepared.import_ref,
            project_id=request.project_id,
        )
        pending = store.ingest_pending(
            prepared.stream,
            ownership=ownership,
            import_id=import_id,
            cancel_check=cancel_check,
        )
        return _NativeWorkspaceImportResponse(
            source=ProjectSourceV1(
                kind="native_folder_snapshot",
                display_name=prepared.display_name,
                import_ref=pending.import_ref,
            ),
            lease_token=pending.lease_token,
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
) -> int:
    parser = argparse.ArgumentParser(description="Run the OpenEvo Desktop sidecar.")
    parser.add_argument("--static-root", type=Path, default=None)
    parser.add_argument(
        "--desktop-config-root",
        type=Path,
        default=DEFAULT_DESKTOP_CONFIG_ROOT,
    )
    parser.add_argument("--listener-fd", type=int, required=True)
    parser.add_argument("--native-instance-stdin", action="store_true", required=True)
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
    native_frame = _read_native_instance_frame()
    if packaged_source_commit is None:
        source_commit = args.source_commit
        build_channel = args.build_channel
    else:
        source_commit = packaged_source_commit
        build_channel = "release"

    import uvicorn

    app = create_app(
        static_root=args.static_root,
        desktop_config_root=args.desktop_config_root,
        native_frame=native_frame,
        source_commit=source_commit,
        build_channel=build_channel,
    )
    listener = socket.socket(fileno=args.listener_fd)
    listener.set_inheritable(False)
    listener.setblocking(False)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
    )
    uvicorn.Server(config).run(sockets=[listener])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
