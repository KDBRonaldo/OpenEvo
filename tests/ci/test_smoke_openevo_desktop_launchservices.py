from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts/ci/smoke_openevo_desktop_launchservices.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("smoke_openevo_desktop_launchservices", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(module, pid: int, parent: int, birth: str = "Mon Jan  2 03:04:05 2023"):
    return module.ProcessRow(module.ProcessIdentity(pid, birth), parent, "/ignored")


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
    assert smoke._app_roots(system, [candidate, unrelated], executable) == {
        candidate.identity
    }
    assert system.probed == [candidate.identity.pid, unrelated.identity.pid]


def test_validate_version_rejects_malformed_release_provider() -> None:
    smoke = _load_module()
    payload = {
        "schema_version": "1",
        "api_name": "openevo-desktop-local-api",
        "preferred_major": 1,
        "supported_majors": [1],
        "openapi_sha256": "a" * 64,
        "build_version": "0.1.7",
        "source_commit": "b" * 40,
        "build_channel": "release",
        "provider_kind": "test_provider",
        "feature_flags": [],
    }

    with pytest.raises(smoke.SmokeFailure, match="expected release provider"):
        smoke.validate_version(payload, "0.1.7")


def test_smoke_times_out_when_no_owned_sidecar_appears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        smoke.smoke_launchservices(
            app,
            expected_version="0.1.7",
            timeout_seconds=1,
            evidence_out=tmp_path / "evidence.json",
            system=System(),
        )


def test_cleanup_signals_only_observed_identity_not_pid_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_successful_smoke_writes_closed_non_sensitive_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = _load_module()
    app, executable = _app_bundle(tmp_path)
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

    version = {
        "schema_version": "1", "api_name": "openevo-desktop-local-api", "preferred_major": 1,
        "supported_majors": [1], "openapi_sha256": "a" * 64, "build_version": "0.1.7",
        "source_commit": "b" * 40, "build_channel": "release", "provider_kind": "desktop_sidecar",
        "feature_flags": [],
    }

    class System:
        launched = False
        alive = True

        def snapshot(self):
            return rows if self.launched and self.alive else []

        def process_path(self, pid):
            if pid == app_identity.pid:
                return str(executable)
            if pid == sidecar_identity.pid:
                return "/private/var/folders/x/openevo-desktop-sidecar"
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
    evidence = smoke.smoke_launchservices(
        app, expected_version="0.1.7", timeout_seconds=2, evidence_out=evidence_path, system=System()
    )
    assert evidence == json.loads(evidence_path.read_text())
    assert set(evidence) == {
        "architecture", "build_version", "cleanup", "launch_origin", "os_major",
        "schema_version", "sidecar_ready", "version_verified",
    }
    assert evidence["cleanup"]["authority_limited_to_observed_tree"] is True


def _app_bundle(tmp_path: Path) -> tuple[Path, Path]:
    app = (tmp_path / "OpenEvo Desktop.app").resolve()
    executable = app / "Contents" / "MacOS" / "openevo-desktop"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    with (app / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": executable.name}, stream)
    return app, executable
