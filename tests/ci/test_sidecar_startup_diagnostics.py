from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, relative_path: str):
    path = REPOSITORY_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _ExitedProcess:
    def __init__(self, stdout) -> None:
        self.stdout = stdout
        self.returncode = 255

    def wait(self, *, timeout: float) -> int:
        assert timeout == 2
        return self.returncode

    def poll(self) -> int:
        return self.returncode


class _BrokenPipeInput:
    def __init__(self, canary: str) -> None:
        self._canary = canary
        self.closed = False

    def write(self, _payload: bytes) -> int:
        raise BrokenPipeError(self._canary)

    def close(self) -> None:
        self.closed = True


class _BrokenPipeProcess(_ExitedProcess):
    def __init__(self, stdout, canary: str) -> None:
        super().__init__(stdout)
        self.stdin = _BrokenPipeInput(canary)


class _CancellingInput:
    def __init__(self, stream, canary: str) -> None:
        self._stream = stream
        self._canary = canary
        self.closed = False

    def write(self, _payload: bytes) -> int:
        raise SystemExit(self._canary)

    def close(self) -> None:
        self.closed = True
        self._stream.close()


def test_startup_producers_exactly_match_smoke_allowlist() -> None:
    builder = _load_module(
        "openevo_sidecar_builder_startup_contract_test",
        "desktop/packaging/build_sidecar.py",
    )
    entry = _load_module(
        "openevo_sidecar_entry_startup_contract_test",
        "desktop/packaging/sidecar_entry.py",
    )
    smoke = _load_module(
        "openevo_sidecar_smoke_startup_contract_test",
        "scripts/ci/smoke_openevo_desktop_sidecar.py",
    )
    bootloader_source = "\n".join(
        (
            builder._BOOTLOADER_RESOLVER_REPLACEMENT,
            builder._BOOTLOADER_ARCHIVE_REPLACEMENT,
            builder._BOOTLOADER_NATIVE_HANDOFF_REPLACEMENT,
            builder._BOOTLOADER_CHILD_EXEC_REPLACEMENT,
            builder._BOOTLOADER_RESTART_REPLACEMENT,
            builder._BOOTLOADER_CHILD_MAIN_REPLACEMENT,
        )
    )
    producer_pairs = set(entry._PYTHON_STARTUP_DIAGNOSTICS)
    producer_pairs.update(
        re.findall(
            r'OPENEVO_STARTUP_FAILURE\("([a-z][a-z0-9_]*)", "([a-z][a-z0-9_]*)"\)',
            bootloader_source,
        )
    )
    allowlisted_pairs = {
        (stage, code)
        for stage, codes in smoke._STARTUP_DIAGNOSTIC_CODES.items()
        for code in codes
    }

    assert producer_pairs == allowlisted_pairs


def test_smoke_failure_exposes_only_bounded_allowlisted_startup_lines(tmp_path: Path) -> None:
    smoke = _load_module(
        "openevo_sidecar_smoke_startup_test",
        "scripts/ci/smoke_openevo_desktop_sidecar.py",
    )
    canaries = (
        "/Users/private/secret-sidecar",
        "token=super-secret",
        "https://credentials.example/private",
        'Traceback (most recent call last): File "/private/source.py"',
    )
    payload = (
        "\n".join(canaries)
        + "\nOPENEVO_STARTUP_V1 stage=bootloader_archive code=archive_open_failed\n"
        + "OPENEVO_STARTUP_V1 stage=unknown code=archive_open_failed\n"
        + "OPENEVO_STARTUP_V1 stage=python_metadata code=load_failed token=leak\n"
        + "OPENEVO_STARTUP_V1 stage=python_metadata code=load_failed errno=13\n"
        + ("X" * (smoke.STARTUP_DIAGNOSTIC_SCAN_MAX_BYTES * 2))
    ).encode()
    process_log = tmp_path / "process.log"
    process_log.write_bytes(payload)

    with process_log.open("rb") as stream:
        failure = smoke._render_process_failure(255, stream)

    assert failure == (
        "sidecar exited before serving /health (exit 255).\n"
        "startup diagnostics:\n"
        "OPENEVO_STARTUP_V1 stage=bootloader_archive code=archive_open_failed\n"
        "OPENEVO_STARTUP_V1 stage=python_metadata code=load_failed errno=13"
    )
    assert len(failure.encode()) < 1024
    assert not any(canary in failure for canary in canaries)


def test_smoke_failure_caps_valid_diagnostic_lines(tmp_path: Path) -> None:
    smoke = _load_module(
        "openevo_sidecar_smoke_startup_line_cap_test",
        "scripts/ci/smoke_openevo_desktop_sidecar.py",
    )
    line = b"OPENEVO_STARTUP_V1 stage=python_launcher code=execution_failed\n"
    process_log = tmp_path / "process.log"
    process_log.write_bytes(line * (smoke.STARTUP_DIAGNOSTIC_MAX_LINES + 20))

    with process_log.open("rb") as stream:
        failure = smoke._render_process_failure(255, stream)

    assert failure.count("OPENEVO_STARTUP_V1") == smoke.STARTUP_DIAGNOSTIC_MAX_LINES


@pytest.mark.parametrize(
    "line",
    [
        b"OPENEVO_STARTUP_V1 stage=bootloader_archive code=archive_open_failed errno=-1\n",
        b"OPENEVO_STARTUP_V1 stage=bootloader_archive code=archive_open_failed errno=1x\n",
        b"OPENEVO_STARTUP_V1 stage=bootloader_archive code=not_allowlisted\n",
        b"prefix OPENEVO_STARTUP_V1 stage=bootloader_archive code=archive_open_failed\n",
        b"OPENEVO_STARTUP_V1 stage=bootloader_archive code=archive_open_failed trailing\n",
    ],
)
def test_smoke_failure_rejects_non_contract_diagnostics(tmp_path: Path, line: bytes) -> None:
    smoke = _load_module(
        "openevo_sidecar_smoke_invalid_startup_test",
        "scripts/ci/smoke_openevo_desktop_sidecar.py",
    )
    process_log = tmp_path / "process.log"
    process_log.write_bytes(line)

    with process_log.open("rb") as stream:
        failure = smoke._render_process_failure(255, stream)

    assert failure.endswith("no valid OPENEVO_STARTUP_V1 diagnostic was emitted")
    assert "archive_open_failed" not in failure


def test_python_startup_failure_is_redacted(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    entry = _load_module(
        "openevo_sidecar_entry_startup_test",
        "desktop/packaging/sidecar_entry.py",
    )
    canary = "https://user:token@example.invalid/private Traceback /Users/secret"
    monkeypatch.setattr(entry.sys, "argv", ["openevo-desktop-sidecar"])
    monkeypatch.setenv(entry.NATIVE_LISTENER_FD_ENV, "3")
    monkeypatch.setenv(entry.NATIVE_EXECUTABLE_FD_ENV, "4")
    monkeypatch.setattr(
        entry,
        "_load_packaged_build_metadata",
        lambda: (_ for _ in ()).throw(RuntimeError(canary)),
    )

    assert entry._startup_main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "OPENEVO_STARTUP_V1 stage=python_metadata code=load_failed\n"
    assert canary not in captured.err


def test_native_frame_broken_pipe_surfaces_only_closed_startup_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_module(
        "openevo_sidecar_smoke_broken_pipe_test",
        "scripts/ci/smoke_openevo_desktop_sidecar.py",
    )
    canary = "Traceback /Users/private token=secret"
    process_log = tmp_path / "process.log"
    process_log.write_bytes(
        b"OPENEVO_STARTUP_V1 stage=bootloader_archive code=archive_open_failed\n"
    )

    with process_log.open("rb") as stream:
        process = _BrokenPipeProcess(stream, canary)
        monkeypatch.setattr(
            smoke,
            "_process_failure",
            lambda active_process, **_: smoke._render_process_failure(
                active_process.returncode,
                active_process.stdout,
            ),
        )
        with pytest.raises(smoke.SmokeFailure) as rejected:
            smoke._send_native_frame(
                process,
                smoke._NativeCredentials.create(),
                process_group_id=424242,
                exit_observer=object(),
            )

    assert str(rejected.value) == (
        "sidecar exited before serving /health (exit 255).\n"
        "startup diagnostics:\n"
        "OPENEVO_STARTUP_V1 stage=bootloader_archive code=archive_open_failed"
    )
    assert canary not in str(rejected.value)
    assert process.stdin is None


def test_native_frame_cancellation_reaps_owned_process_group(tmp_path: Path) -> None:
    smoke = _load_module(
        "openevo_sidecar_smoke_frame_cancellation_test",
        "scripts/ci/smoke_openevo_desktop_sidecar.py",
    )
    child_path = tmp_path / "descendant.pid"
    leader_program = (
        "import os,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'])\n"
        "pending=Path(str(sys.argv[1])+'.pending')\n"
        "pending.write_text(str(child.pid),encoding='ascii')\n"
        "os.replace(pending,sys.argv[1])\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", leader_program, str(child_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process_group_id = smoke._owned_process_group_id(process)
    observer = smoke._SubprocessExitObserver(process)
    cancellation = _CancellingInput(
        process.stdin,
        "Traceback /Users/private token=secret",
    )
    process.stdin = cancellation
    child_id: int | None = None
    try:
        deadline = time.monotonic() + 3
        while not child_path.exists():
            if time.monotonic() >= deadline:
                pytest.fail("descendant identity was not published")
            time.sleep(0.01)
        child_id = int(child_path.read_text(encoding="ascii"))

        with pytest.raises(SystemExit, match="token=secret"):
            smoke._send_native_frame(
                process,
                smoke._NativeCredentials.create(),
                process_group_id=process_group_id,
                exit_observer=observer,
            )

        assert process.stdin is None
        assert process.stdout.closed is True
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(child_id, 0)
    finally:
        observer.close()
        if process.returncode is None:
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
        if not cancellation.closed:
            cancellation.close()
        if not process.stdout.closed:
            process.stdout.close()


def test_smoke_uses_bounded_pipe_instead_of_disk_backed_process_log() -> None:
    source = (
        REPOSITORY_ROOT / "scripts/ci/smoke_openevo_desktop_sidecar.py"
    ).read_text(encoding="utf-8")

    assert "stdout=subprocess.PIPE" in source
    assert "stderr=subprocess.STDOUT" in source
    assert "TemporaryFile" not in source


@pytest.mark.parametrize(
    "force_authority_failure",
    [False, True],
    ids=("normal", "observer-failure"),
)
def test_terminate_reaps_descendant_that_retains_output_pipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_authority_failure: bool,
) -> None:
    smoke = _load_module(
        "openevo_sidecar_smoke_group_cleanup_test",
        "scripts/ci/smoke_openevo_desktop_sidecar.py",
    )
    child_path = tmp_path / "descendant.pid"
    leader_program = (
        "import os,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'])\n"
        "pending=Path(str(sys.argv[1])+'.pending')\n"
        "pending.write_text(str(child.pid),encoding='ascii')\n"
        "os.replace(pending,sys.argv[1])\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", leader_program, str(child_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    process_group_id = smoke._owned_process_group_id(process)
    observer = smoke._SubprocessExitObserver(process)
    child_id: int | None = None
    try:
        deadline = time.monotonic() + 3
        while not child_path.exists():
            if time.monotonic() >= deadline:
                pytest.fail("descendant identity was not published")
            time.sleep(0.01)
        child_id = int(child_path.read_text(encoding="ascii"))

        if force_authority_failure:
            monkeypatch.setattr(
                smoke,
                "_terminate_and_reap_subprocess",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("process-group observation failed")
                ),
            )
            with pytest.raises(
                smoke.SmokeFailure,
                match="process-group authority failed closed",
            ):
                smoke._terminate(
                    process,
                    process_group_id=process_group_id,
                    exit_observer=observer,
                )
        else:
            smoke._terminate(
                process,
                process_group_id=process_group_id,
                exit_observer=observer,
            )

        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(child_id, 0)
    finally:
        observer.close()
        if process.returncode is None:
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
        process.stdout.close()


def test_python_startup_redacts_launcher_system_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    entry = _load_module(
        "openevo_sidecar_entry_system_exit_test",
        "desktop/packaging/sidecar_entry.py",
    )
    from desktop.server import launcher

    monkeypatch.setattr(entry.sys, "argv", ["openevo-desktop-sidecar"])
    monkeypatch.setenv(entry.NATIVE_LISTENER_FD_ENV, "3")
    monkeypatch.setenv(entry.NATIVE_EXECUTABLE_FD_ENV, "4")
    monkeypatch.setattr(
        entry,
        "_load_packaged_build_metadata",
        lambda: entry._PackagedBuildMetadata(source_commit="abcdef1"),
    )
    monkeypatch.setattr(
        launcher,
        "main",
        lambda **_: (_ for _ in ()).throw(
            SystemExit("Traceback /Users/private token=secret")
        ),
    )

    assert entry._startup_main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "OPENEVO_STARTUP_V1 stage=python_launcher code=execution_failed\n"
    assert "Traceback" not in captured.err
    assert "/Users/private" not in captured.err
    assert "token=secret" not in captured.err
