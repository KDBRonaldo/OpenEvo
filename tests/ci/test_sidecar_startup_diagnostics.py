from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _stock_loader_failure(*, team_phrase: str = "different Team IDs") -> bytes:
    return (
        "[PYI-43120:ERROR] Failed to load Python shared library "
        "'/private/var/folders/secret/_MEI123/Python': "
        "dlopen(https://user:token@example.invalid/private): code signature in "
        "<Python.framework token=super-secret> not valid for use in process: "
        f"mapping process and mapped file (non-platform) have {team_phrase}"
    ).encode("utf-8")


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
    from desktop.server import launcher

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
        (stage, code) for stage, codes in smoke._STARTUP_DIAGNOSTIC_CODES.items() for code in codes
    }

    assert producer_pairs == allowlisted_pairs
    assert launcher._PACKAGED_STARTUP_CODES == {
        code for stage, code in producer_pairs if stage == "python_launcher"
    }


def test_stock_loader_failure_maps_to_closed_diagnostic_without_canaries(
    tmp_path: Path,
) -> None:
    classifier = _load_module(
        "openevo_stock_startup_classifier_test",
        "scripts/ci/openevo_startup_diagnostics.py",
    )
    smoke = _load_module(
        "openevo_sidecar_smoke_stock_loader_test",
        "scripts/ci/smoke_openevo_desktop_sidecar.py",
    )
    raw = _stock_loader_failure()

    assert classifier.classify_stock_loader_line(raw) == (
        "embedded_python_loader",
        "python_shared_library_validation_failed",
    )
    process_log = tmp_path / "process.log"
    process_log.write_bytes(raw + b"\n")
    with process_log.open("rb") as stream:
        failure = smoke._render_process_failure(255, stream)

    assert failure == (
        "sidecar exited before serving /health (exit 255).\n"
        "startup diagnostics:\n"
        "OPENEVO_STARTUP_CLASSIFIED_V1 stage=embedded_python_loader "
        "code=python_shared_library_validation_failed"
    )
    assert "/private/" not in failure
    assert "token" not in failure
    assert "https://" not in failure


@pytest.mark.parametrize(
    "line",
    [
        _stock_loader_failure(team_phrase="the same Team IDs"),
        _stock_loader_failure().replace(b"[PYI-43120:ERROR]", b"[PYI-X:ERROR]"),
        _stock_loader_failure().replace(b"Failed to load", b"Could not load"),
        _stock_loader_failure().replace(b"code signature in", b"signature in"),
    ],
)
def test_stock_loader_classifier_rejects_near_matches(line: bytes) -> None:
    classifier = _load_module(
        "openevo_stock_startup_classifier_near_match_test",
        "scripts/ci/openevo_startup_diagnostics.py",
    )

    assert classifier.classify_stock_loader_line(line) is None


def test_stock_loader_classifier_rejects_over_budget_line() -> None:
    classifier = _load_module(
        "openevo_stock_startup_classifier_budget_test",
        "scripts/ci/openevo_startup_diagnostics.py",
    )
    payload = _stock_loader_failure() + b"X" * classifier.STARTUP_OUTPUT_LINE_MAX_BYTES

    assert classifier.classify_stock_loader_line(payload) is None


def test_unknown_startup_output_keeps_only_count_category_and_fingerprint(
    tmp_path: Path,
) -> None:
    smoke = _load_module(
        "openevo_sidecar_smoke_unknown_summary_test",
        "scripts/ci/smoke_openevo_desktop_sidecar.py",
    )
    process_log = tmp_path / "process.log"
    process_log.write_bytes(
        b"Traceback /Users/private token=super-secret\n"
        b"https://user:password@example.invalid/private\n"
    )

    with process_log.open("rb") as stream:
        failure = smoke._render_process_failure(255, stream)

    assert re.search(
        r"OPENEVO_STARTUP_UNKNOWN_V1 category=unclassified count=2 "
        r"fingerprint=sha256:[0-9a-f]{64}$",
        failure,
    )
    assert "/Users/" not in failure
    assert "token" not in failure
    assert "https://" not in failure


def test_bundle_smoke_accepts_only_closed_native_startup_failure_marker() -> None:
    bundle = _load_module(
        "openevo_bundle_smoke_startup_classifier_test",
        "scripts/ci/smoke_openevo_desktop_bundle.py",
    )
    payload = (
        b"raw /Users/private token=secret\n"
        b"OPENEVO_DESKTOP_STARTUP_FAILURE_V1 stage=embedded_python_loader "
        b"code=python_shared_library_validation_failed\n"
    )

    observation = bundle._parse_native_host_observation(payload)

    assert observation.startup_failure == (
        "embedded_python_loader",
        "python_shared_library_validation_failed",
    )
    assert "/Users/" not in repr(observation)
    assert "token" not in repr(observation)


@pytest.mark.parametrize(
    "marker",
    [
        b"OPENEVO_DESKTOP_STARTUP_FAILURE_V1 stage=embedded_python_loader "
        b"code=unknown_failure\n",
        b"OPENEVO_DESKTOP_STARTUP_FAILURE_V1 stage=embedded_python_loader "
        b"code=python_shared_library_validation_failed raw=/Users/private\n",
        b"OPENEVO_DESKTOP_STARTUP_FAILURE_V2 stage=embedded_python_loader "
        b"code=python_shared_library_validation_failed\n",
    ],
)
def test_bundle_smoke_rejects_non_contract_startup_failure_marker(marker: bytes) -> None:
    bundle = _load_module(
        "openevo_bundle_smoke_invalid_startup_classifier_test",
        "scripts/ci/smoke_openevo_desktop_bundle.py",
    )

    with pytest.raises(bundle.SmokeFailure, match="marker is malformed"):
        bundle._parse_native_host_observation(marker)


def test_bundle_smoke_requires_a_successful_v2_startup_attempt_without_canaries() -> None:
    bundle = _load_module(
        "openevo_bundle_smoke_v2_startup_envelope_test",
        "scripts/ci/smoke_openevo_desktop_bundle.py",
    )
    attempt_id = "a" * 32

    def stage(sequence: int, attempt_sequence: int, name: str, result: str) -> dict[str, object]:
        return {
            "schema_version": "2",
            "sequence": sequence,
            "occurred_at": f"2026-07-23T01:02:03.{sequence:03d}Z",
            "attempt_id": attempt_id,
            "attempt_ordinal": 4,
            "attempt_sequence": attempt_sequence,
            "component": "renderer" if name.startswith("renderer_") else "native",
            "level": "error" if result == "failed" else "info",
            "event": "startup_stage",
            "stage": name,
            "result": result,
            "code": "ready" if name == "renderer_ready" else "stage_complete",
            "duration_bucket": "under_1s",
            "product_version": "0.1.9",
            "source_commit": "b" * 40,
            "exit_code": None,
            "signal": None,
            "errno": None,
        }

    summary = bundle._validate_startup_diagnostic_events(
        (
            stage(7, 1, "native_application", "completed"),
            stage(8, 2, "local_api", "completed"),
            stage(9, 3, "renderer_bootstrap", "completed"),
            stage(10, 4, "renderer_ready", "completed"),
        ),
        checkpoint=6,
    )

    assert summary == {
        "schema_version": "2",
        "attempt_id": attempt_id,
        "attempt_ordinal": 4,
        "last_completed_stage": "renderer_ready",
        "first_failed_stage": None,
        "duration_bucket": "under_1s",
        "product_version": "0.1.9",
        "source_commit": "b" * 40,
    }
    encoded = json.dumps(summary, sort_keys=True)
    assert "/Users/" not in encoded
    assert "token" not in encoded


def test_bundle_smoke_rejects_a_failed_v2_startup_attempt() -> None:
    bundle = _load_module(
        "openevo_bundle_smoke_failed_v2_startup_envelope_test",
        "scripts/ci/smoke_openevo_desktop_bundle.py",
    )
    event = {
        "schema_version": "2",
        "sequence": 7,
        "occurred_at": "2026-07-23T01:02:03.007Z",
        "attempt_id": "a" * 32,
        "attempt_ordinal": 4,
        "attempt_sequence": 1,
        "component": "startup",
        "level": "error",
        "event": "startup_stage",
        "stage": "state_store",
        "result": "failed",
        "code": "provider_store_failed",
        "duration_bucket": "under_1s",
        "product_version": "0.1.9",
        "source_commit": None,
        "exit_code": None,
        "signal": None,
        "errno": None,
    }

    with pytest.raises(bundle.SmokeFailure, match="failed startup attempt"):
        bundle._validate_startup_diagnostic_events((event,), checkpoint=6)


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

    assert failure.startswith(
        "sidecar exited before serving /health (exit 255).\n"
        "startup diagnostics:\n"
        "OPENEVO_STARTUP_V1 stage=bootloader_archive code=archive_open_failed\n"
        "OPENEVO_STARTUP_V1 stage=python_metadata code=load_failed errno=13\n"
    )
    assert re.search(
        r"OPENEVO_STARTUP_UNKNOWN_V1 category=unclassified count=7 "
        r"fingerprint=sha256:[0-9a-f]{64}$",
        failure,
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


def test_smoke_failure_reserves_bounded_output_for_unknown_summary(tmp_path: Path) -> None:
    smoke = _load_module(
        "openevo_sidecar_smoke_unknown_line_cap_test",
        "scripts/ci/smoke_openevo_desktop_sidecar.py",
    )
    valid = b"OPENEVO_STARTUP_V1 stage=python_launcher code=execution_failed\n"
    process_log = tmp_path / "process.log"
    process_log.write_bytes(valid * smoke.STARTUP_DIAGNOSTIC_MAX_LINES + b"secret output\n")

    with process_log.open("rb") as stream:
        diagnostics = smoke._read_startup_diagnostics(stream)

    assert len(diagnostics) == smoke.STARTUP_DIAGNOSTIC_MAX_LINES
    assert diagnostics[-1].startswith(
        "OPENEVO_STARTUP_UNKNOWN_V1 category=unclassified count=1 "
    )
    assert "secret output" not in "\n".join(diagnostics)


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

    assert re.search(
        r"OPENEVO_STARTUP_UNKNOWN_V1 category=unclassified count=1 "
        r"fingerprint=sha256:[0-9a-f]{64}$",
        failure,
    )
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


def test_python_startup_dispatches_system_ssh_owner_before_native_handoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    entry = _load_module(
        "openevo_sidecar_entry_system_ssh_owner_test",
        "desktop/packaging/sidecar_entry.py",
    )
    from openevo.deployment import system_executables

    calls: list[list[str]] = []
    arguments = [
        "openevo-desktop-sidecar",
        system_executables.SYSTEM_OPENSSH_OWNER_ARGUMENT,
        "17",
        system_executables.SSH_EXECUTABLE,
        "-V",
    ]
    monkeypatch.setattr(entry.sys, "argv", arguments)
    monkeypatch.setattr(
        system_executables,
        "run_packaged_system_openssh_owner",
        lambda value: calls.append(value),
    )

    assert entry._startup_main() == 126
    assert calls == [arguments]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


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
    source = (REPOSITORY_ROOT / "scripts/ci/smoke_openevo_desktop_sidecar.py").read_text(
        encoding="utf-8"
    )

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
        lambda: entry._PackagedBuildMetadata(
            source_commit="abcdef1",
            ssh_askpass_helper=entry._PackagedAskpassHelper(
                architecture="arm64",
                byte_size=42,
                filename="openevo-ssh-askpass",
                mode="0755",
                sha256="a" * 64,
                signature="adhoc",
                target_triple="aarch64-apple-darwin",
            ),
        ),
    )
    monkeypatch.setattr(
        launcher,
        "main",
        lambda **_: (_ for _ in ()).throw(SystemExit("Traceback /Users/private token=secret")),
    )

    assert entry._startup_main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "OPENEVO_STARTUP_V1 stage=python_launcher code=execution_failed\n"
    assert "Traceback" not in captured.err
    assert "/Users/private" not in captured.err
    assert "token=secret" not in captured.err


def test_python_startup_preserves_typed_redacted_launcher_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    entry = _load_module(
        "openevo_sidecar_entry_typed_launcher_failure_test",
        "desktop/packaging/sidecar_entry.py",
    )
    from desktop.server import launcher

    monkeypatch.setattr(entry.sys, "argv", ["openevo-desktop-sidecar"])
    monkeypatch.setenv(entry.NATIVE_LISTENER_FD_ENV, "3")
    monkeypatch.setenv(entry.NATIVE_EXECUTABLE_FD_ENV, "4")
    monkeypatch.setattr(
        entry,
        "_load_packaged_build_metadata",
        lambda: entry._PackagedBuildMetadata(
            source_commit="abcdef1",
            ssh_askpass_helper=entry._PackagedAskpassHelper(
                architecture="arm64",
                byte_size=42,
                filename="openevo-ssh-askpass",
                mode="0755",
                sha256="a" * 64,
                signature="adhoc",
                target_triple="aarch64-apple-darwin",
            ),
        ),
    )
    canary = "Traceback /Users/private token=secret"

    def fail_launcher(**_: object) -> int:
        try:
            raise RuntimeError(canary)
        except RuntimeError as exc:
            raise launcher.PackagedLauncherStartupError("core_bridge_store_failed") from exc

    monkeypatch.setattr(launcher, "main", fail_launcher)

    assert entry._startup_main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err == "OPENEVO_STARTUP_V1 stage=python_launcher code=core_bridge_store_failed\n"
    )
    assert canary not in captured.err


def test_packaged_launcher_startup_error_rejects_unknown_code() -> None:
    from desktop.server import launcher

    with pytest.raises(ValueError, match="startup diagnostic code"):
        launcher.PackagedLauncherStartupError("unknown_failure")
