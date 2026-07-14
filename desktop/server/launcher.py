from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import secrets
import socket
import sys
from typing import Literal

from fastapi import FastAPI, Request, Response

from desktop.server.app import create_desktop_app
from desktop.sidecar.release_app import create_release_desktop_local_api_app
from desktop.sidecar.release_provider import NATIVE_SIDECAR_PROTOCOL


DEFAULT_DESKTOP_CONFIG_ROOT = Path("~/.openevo/desktop")
LOCAL_API_STATE_DIRECTORY = "local-api-v1"
NATIVE_INSTANCE_FRAME_MAX_BYTES = 512
NATIVE_SESSION_HEADER = "X-OpenEvo-Desktop-Session"
_NATIVE_SESSION_HEADER_BYTES = NATIVE_SESSION_HEADER.lower().encode("ascii")
_NATIVE_SESSION_PROBE_ROUTE = "/openevo-native/session"
_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{7,40}")


@dataclass(frozen=True)
class _NativeLauncherFrame:
    instance_id: str
    readiness_key: bytes = field(repr=False)
    session_token: str = field(repr=False)


def create_app(
    *,
    static_root: Path | str | None = None,
    desktop_config_root: Path | str | None = None,
    native_frame: _NativeLauncherFrame,
    source_commit: str,
    build_channel: Literal["release", "development", "test"],
) -> FastAPI:
    _validate_source_commit(source_commit, build_channel=build_channel)
    config_root = (
        Path(desktop_config_root).expanduser()
        if desktop_config_root is not None
        else DEFAULT_DESKTOP_CONFIG_ROOT.expanduser()
    )
    app = create_release_desktop_local_api_app(
        state_root=config_root / LOCAL_API_STATE_DIRECTORY,
        session_token=native_frame.session_token,
        instance_id=native_frame.instance_id,
        readiness_key=native_frame.readiness_key,
        source_commit=source_commit,
        build_channel=build_channel,
    )
    expected_session_token = native_frame.session_token.encode("ascii")

    @app.get(
        _NATIVE_SESSION_PROBE_ROUTE,
        include_in_schema=False,
        status_code=204,
    )
    def native_session_probe(request: Request) -> Response:
        candidates = [
            value
            for name, value in request.scope["headers"]
            if name == _NATIVE_SESSION_HEADER_BYTES
        ]
        if len(candidates) != 1 or not secrets.compare_digest(
            candidates[0], expected_session_token
        ):
            return Response(status_code=403)
        return Response(status_code=204)

    return create_desktop_app(app, static_root=static_root)


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
    }:
        raise ValueError("invalid native instance frame")
    protocol = payload["protocol"]
    instance_id = payload["instance_id"]
    readiness_key = payload["readiness_key"]
    session_token = payload["session_token"]
    if (
        type(protocol) is not str
        or protocol != NATIVE_SIDECAR_PROTOCOL
        or type(instance_id) is not str
        or re.fullmatch(r"[0-9a-f]{32}", instance_id) is None
        or type(readiness_key) is not str
        or re.fullmatch(r"[0-9a-f]{64}", readiness_key) is None
        or type(session_token) is not str
        or re.fullmatch(r"[0-9a-f]{64}", session_token) is None
    ):
        raise ValueError("invalid native instance frame")
    return _NativeLauncherFrame(
        instance_id=instance_id,
        readiness_key=bytes.fromhex(readiness_key),
        session_token=session_token,
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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
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
        host=args.host,
        port=args.port,
    )
    uvicorn.Server(config).run(sockets=[listener])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
