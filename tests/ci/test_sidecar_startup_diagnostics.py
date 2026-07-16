from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys

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
    returncode = 255

    def wait(self, *, timeout: float) -> int:
        assert timeout == 2
        return self.returncode


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
        failure = smoke._process_failure(_ExitedProcess(), stream)

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
        failure = smoke._process_failure(_ExitedProcess(), stream)

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
        failure = smoke._process_failure(_ExitedProcess(), stream)

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


def test_python_startup_preserves_launcher_system_exit(
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
        lambda **_: (_ for _ in ()).throw(SystemExit(37)),
    )

    with pytest.raises(SystemExit) as exc:
        entry._startup_main()

    assert exc.value.code == 37
    assert capsys.readouterr().err == ""
