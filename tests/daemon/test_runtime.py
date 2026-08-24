from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo.daemon.runtime import (
    DaemonRuntime,
    DaemonRuntimePaths,
    DaemonStartOptions,
)


class FakeProcess:
    pid = 4321

    def poll(self) -> None:
        return None


def _paths(tmp_path: Path) -> DaemonRuntimePaths:
    return DaemonRuntimePaths.resolve(tmp_path / "daemon")


def test_build_command_is_loopback_only(tmp_path: Path) -> None:
    runtime = DaemonRuntime(paths=_paths(tmp_path), python_executable="python")

    command = runtime.build_command(DaemonStartOptions(port=9000))

    assert command[:4] == ["python", "-m", "openevo.daemon", "serve"]
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "9000"
    with pytest.raises(ValueError, match="must bind to loopback"):
        runtime.build_command(DaemonStartOptions(host="0.0.0.0"))


def test_start_persists_private_identity_and_reuses_token(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def popen(command: list[str], **kwargs: object) -> FakeProcess:
        calls.append((command, kwargs))
        return FakeProcess()

    paths = _paths(tmp_path)
    runtime = DaemonRuntime(
        paths=paths,
        python_executable="python",
        popen=popen,
        process_alive=lambda pid: pid == 4321,
        process_identity=lambda pid: f"identity-{pid}",
        ready_probe=lambda host, port: host == "127.0.0.1" and port == 8787,
        sleep=lambda _: None,
    )

    first_token = runtime.ensure_token()
    result = runtime.start(DaemonStartOptions())
    second_token = runtime.ensure_token()

    assert result.ok is True
    assert result.status.running is True
    assert first_token == second_token
    assert first_token.startswith("oev_")
    assert calls[0][1]["start_new_session"] is True
    state = json.loads(paths.state_path.read_text(encoding="utf-8"))
    assert state["identity"] == "identity-4321"
    assert state["schema_version"] == "1"
    if paths.token_path.stat().st_mode & 0o777:
        assert paths.token_path.stat().st_mode & 0o777 == 0o600


def test_stale_identity_is_cleared_without_signalling(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.run_dir.mkdir(parents=True)
    paths.state_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "pid": 4321,
                "identity": "old",
                "host": "127.0.0.1",
                "port": 8787,
            }
        ),
        encoding="utf-8",
    )
    terminated: list[int] = []
    runtime = DaemonRuntime(
        paths=paths,
        process_alive=lambda _: True,
        process_identity=lambda _: "new",
        terminate=lambda pid, _: terminated.append(pid) is None,
    )

    status = runtime.status()
    stopped = runtime.stop()

    assert status.running is False
    assert status.reason == "stale_state"
    assert stopped.message == "daemon_not_running"
    assert terminated == []
    assert not paths.state_path.exists()


def test_start_timeout_terminates_child_and_clears_state(tmp_path: Path) -> None:
    terminated: list[int] = []
    paths = _paths(tmp_path)
    runtime = DaemonRuntime(
        paths=paths,
        popen=lambda *_args, **_kwargs: FakeProcess(),
        process_alive=lambda _: True,
        process_identity=lambda _: "identity",
        terminate=lambda pid, _: terminated.append(pid) is None or True,
        ready_probe=lambda _host, _port: False,
        sleep=lambda _: None,
    )

    result = runtime.start(DaemonStartOptions(), timeout_s=0)

    assert result.ok is False
    assert result.message == "daemon_start_timeout"
    assert terminated == [4321]
    assert not paths.state_path.exists()


def test_start_timeout_keeps_identity_when_child_cannot_be_stopped(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    runtime = DaemonRuntime(
        paths=paths,
        popen=lambda *_args, **_kwargs: FakeProcess(),
        process_alive=lambda _: True,
        process_identity=lambda _: "identity",
        terminate=lambda _pid, _timeout: False,
        ready_probe=lambda _host, _port: False,
        sleep=lambda _: None,
    )

    result = runtime.start(DaemonStartOptions(), timeout_s=0)

    assert result.ok is False
    assert result.message == "daemon_start_timeout_process_alive"
    assert result.status.running is True
    assert paths.state_path.exists()


def test_unknown_state_schema_is_retained_and_never_signalled(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.run_dir.mkdir(parents=True)
    paths.state_path.write_text(
        json.dumps(
            {
                "schema_version": "future",
                "pid": 4321,
                "identity": "identity",
                "host": "127.0.0.1",
                "port": 8787,
            }
        ),
        encoding="utf-8",
    )
    terminated: list[int] = []
    runtime = DaemonRuntime(
        paths=paths,
        process_alive=lambda _: True,
        process_identity=lambda _: "identity",
        terminate=lambda pid, _: terminated.append(pid) is None,
    )

    status = runtime.status()
    result = runtime.stop()

    assert status.reason == "invalid_state_schema"
    assert result.message == "daemon_not_running"
    assert terminated == []
    assert paths.state_path.exists()
