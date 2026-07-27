from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts/ci/smoke_openevo_desktop_launchservices.py"
)
RELEASE_OPENAPI_SHA256 = "f0996184595992a22ec6abd257d9040342c9d2f7a31a9882b4a0597061594760"
RELEASE_EVENT_SCHEMA_SHA256 = "515b6d90e9ebdf3f5b4f7c4a57a1924dc85011536d9396b1ab3a5dc73fc48b6b"
RELEASE_FEATURE_FLAGS = [
    "core_control_v2",
    "daemon_bundle_v2",
    "event_replay_v2",
    "host_key_review",
    "lifecycle_operations_v2",
    "lifecycle_process_logs_v2",
    "mutation_idempotency_v2",
    "native_askpass",
    "system_openssh_profiles",
    "task_admission_v2",
]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "smoke_openevo_desktop_launchservices", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(module, pid: int, parent: int, birth: str = "Mon Jan  2 03:04:05 2023"):
    return module.ProcessRow(module.ProcessIdentity(pid, birth), parent, "/ignored")


def _version_payload() -> dict[str, object]:
    return {
        "schema_version": "2",
        "api_name": "openevo-desktop-local-api",
        "preferred_major": 2,
        "supported_majors": [2],
        "mutation_major": 2,
        "openapi_sha256": RELEASE_OPENAPI_SHA256,
        "event_schema_sha256": RELEASE_EVENT_SCHEMA_SHA256,
        "release_version": "0.1.10",
        "build_id": "a" * 64,
        "source_commit": "b" * 40,
        "build_channel": "release",
        "provider_kind": "desktop_sidecar",
        "feature_flags": RELEASE_FEATURE_FLAGS,
        "feature_set_sha256": hashlib.sha256(
            json.dumps(
                RELEASE_FEATURE_FLAGS,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "required_core_api_major": 2,
        "mutation_compatible": True,
    }


def test_parse_ps_rows_keeps_birth_identity_and_rejects_partial_rows() -> None:
    smoke = _load_module()
    rows = smoke.parse_ps_rows(
        "  101     1 Mon Jan  2 03:04:05 2023 /Applications/OpenEvo Desktop.app/Contents/MacOS/openevo-desktop\n"
        "bad row\n"
    )

    assert rows == [
        smoke.ProcessRow(
            smoke.ProcessIdentity(101, "Mon Jan  2 03:04:05 2023"),
            1,
            "/Applications/OpenEvo Desktop.app/Contents/MacOS/openevo-desktop",
        )
    ]


def test_parse_lsof_listeners_only_accepts_loopback_listening_records() -> None:
    smoke = _load_module()
    owner = smoke.ProcessIdentity(71, "Mon Jan  2 03:04:05 2023")
    listeners = smoke.parse_lsof_listeners(
        "p71\nf3\ntIPv4\nn127.0.0.1:41731\nTST=LISTEN\n"
        "f4\ntIPv4\nn0.0.0.0:8888\nTST=LISTEN\n"
        "f5\ntIPv4\nn127.0.0.1:7777\n",
        owner,
    )

    assert listeners == [smoke.Listener(owner, 41731)]


def test_listener_inventory_requests_every_field_required_by_parser() -> None:
    smoke = _load_module()
    owner = smoke.ProcessIdentity(71, "Mon Jan  2 03:04:05 2023")
    observed: list[list[str]] = []

    class System(smoke.DarwinSystem):
        def __init__(self):
            pass

        def command(self, arguments, timeout=smoke.COMMAND_TIMEOUT_SECONDS):
            observed.append(arguments)
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout="f3\ntIPv4\nn127.0.0.1:41731\nTST=LISTEN\n",
            )

    assert System().listener_rows(owner) == [smoke.Listener(owner, 41731)]
    assert observed[0][-1] == "-FftnT"


def test_descendants_do_not_adopt_pid_reuse_or_sibling_processes() -> None:
    smoke = _load_module()
    original = smoke.ProcessIdentity(10, "Mon Jan  2 03:04:05 2023")
    reused = _row(smoke, 10, 1, "Tue Jan  3 03:04:05 2023")
    child = _row(smoke, 11, 10)
    sibling = _row(smoke, 12, 1)

    assert smoke.descendants([reused, child, sibling], [original]) == set()


def test_multiple_sidecar_listeners_fail_closed() -> None:
    smoke = _load_module()
    one = smoke.ProcessIdentity(20, "Mon Jan  2 03:04:05 2023")
    two = smoke.ProcessIdentity(21, "Mon Jan  2 03:04:05 2023")

    class System:
        def listener_rows(self, identity):
            return [smoke.Listener(identity, 41000 if identity == one else 41001)]

    with pytest.raises(smoke.SmokeFailure, match="multiple loopback listeners"):
        smoke._single_listener(System(), {one, two})


def test_app_roots_uses_exact_process_paths_not_command_text(tmp_path: Path) -> None:
    smoke = _load_module()
    _app, executable = _app_bundle(tmp_path)
    candidate = smoke.ProcessRow(
        smoke.ProcessIdentity(20, "Mon Jan  2 03:04:05 2023"),
        1,
        f"{executable} --release",
    )
    unrelated = smoke.ProcessRow(
        smoke.ProcessIdentity(21, "Mon Jan  2 03:04:05 2023"),
        1,
        "/usr/bin/unrelated",
    )

    class System:
        probed: list[int] = []

        def process_path(self, pid):
            self.probed.append(pid)
            return str(executable) if pid == candidate.identity.pid else "/usr/bin/unrelated"

    system = System()
    assert smoke._app_roots(system, [candidate, unrelated], executable) == {candidate.identity}
    assert system.probed == [candidate.identity.pid, unrelated.identity.pid]


def test_validate_version_accepts_closed_v2_release() -> None:
    smoke = _load_module()

    smoke.validate_version(_version_payload(), "0.1.10")


def test_validate_version_rejects_malformed_release_provider() -> None:
    smoke = _load_module()
    payload = _version_payload()
    payload["provider_kind"] = "test_provider"

    with pytest.raises(smoke.SmokeFailure, match="expected release provider"):
        smoke.validate_version(payload, "0.1.10")


def test_validate_version_rejects_unbound_feature_set() -> None:
    smoke = _load_module()
    payload = _version_payload()
    payload["feature_set_sha256"] = "c" * 64

    with pytest.raises(smoke.SmokeFailure, match="malformed"):
        smoke.validate_version(payload, "0.1.10")


def test_launchservices_reads_only_new_closed_startup_failure(tmp_path: Path) -> None:
    smoke = _load_module()
    log_root = tmp_path / "logs-v1"
    log_root.mkdir(mode=0o700)
    log_file = log_root / "desktop.jsonl"
    events = [
        {
            "schema_version": "1",
            "sequence": 6,
            "occurred_at": "2026-07-23T01:02:03.004Z",
            "source": "startup",
            "level": "error",
            "event": "sidecar_startup_diagnostic",
            "code": "embedded_python_loader_python_shared_library_validation_failed",
            "exit_code": None,
            "signal": None,
            "errno": None,
        },
        {
            "schema_version": "2",
            "sequence": 7,
            "occurred_at": "2026-07-23T01:02:03.005Z",
            "attempt_id": "a" * 32,
            "attempt_ordinal": 2,
            "attempt_sequence": 4,
            "component": "startup",
            "level": "error",
            "event": "startup_stage",
            "stage": "embedded_python",
            "result": "failed",
            "code": "python_shared_library_validation_failed",
            "duration_bucket": "under_1s",
            "product_version": "0.1.10",
            "source_commit": None,
            "exit_code": None,
            "signal": None,
            "errno": None,
        },
        {
            "schema_version": "2",
            "sequence": 8,
            "occurred_at": "2026-07-23T01:02:03.006Z",
            "attempt_id": "a" * 32,
            "attempt_ordinal": 2,
            "attempt_sequence": 5,
            "component": "sidecar",
            "level": "warning",
            "event": "sidecar_unstructured_output_discarded",
            "stage": None,
            "result": None,
            "code": "unknown_2_sha256_" + "a" * 64,
            "duration_bucket": None,
            "product_version": "0.1.10",
            "source_commit": "b" * 40,
            "exit_code": None,
            "signal": None,
            "errno": None,
        },
    ]
    log_file.write_text(
        "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
        encoding="utf-8",
    )
    log_file.chmod(0o600)

    assert smoke._startup_log_checkpoint(log_root) == 8
    assert smoke._startup_failure_since(log_root, 6) == (
        "embedded_python_loader",
        "python_shared_library_validation_failed",
    )
    assert smoke._startup_failure_since(log_root, 7) is None


def test_launchservices_rejects_open_v2_startup_envelopes(tmp_path: Path) -> None:
    smoke = _load_module()
    log_root = tmp_path / "logs-v1"
    log_root.mkdir(mode=0o700)
    log_file = log_root / "desktop.jsonl"
    event = {
        "schema_version": "2",
        "sequence": 7,
        "occurred_at": "2026-07-23T01:02:03.005Z",
        "attempt_id": "a" * 32,
        "attempt_ordinal": 2,
        "attempt_sequence": 4,
        "component": "startup",
        "level": "error",
        "event": "startup_stage",
        "stage": "embedded_python",
        "result": "failed",
        "code": "python_shared_library_validation_failed",
        "duration_bucket": "under_1s",
        "product_version": "0.1.10",
        "source_commit": None,
        "exit_code": None,
        "signal": None,
        "errno": None,
        "raw_path": "/Users/private/token=secret",
    }
    log_file.write_text(json.dumps(event) + "\n", encoding="utf-8")
    log_file.chmod(0o600)

    assert smoke._startup_log_events(log_root) == ()
    assert smoke._startup_failure_since(log_root, 0) is None


def test_smoke_times_out_when_no_owned_sidecar_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = _load_module()
    app, executable = _app_bundle(tmp_path)
    monkeypatch.setattr(smoke.sys, "platform", "darwin")
    ticks = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(ticks, 2.0))
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)

    class System:
        def snapshot(self):
            return []

        def process_path(self, _pid):
            return str(executable)

        def remove_quarantine(self, _app):
            pass

        def launch(self, _app):
            pass

        def signal(self, _identity, _signal):
            raise AssertionError("nothing was owned")

    with pytest.raises(smoke.SmokeFailure, match="timed out"):
        source_dmg = tmp_path / "OpenEvo-Desktop-0.1.7-aarch64.dmg"
        source_dmg.write_bytes(b"candidate dmg")
        smoke.smoke_launchservices(
            app,
            expected_version="0.1.7",
            timeout_seconds=1,
            evidence_out=tmp_path / "evidence.json",
            source_dmg=source_dmg,
            system=System(),
        )


def test_cleanup_signals_only_observed_identity_not_pid_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_module()
    original = smoke.ProcessIdentity(40, "Mon Jan  2 03:04:05 2023")
    reused = _row(smoke, 40, 1, "Tue Jan  3 03:04:05 2023")
    signalled = []
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)

    class System:
        def snapshot(self):
            return [reused]

        def signal(self, identity, sig):
            signalled.append((identity, sig))
            return True

    assert smoke._cleanup(System(), {original}, 0.1) is True
    assert signalled == []


def test_successful_smoke_writes_closed_non_sensitive_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = _load_module()
    app, executable = _app_bundle(tmp_path)
    sidecar = executable.parent / "openevo-desktop-sidecar"
    app_identity = smoke.ProcessIdentity(50, "Mon Jan  2 03:04:05 2023")
    sidecar_identity = smoke.ProcessIdentity(51, "Mon Jan  2 03:04:06 2023")
    rows = [
        smoke.ProcessRow(app_identity, 1, str(executable)),
        _row(smoke, 51, 50, "Mon Jan  2 03:04:06 2023"),
    ]
    monkeypatch.setattr(smoke.sys, "platform", "darwin")
    monkeypatch.setattr(smoke.platform, "mac_ver", lambda: ("15.5", (15, 5, 0), ""))
    monkeypatch.setattr(smoke.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)

    version = _version_payload()

    class System:
        launched = False
        alive = True

        def snapshot(self):
            return rows if self.launched and self.alive else []

        def process_path(self, pid):
            if pid == app_identity.pid:
                return str(executable)
            if pid == sidecar_identity.pid:
                return str(sidecar)
            return None

        def remove_quarantine(self, _app):
            pass

        def launch(self, _app):
            self.launched = True

        def listener_rows(self, identity):
            return [smoke.Listener(identity, 41111)] if identity == sidecar_identity else []

        def http_version(self, _port, _timeout):
            return json.dumps(version).encode()

        def signal(self, _identity, _sig):
            self.alive = False
            return True

    evidence_path = tmp_path / "evidence.json"
    source_dmg = tmp_path / "OpenEvo-Desktop-0.1.10-aarch64.dmg"
    source_dmg.write_bytes(b"candidate dmg")
    evidence = smoke.smoke_launchservices(
        app,
        expected_version="0.1.10",
        timeout_seconds=2,
        evidence_out=evidence_path,
        source_dmg=source_dmg,
        system=System(),
    )
    assert evidence == json.loads(evidence_path.read_text())
    assert set(evidence) == {
        "architecture",
        "binary_sha256",
        "build_version",
        "cleanup",
        "launch_origin",
        "os_major",
        "process_image_bound",
        "quarantine_present_before_allow",
        "quarantine_removed_before_launch",
        "schema_version",
        "sidecar_ready",
        "source_dmg",
        "version_verified",
    }
    assert evidence["cleanup"]["authority_limited_to_observed_tree"] is True
    assert evidence["quarantine_present_before_allow"] is True
    assert evidence["quarantine_removed_before_launch"] is True
    assert evidence["process_image_bound"] is True
    assert evidence["source_dmg"] == {
        "filename": source_dmg.name,
        "sha256": hashlib.sha256(source_dmg.read_bytes()).hexdigest(),
    }
    assert evidence["binary_sha256"] == {
        "bundled_external_bin": hashlib.sha256(sidecar.read_bytes()).hexdigest(),
        "native_executable": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }


def test_smoke_retries_owned_listener_until_version_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_module()
    app, executable = _app_bundle(tmp_path)
    sidecar = executable.parent / "openevo-desktop-sidecar"
    app_identity = smoke.ProcessIdentity(50, "Mon Jan  2 03:04:05 2023")
    sidecar_identity = smoke.ProcessIdentity(51, "Mon Jan  2 03:04:06 2023")
    rows = [
        smoke.ProcessRow(app_identity, 1, str(executable)),
        _row(smoke, 51, 50, "Mon Jan  2 03:04:06 2023"),
    ]
    monkeypatch.setattr(smoke.sys, "platform", "darwin")
    monkeypatch.setattr(smoke.platform, "mac_ver", lambda: ("15.5", (15, 5, 0), ""))
    monkeypatch.setattr(smoke.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)
    version = _version_payload()

    class System:
        launched = False
        alive = True
        attempts = 0

        def snapshot(self):
            return rows if self.launched and self.alive else []

        def process_path(self, pid):
            return str(executable) if pid == 50 else str(sidecar)

        def remove_quarantine(self, _app):
            pass

        def launch(self, _app):
            self.launched = True

        def listener_rows(self, identity):
            return [smoke.Listener(identity, 41111)] if identity == sidecar_identity else []

        def http_version(self, _port, _timeout):
            self.attempts += 1
            if self.attempts == 1:
                raise smoke.SidecarNotReady("not ready")
            return json.dumps(version).encode()

        def signal(self, _identity, _sig):
            self.alive = False
            return True

    system = System()
    source_dmg = tmp_path / "OpenEvo-Desktop-0.1.10-aarch64.dmg"
    source_dmg.write_bytes(b"candidate dmg")
    smoke.smoke_launchservices(
        app,
        expected_version="0.1.10",
        timeout_seconds=2,
        evidence_out=tmp_path / "evidence.json",
        source_dmg=source_dmg,
        system=system,
    )
    assert system.attempts == 2


def _app_bundle(tmp_path: Path) -> tuple[Path, Path]:
    app = (tmp_path / "OpenEvo Desktop.app").resolve()
    executable = app / "Contents" / "MacOS" / "openevo-desktop"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    sidecar = executable.parent / "openevo-desktop-sidecar"
    sidecar.write_text("#!/bin/sh\n", encoding="utf-8")
    sidecar.chmod(0o755)
    with (app / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": executable.name}, stream)
    return app, executable
