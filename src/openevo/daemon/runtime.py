"""Managed background lifecycle for the loopback OpenEvo daemon.

The layout and lifecycle approach are adapted from HKUDS/nanobot's MIT-licensed
managed gateway runtime. OpenEvo keeps a smaller Linux-server boundary and its
own wire and state contracts.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from filelock import FileLock


@dataclass(frozen=True)
class DaemonRuntimePaths:
    state_root: Path
    run_dir: Path
    logs_dir: Path
    state_path: Path
    log_path: Path
    token_path: Path

    @classmethod
    def resolve(cls, state_root: Path | str | None = None) -> "DaemonRuntimePaths":
        root = (
            Path(state_root).expanduser()
            if state_root is not None
            else Path(os.environ.get("OPENEVO_DAEMON_STATE_DIR", "~/.openevo/daemon")).expanduser()
        ).resolve(strict=False)
        return cls(
            state_root=root,
            run_dir=root / "run",
            logs_dir=root / "logs",
            state_path=root / "run" / "daemon.json",
            log_path=root / "logs" / "daemon.log",
            token_path=root / "run" / "daemon.token",
        )


@dataclass(frozen=True)
class DaemonStartOptions:
    host: str = "127.0.0.1"
    port: int = 8787


@dataclass(frozen=True)
class DaemonProcessStatus:
    running: bool
    pid: int | None
    host: str
    port: int | None
    started_at: str | None
    reason: str
    state_path: Path
    log_path: Path


@dataclass(frozen=True)
class DaemonProcessResult:
    ok: bool
    message: str
    status: DaemonProcessStatus


class DaemonRuntime:
    """Own exactly one background daemon process for one state root."""

    def __init__(
        self,
        *,
        paths: DaemonRuntimePaths | None = None,
        python_executable: str | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        process_alive: Callable[[int], bool] | None = None,
        process_identity: Callable[[int], str | None] | None = None,
        terminate: Callable[[int, float], bool] | None = None,
        ready_probe: Callable[[str, int], bool] | None = None,
    ) -> None:
        self.paths = paths or DaemonRuntimePaths.resolve()
        self.python_executable = python_executable or sys.executable
        self._popen = popen
        self._sleep = sleep
        self._process_alive = process_alive or _process_is_alive
        self._process_identity = process_identity or _linux_process_identity
        self._terminate_process = terminate or _terminate_process_group
        self._ready_probe = ready_probe or _health_ready
        self._owned_process: Any | None = None

    def build_command(self, options: DaemonStartOptions) -> list[str]:
        _require_loopback_host(options.host)
        _require_port(options.port)
        return [
            self.python_executable,
            "-m",
            "openevo.daemon",
            "serve",
            "--host",
            options.host,
            "--port",
            str(options.port),
            "--state-root",
            str(self.paths.state_root),
            "--token-file",
            str(self.paths.token_path),
        ]

    def start(self, options: DaemonStartOptions, *, timeout_s: float = 10) -> DaemonProcessResult:
        with self._lock():
            current = self.status()
            if current.running:
                return DaemonProcessResult(False, "daemon_already_running", current)
            self._prepare_private_directories()
            self.ensure_token()
            command = self.build_command(options)
            with self.paths.log_path.open("a", encoding="utf-8") as log_handle:
                process = self._popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self._owned_process = process
            pid = int(process.pid)
            started_at = _utc_now()
            state = {
                "schema_version": "1",
                "pid": pid,
                "identity": self._process_identity(pid),
                "host": options.host,
                "port": options.port,
                "started_at": started_at,
                "command": command,
            }
            self._write_state(state)
            deadline = time.monotonic() + max(timeout_s, 0)
            while time.monotonic() < deadline:
                if not self._is_alive(pid):
                    self._clear_state()
                    return DaemonProcessResult(
                        False,
                        "daemon_exited_during_startup",
                        self.status(reason="startup_failed"),
                    )
                if self._ready_probe(options.host, options.port):
                    return DaemonProcessResult(True, "daemon_started", self.status())
                self._sleep(0.1)
            terminated = self._terminate_process(pid, min(max(timeout_s, 0.1), 2.0))
            if not terminated:
                return DaemonProcessResult(
                    False,
                    "daemon_start_timeout_process_alive",
                    self.status(reason="start_timeout_process_alive"),
                )
            self._clear_state()
            return DaemonProcessResult(
                False,
                "daemon_start_timeout",
                self._stopped("start_timeout"),
            )

    def status(self, *, reason: str | None = None) -> DaemonProcessStatus:
        state = self._read_state()
        if state is not None and state.get("schema_version") != "1":
            return self._stopped("invalid_state_schema")
        pid = _as_int(state.get("pid")) if state else None
        host = _as_str(state.get("host")) if state else None
        port = _as_int(state.get("port")) if state else None
        if pid is None or host is None:
            return self._stopped(reason or "not_started")
        assert state is not None
        identity = _as_str(state.get("identity"))
        current_identity = self._process_identity(pid)
        if not self._is_alive(pid) or (
            identity is not None
            and current_identity is not None
            and not secrets.compare_digest(identity, current_identity)
        ):
            self._clear_state()
            return self._stopped(reason or "stale_state")
        return DaemonProcessStatus(
            running=True,
            pid=pid,
            host=host,
            port=port,
            started_at=_as_str(state.get("started_at")),
            reason=reason or ("running" if current_identity is not None else "identity_unavailable"),
            state_path=self.paths.state_path,
            log_path=self.paths.log_path,
        )

    def stop(self, *, timeout_s: float = 20) -> DaemonProcessResult:
        with self._lock():
            status = self.status()
            if not status.running or status.pid is None:
                return DaemonProcessResult(False, "daemon_not_running", status)
            state = self._read_state()
            recorded_identity = _as_str(state.get("identity")) if state else None
            current_identity = self._process_identity(status.pid)
            if recorded_identity is not None and current_identity is None:
                return DaemonProcessResult(False, "daemon_identity_unavailable", status)
            if recorded_identity is not None and not secrets.compare_digest(
                recorded_identity, cast(str, current_identity)
            ):
                self._clear_state()
                return DaemonProcessResult(False, "daemon_state_stale", self._stopped("stale_state"))
            if not self._terminate_process(status.pid, timeout_s):
                return DaemonProcessResult(False, "daemon_stop_timeout", self.status(reason="stop_timeout"))
            self._clear_state()
            return DaemonProcessResult(True, "daemon_stopped", self._stopped("stopped"))

    def restart(
        self,
        options: DaemonStartOptions,
        *,
        timeout_s: float = 20,
    ) -> DaemonProcessResult:
        stopped = self.stop(timeout_s=timeout_s)
        if not stopped.ok and stopped.message not in {"daemon_not_running", "daemon_state_stale"}:
            return stopped
        return self.start(options, timeout_s=timeout_s)

    def ensure_token(self) -> str:
        self._prepare_private_directories()
        try:
            token = self.paths.token_path.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            with suppress(OSError):
                self.paths.token_path.chmod(0o600)
            return token
        token = f"oev_{secrets.token_urlsafe(32)}"
        _atomic_private_text(self.paths.token_path, token + "\n")
        return token

    def read_log_tail(self, *, tail: int = 200) -> list[str]:
        if tail <= 0 or not self.paths.log_path.is_file():
            return []
        try:
            lines = self.paths.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-tail:]

    def _prepare_private_directories(self) -> None:
        for path in (self.paths.state_root, self.paths.run_dir, self.paths.logs_dir):
            path.mkdir(parents=True, exist_ok=True)
            with suppress(OSError):
                path.chmod(0o700)

    def _write_state(self, payload: dict[str, object]) -> None:
        _atomic_private_text(
            self.paths.state_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def _read_state(self) -> dict[str, object] | None:
        try:
            payload = json.loads(self.paths.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return cast(dict[str, object], payload) if isinstance(payload, dict) else None

    def _clear_state(self) -> None:
        self.paths.state_path.unlink(missing_ok=True)

    def _is_alive(self, pid: int) -> bool:
        owned = self._owned_process
        if owned is not None and getattr(owned, "pid", None) == pid:
            poll = getattr(owned, "poll", None)
            if callable(poll):
                return poll() is None
        return self._process_alive(pid)

    def _stopped(self, reason: str) -> DaemonProcessStatus:
        return DaemonProcessStatus(
            running=False,
            pid=None,
            host="127.0.0.1",
            port=None,
            started_at=None,
            reason=reason,
            state_path=self.paths.state_path,
            log_path=self.paths.log_path,
        )

    def _lock(self) -> FileLock:
        self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.paths.run_dir.chmod(0o700)
        return FileLock(str(self.paths.state_path.with_suffix(".lock")))


def read_token_file(path: Path | str) -> str:
    token = Path(path).expanduser().read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("daemon token file is empty")
    return token


def _atomic_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        with suppress(OSError):
            temporary_path.chmod(0o600)
        temporary_path.replace(path)
        with suppress(OSError):
            path.chmod(0o600)
    finally:
        temporary_path.unlink(missing_ok=True)


def _require_loopback_host(host: str) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("OpenEvo daemon must bind to loopback")


def _require_port(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("daemon port must be between 1 and 65535")


def _health_ready(host: str, port: int) -> bool:
    browser_host = "127.0.0.1" if host == "localhost" else host
    if browser_host == "::1":
        browser_host = "[::1]"
    try:
        with urllib.request.urlopen(f"http://{browser_host}:{port}/health", timeout=0.25) as response:
            return response.status == 200
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        return False


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _linux_process_identity(pid: int) -> str | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    closing = value.rfind(")")
    fields = value[closing + 2 :].split() if closing >= 0 else []
    return fields[19] if len(fields) > 19 else None


def _terminate_process_group(pid: int, timeout_s: float) -> bool:
    try:
        process_group = os.getpgid(pid)
    except OSError:
        process_group = None
    try:
        if process_group is None:
            os.kill(pid, signal.SIGTERM)
        else:
            os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + max(timeout_s, 0)
    while time.monotonic() < deadline:
        if not _process_is_alive(pid):
            return True
        time.sleep(0.1)
    with suppress(ProcessLookupError, PermissionError):
        if process_group is None:
            os.kill(pid, signal.SIGKILL)
        else:
            os.killpg(process_group, signal.SIGKILL)
    return not _process_is_alive(pid)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
