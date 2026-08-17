from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
from typing import Annotated, Literal, Mapping

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError


BROWSER_BOOTSTRAP_ROUTE = "/openevo-native/browser/bootstrap"
BROWSER_SSH_HOST_ROUTE = "/openevo-native/browser/ssh-hosts"
DESKTOP_SESSION_HEADER = "X-OpenEvo-Desktop-Session"
_DESKTOP_SESSION_HEADER_BYTES = DESKTOP_SESSION_HEADER.lower().encode("ascii")
_MAX_REQUEST_BYTES = 8_192
_HOST_PATTERN = re.compile(r"^(?!-)[A-Za-z0-9._:-]{1,253}$")
_USER_PATTERN = re.compile(r"^(?!-)[A-Za-z0-9._-]{1,64}$")


class BrowserBootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    schema_version: Literal["2"]
    bootstrap_token: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


class BrowserSshHostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    schema_version: Literal["2"]
    host: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=253)]
    port: Annotated[int, Field(strict=True, ge=1, le=65535)]
    username: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]


class ManagedOpenSshHome:
    """Own a minimal, private OpenSSH home for browser-entered hosts.

    Credentials never enter this store. Authentication remains owned by the
    process' ssh-agent/Keychain or the sealed native askpass helper.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.ssh_dir = self.root / ".ssh"
        self.config_path = self.ssh_dir / "config"
        self._lock = threading.Lock()
        self.ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.ssh_dir, 0o700)
        if not self.config_path.exists():
            self._write_config(())

    def register(self, *, host: str, port: int, username: str) -> str:
        host = host.strip()
        username = username.strip()
        if _HOST_PATTERN.fullmatch(host) is None or _USER_PATTERN.fullmatch(username) is None:
            raise ValueError("SSH host or username is invalid")
        canonical = f"{username}\0{host.lower()}\0{port}".encode("utf-8")
        alias = f"openevo-{hashlib.sha256(canonical).hexdigest()[:20]}"
        with self._lock:
            entries = self._load_entries()
            entries[alias] = {"host": host, "port": port, "username": username}
            self._write_config(tuple(sorted(entries.items())))
        return alias

    def _load_entries(self) -> dict[str, dict[str, object]]:
        state_path = self.ssh_dir / "openevo-hosts.json"
        if not state_path.exists():
            return {}
        raw = state_path.read_bytes()
        if len(raw) > 256_000:
            raise ValueError("managed SSH host catalog is too large")
        value = json.loads(raw.decode("utf-8", errors="strict"))
        if type(value) is not dict:
            raise ValueError("managed SSH host catalog is invalid")
        entries: dict[str, dict[str, object]] = {}
        for alias, entry in value.items():
            if (
                type(alias) is not str
                or re.fullmatch(r"openevo-[0-9a-f]{20}", alias) is None
                or type(entry) is not dict
                or set(entry) != {"host", "port", "username"}
                or type(entry["host"]) is not str
                or _HOST_PATTERN.fullmatch(entry["host"]) is None
                or type(entry["port"]) is not int
                or not 1 <= entry["port"] <= 65535
                or type(entry["username"]) is not str
                or _USER_PATTERN.fullmatch(entry["username"]) is None
            ):
                raise ValueError("managed SSH host catalog is invalid")
            entries[alias] = dict(entry)
        return entries

    def _write_config(self, entries: tuple[tuple[str, Mapping[str, object]], ...]) -> None:
        state = {
            alias: {
                "host": entry["host"],
                "port": entry["port"],
                "username": entry["username"],
            }
            for alias, entry in entries
        }
        config = "".join(
            f"Host {alias}\n"
            f"    HostName {entry['host']}\n"
            f"    User {entry['username']}\n"
            f"    Port {entry['port']}\n"
            "    IdentitiesOnly no\n\n"
            for alias, entry in entries
        )
        self._atomic_write(self.ssh_dir / "openevo-hosts.json", (
            json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8"))
        self._atomic_write(self.config_path, config.encode("utf-8"))

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.ssh_dir)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class BrowserBootstrapAuthority:
    def __init__(self, *, bootstrap_token: str, context: Mapping[str, object]) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", bootstrap_token) is None:
            raise ValueError("browser bootstrap token is invalid")
        self._bootstrap_token = bootstrap_token.encode("ascii")
        self._context = dict(context)
        self._lock = threading.Lock()
        self._consumed = False

    def consume(self, candidate: str) -> dict[str, object] | None:
        encoded = candidate.encode("ascii")
        with self._lock:
            matches = secrets.compare_digest(encoded, self._bootstrap_token)
            if self._consumed or not matches:
                return None
            self._consumed = True
            return dict(self._context)


def install_browser_host_routes(
    app: FastAPI,
    *,
    endpoint: str,
    bootstrap_token: str,
    session_token: str,
    negotiated_contract: Mapping[str, object],
    managed_ssh_home: ManagedOpenSshHome,
) -> None:
    authority = BrowserBootstrapAuthority(
        bootstrap_token=bootstrap_token,
        context={
            "schema_version": "2",
            "endpoint": endpoint,
            "session_token": session_token,
            "negotiated_contract": dict(negotiated_contract),
        },
    )
    expected_session = session_token.encode("ascii")

    @app.post(BROWSER_BOOTSTRAP_ROUTE, include_in_schema=False)
    async def browser_bootstrap(request: Request) -> Response:
        if not _same_loopback_origin(request, endpoint):
            return Response(status_code=403)
        try:
            parsed = BrowserBootstrapRequest.model_validate(await _read_json(request))
        except (ValueError, ValidationError, UnicodeDecodeError, json.JSONDecodeError):
            return Response(status_code=422)
        context = authority.consume(parsed.bootstrap_token)
        if context is None:
            return Response(status_code=403)
        return JSONResponse(context, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.post(BROWSER_SSH_HOST_ROUTE, include_in_schema=False)
    async def browser_register_ssh_host(request: Request) -> Response:
        if not _same_loopback_origin(request, endpoint) or not _session_matches(
            request, expected_session
        ):
            return Response(status_code=403)
        try:
            parsed = BrowserSshHostRequest.model_validate(await _read_json(request))
            alias = managed_ssh_home.register(
                host=parsed.host,
                port=parsed.port,
                username=parsed.username,
            )
        except (ValueError, ValidationError, UnicodeDecodeError, json.JSONDecodeError, OSError):
            return JSONResponse(
                status_code=422,
                content={"code": "ssh_host_invalid", "message": "The SSH server details are invalid."},
            )
        return JSONResponse(
            {"schema_version": "2", "ssh_host_alias": alias},
            headers={"Cache-Control": "no-store"},
        )


async def _read_json(request: Request) -> object:
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
        raise ValueError("JSON content type required")
    payload = bytearray()
    async for chunk in request.stream():
        if len(chunk) > _MAX_REQUEST_BYTES - len(payload):
            raise ValueError("request too large")
        payload.extend(chunk)
    return json.loads(payload.decode("utf-8", errors="strict"))


def _session_matches(request: Request, expected: bytes) -> bool:
    values = [value for name, value in request.scope["headers"] if name == _DESKTOP_SESSION_HEADER_BYTES]
    candidate = values[0] if len(values) == 1 else b""
    return len(values) == 1 and secrets.compare_digest(candidate, expected)


def _same_loopback_origin(request: Request, endpoint: str) -> bool:
    if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return False
    origin = request.headers.get("origin")
    return origin is None or origin == endpoint
