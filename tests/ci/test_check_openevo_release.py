from __future__ import annotations

import importlib.util
import json
import hashlib
from io import BytesIO
import os
from pathlib import Path
import plistlib
import signal
import stat
import subprocess
from types import SimpleNamespace
import time
from zipfile import ZipFile

import pytest


GOOD_METADATA = "\n".join(
    [
        "Metadata-Version: 2.4",
        "Name: openevo",
        "Version: 0.1.0",
        "Summary: OpenEvo Desktop and agent evolution orchestration.",
        "",
    ]
)

GOOD_ENTRY_POINTS = "\n".join(
    [
        "[console_scripts]",
        "openevo-backend = openevo.backend.launcher:main",
        "openevo-core-service = openevo.backend.service:main",
        "",
    ]
)


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/check_openevo_release.py"
    spec = importlib.util.spec_from_file_location("check_openevo_release", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_desktop_wheel_smoke_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/smoke_openevo_desktop_wheel.py"
    spec = importlib.util.spec_from_file_location("smoke_openevo_desktop_wheel", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_sha256_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/write_sha256.py"
    spec = importlib.util.spec_from_file_location("write_sha256", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_sidecar_smoke_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/smoke_openevo_desktop_sidecar.py"
    spec = importlib.util.spec_from_file_location("smoke_openevo_desktop_sidecar", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_bundle_smoke_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/smoke_openevo_desktop_bundle.py"
    spec = importlib.util.spec_from_file_location("smoke_openevo_desktop_bundle", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _process_is_live(pid: int) -> bool:
    if Path("/proc").is_dir():
        try:
            stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        except FileNotFoundError:
            return False
        return len(stat_fields) <= 2 or stat_fields[2] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _write_fake_native_with_independent_sidecar(
    app: Path,
    *,
    renderer_marker: str,
    sidecar_pid_path: Path,
    clean_sidecar: bool,
    exit_after_markers: bool = False,
) -> Path:
    executable = app / "Contents" / "MacOS" / "openevo-desktop"
    executable.parent.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": executable.name})
    )
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import ctypes\n"
        "import os\n"
        "from pathlib import Path\n"
        "import signal\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "child = subprocess.Popen([\"/bin/sh\", \"-c\", \"trap '' TERM; exec sleep 30\"], start_new_session=True)\n"
        f"Path({str(sidecar_pid_path)!r}).write_text(str(child.pid), encoding='utf-8')\n"
        "if sys.platform == 'darwin':\n"
        "    class ProcBsdInfo(ctypes.Structure):\n"
        "        _fields_ = [('pbi_flags', ctypes.c_uint32), ('pbi_status', ctypes.c_uint32), ('pbi_xstatus', ctypes.c_uint32), ('pbi_pid', ctypes.c_uint32), ('pbi_ppid', ctypes.c_uint32), ('pbi_uid', ctypes.c_uint32), ('pbi_gid', ctypes.c_uint32), ('pbi_ruid', ctypes.c_uint32), ('pbi_rgid', ctypes.c_uint32), ('pbi_svuid', ctypes.c_uint32), ('pbi_svgid', ctypes.c_uint32), ('rfu_1', ctypes.c_uint32), ('pbi_comm', ctypes.c_char * 16), ('pbi_name', ctypes.c_char * 32), ('pbi_nfiles', ctypes.c_uint32), ('pbi_pgid', ctypes.c_uint32), ('pbi_pjobc', ctypes.c_uint32), ('e_tdev', ctypes.c_uint32), ('e_tpgid', ctypes.c_uint32), ('pbi_nice', ctypes.c_int32), ('pbi_start_tvsec', ctypes.c_uint64), ('pbi_start_tvusec', ctypes.c_uint64)]\n"
        "    info = ProcBsdInfo()\n"
        "    proc_pidinfo = ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidinfo\n"
        "    proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]\n"
        "    proc_pidinfo.restype = ctypes.c_int\n"
        "    assert proc_pidinfo(child.pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) == ctypes.sizeof(info)\n"
        "    birth = f'darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}'\n"
        "else:\n"
        "    start_ticks = Path(f'/proc/{child.pid}/stat').read_text(encoding='utf-8').rsplit(')', 1)[1].split()[19]\n"
        "    birth = f'linux:{start_ticks}'\n"
        "print(f'OPENEVO_DESKTOP_SIDECAR_PROCESS_V1 {\"a\" * 32} {child.pid} {os.getpgid(child.pid)} {os.getsid(child.pid)} {birth}', flush=True)\n"
        f"print({renderer_marker!r}, flush=True)\n"
        + (
            "def stop(_signum, _frame):\n"
            "    try:\n"
            "        os.killpg(child.pid, signal.SIGKILL)\n"
            "    except ProcessLookupError:\n"
            "        pass\n"
            "    child.wait()\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            if clean_sidecar
            else ""
        )
        + ("raise SystemExit(7)\n" if exit_after_markers else "while True:\n    time.sleep(1)\n"),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _load_remote_capability_smoke_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/smoke_openevo_remote_capabilities.py"
    spec = importlib.util.spec_from_file_location("smoke_openevo_remote_capabilities", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_desktop_wheel_smoke_exercises_config_backed_lifecycle(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    smoke = _load_desktop_wheel_smoke_module()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    wheel.write_bytes(_nested_wheel_bytes(metadata=GOOD_METADATA))
    monkeypatch.setattr(
        "desktop.sidecar.api.discover_local_openevo_wheel",
        lambda: wheel,
    )

    assert smoke.main() == 0

    output = capsys.readouterr().out
    assert "Installed Core + source Desktop harness smoke passed" in output
    assert "Source Desktop config-backed lifecycle harness passed" in output


def test_write_sha256_writes_sibling_checksum(tmp_path: Path) -> None:
    writer = _load_sha256_module()
    artifact = tmp_path / "OpenEvo Desktop.dmg"
    artifact.write_bytes(b"desktop")

    checksum_path = writer.write_sha256(artifact)

    assert checksum_path.name == "OpenEvo Desktop.dmg.sha256"
    assert checksum_path.read_text(encoding="utf-8") == (
        f"{hashlib.sha256(b'desktop').hexdigest()}  OpenEvo Desktop.dmg\n"
    )


def test_sidecar_smoke_extracts_desktop_static_assets() -> None:
    smoke = _load_sidecar_smoke_module()

    assets = smoke._asset_references(
        '<html><head><link href="/assets/index.css"></head>'
        '<body><script src="assets/index.js"></script></body></html>'
    )

    assert assets == ["assets/index.css", "assets/index.js"]


@pytest.mark.parametrize(
    "startup_error",
    [
        TimeoutError("application has not accepted the inherited listener yet"),
        ConnectionResetError("listener changed ownership during startup"),
    ],
)
def test_sidecar_smoke_treats_startup_socket_error_as_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    startup_error: OSError,
) -> None:
    smoke = _load_sidecar_smoke_module()

    class _TimeoutOpener:
        def open(self, request, timeout):
            raise startup_error

    monkeypatch.setattr(smoke, "_LOCAL_HTTP_OPENER", _TimeoutOpener())

    with pytest.raises(smoke.SmokeFailure, match="was not reachable"):
        smoke._read_url("http://127.0.0.1:1/health")


def test_release_smokes_use_the_native_fd_and_five_key_frame_protocol() -> None:
    sidecar_smoke = Path("scripts/ci/smoke_openevo_desktop_sidecar.py").read_text(
        encoding="utf-8"
    )
    remote_smoke = Path("scripts/ci/smoke_openevo_remote_capabilities.py").read_text(
        encoding="utf-8"
    )
    bundle_smoke = Path("scripts/ci/smoke_openevo_desktop_bundle.py").read_text(
        encoding="utf-8"
    )

    assert 'NATIVE_LISTENER_FD_ENV = "OPENEVO_NATIVE_LISTENER_FD"' in sidecar_smoke
    assert 'NATIVE_ARCHIVE_FD_ENV = "OPENEVO_NATIVE_EXECUTABLE_FD"' in sidecar_smoke
    assert "pass_fds=(NATIVE_LISTENER_FD, NATIVE_ARCHIVE_FD)" in sidecar_smoke
    assert "process_log_guard = _duplicate_fd(process_log.fileno())" in sidecar_smoke
    assert "stdout=process_log_guard" in sidecar_smoke
    assert '"handoff_token": self.handoff_token' in sidecar_smoke
    assert '"--host"' not in sidecar_smoke
    assert '"--port"' not in sidecar_smoke
    assert "_smoke_capability_proxy" not in sidecar_smoke
    assert "X-OpenEvo-Sidecar-Token" not in sidecar_smoke
    assert '"--host"' not in remote_smoke
    assert '"--port"' not in remote_smoke
    assert "ensure_core_service(" in remote_smoke
    assert "stop_core_service(" in remote_smoke
    assert 'f"{base_url}/v1/capabilities' in remote_smoke
    assert '"Authorization": f"Bearer {attachment.bearer_token}"' in remote_smoke
    assert "sidecar_smoke.smoke_sidecar(" in remote_smoke
    assert "smoke_sidecar(sidecar" in bundle_smoke


def test_sidecar_smoke_rejects_core_owned_fields_in_project_contract() -> None:
    smoke = _load_sidecar_smoke_module()

    with pytest.raises(smoke.SmokeFailure, match="Core-owned field"):
        smoke._assert_project_method_contract(
            {
                "method_id": "reflect",
                "config_schema_json": json.dumps(
                    {
                        "type": "object",
                        "properties": {"reflector_llm": {"type": "object"}},
                        "additionalProperties": False,
                    }
                ),
                "default_config_json": "{}",
            }
        )


def test_sidecar_smoke_launches_process_and_checks_assets(tmp_path: Path) -> None:
    smoke = _load_sidecar_smoke_module()
    sidecar = tmp_path / "fake-openevo-desktop-sidecar"
    _write_fake_sidecar(sidecar)

    smoke.smoke_sidecar(sidecar, timeout_seconds=5)


def test_bundle_smoke_finds_and_launches_app_sidecar(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    sidecar = tmp_path / "OpenEvo Desktop.app" / "Contents" / "MacOS" / "openevo-desktop-sidecar"
    _write_fake_sidecar(sidecar)

    smoked_sidecar = smoke.smoke_bundle(tmp_path, timeout_seconds=5)

    assert smoked_sidecar == sidecar


def test_bundle_smoke_requires_openevo_desktop_app_bundle(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    other_sidecar = tmp_path / "Other.app" / "Contents" / "MacOS" / "openevo-desktop-sidecar"
    _write_fake_sidecar(other_sidecar)

    try:
        smoke.find_bundled_sidecar(tmp_path)
    except smoke.SmokeFailure as exc:
        assert "No OpenEvo Desktop.app bundle found" in str(exc)
    else:
        raise AssertionError("Expected missing OpenEvo Desktop.app bundle to fail")


def test_bundle_smoke_launches_the_native_app_until_renderer_is_ready(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    executable = _write_fake_native_with_independent_sidecar(
        app,
        renderer_marker=smoke.RENDERER_READY_MARKER,
        sidecar_pid_path=tmp_path / "sidecar.pid",
        clean_sidecar=True,
    )

    launched = smoke.smoke_native_app(app, timeout_seconds=5)

    assert launched == executable


def test_bundle_smoke_requires_instance_bound_sidecar_process_evidence(
    tmp_path: Path,
) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "openevo-desktop"
    executable.parent.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": executable.name})
    )
    executable.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{smoke.RENDERER_READY_MARKER}'\nsleep 30\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    with pytest.raises(smoke.SmokeFailure, match="sidecar process evidence"):
        smoke.smoke_native_app(app, timeout_seconds=1)


def test_bundle_smoke_proves_independent_sidecar_group_exit(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    sidecar_pid_path = tmp_path / "sidecar.pid"
    executable = _write_fake_native_with_independent_sidecar(
        app,
        renderer_marker=smoke.RENDERER_READY_MARKER,
        sidecar_pid_path=sidecar_pid_path,
        clean_sidecar=True,
    )

    assert smoke.smoke_native_app(app, timeout_seconds=2) == executable
    assert not _process_is_live(int(sidecar_pid_path.read_text(encoding="utf-8")))


def test_bundle_smoke_cleans_and_fails_for_surviving_setsid_sidecar(
    tmp_path: Path,
) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    sidecar_pid_path = tmp_path / "leaked-sidecar.pid"
    _write_fake_native_with_independent_sidecar(
        app,
        renderer_marker=smoke.RENDERER_READY_MARKER,
        sidecar_pid_path=sidecar_pid_path,
        clean_sidecar=False,
    )

    try:
        with pytest.raises(smoke.SmokeFailure, match="sidecar process group survived"):
            smoke.smoke_native_app(app, timeout_seconds=2)
    finally:
        if sidecar_pid_path.exists():
            sidecar_pid = int(sidecar_pid_path.read_text(encoding="utf-8"))
            try:
                os.killpg(sidecar_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    assert not _process_is_live(int(sidecar_pid_path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("outcome", ["timeout", "error"])
def test_bundle_smoke_cleans_independent_sidecar_on_readiness_failure(
    tmp_path: Path,
    outcome: str,
) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    sidecar_pid_path = tmp_path / f"{outcome}-sidecar.pid"
    _write_fake_native_with_independent_sidecar(
        app,
        renderer_marker="not-ready",
        sidecar_pid_path=sidecar_pid_path,
        clean_sidecar=True,
        exit_after_markers=outcome == "error",
    )

    with pytest.raises(smoke.SmokeFailure):
        smoke.smoke_native_app(app, timeout_seconds=0.5)
    assert not _process_is_live(int(sidecar_pid_path.read_text(encoding="utf-8")))


def test_bundle_smoke_reviewer_regression_kills_evidenced_setsid_sidecar_on_code7(
    tmp_path: Path,
) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    sidecar_pid_path = tmp_path / "reviewer-code7-sidecar.pid"
    _write_fake_native_with_independent_sidecar(
        app,
        renderer_marker="renderer-never-ready",
        sidecar_pid_path=sidecar_pid_path,
        clean_sidecar=True,
        exit_after_markers=True,
    )

    with pytest.raises(smoke.SmokeFailure):
        smoke.smoke_native_app(app, timeout_seconds=0.5)
    sidecar_pid = int(sidecar_pid_path.read_text(encoding="utf-8"))
    assert not _process_is_live(sidecar_pid)


def test_bundle_smoke_uses_the_original_bundle_path_on_darwin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "openevo-desktop"
    executable.parent.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": executable.name})
    )
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    canonical_executable = (
        Path(os.path.realpath(app.parent)) / app.name / "Contents" / "MacOS" / executable.name
    )
    with smoke._PinnedNativeExecutable.open(app) as pinned:
        monkeypatch.setattr(smoke.sys, "platform", "darwin")
        assert pinned.execution_path() == str(canonical_executable)


def test_bundle_smoke_canonicalizes_only_a_darwin_parent_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    canonical_parent = tmp_path / "canonical"
    app = canonical_parent / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "openevo-desktop"
    executable.parent.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": executable.name})
    )
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    alias = tmp_path / "alias"
    alias.symlink_to(canonical_parent, target_is_directory=True)
    monkeypatch.setattr(smoke.sys, "platform", "darwin")
    monkeypatch.setattr(smoke, "_validate_component", lambda *args, **kwargs: None)

    expected_app = Path(os.path.realpath(canonical_parent)) / app.name
    with smoke._PinnedNativeExecutable.open(alias / app.name) as pinned:
        assert pinned.app_bundle == expected_app
        assert pinned.execution_path() == str(
            expected_app / "Contents" / "MacOS" / executable.name
        )


@pytest.mark.parametrize(
    ("metadata", "kind"),
    [
        (SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=os.geteuid(), st_nlink=1), "directory"),
        (SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=os.geteuid() + 1, st_nlink=1), "executable"),
        (SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=os.geteuid(), st_nlink=2), "executable"),
    ],
)
def test_bundle_smoke_rejects_untrusted_darwin_components(
    metadata: SimpleNamespace,
    kind: str,
) -> None:
    smoke = _load_bundle_smoke_module()

    with pytest.raises(smoke.SmokeFailure, match="not trustworthy"):
        smoke._validate_darwin_component(metadata, kind=kind, path=Path("component"))


@pytest.mark.parametrize("component", ["app", "Contents", "MacOS"])
def test_bundle_smoke_rejects_symlinked_native_path_components(
    tmp_path: Path,
    component: str,
) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    external_contents = tmp_path / "external" / "Contents"
    executable = external_contents / "MacOS" / "openevo-desktop"
    executable.parent.mkdir(parents=True)
    (external_contents / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": executable.name})
    )
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    if component == "app":
        app.symlink_to(external_contents.parent, target_is_directory=True)
    elif component == "Contents":
        app.mkdir()
        (app / "Contents").symlink_to(external_contents, target_is_directory=True)
    else:
        contents = app / "Contents"
        contents.mkdir(parents=True)
        (contents / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleExecutable": executable.name})
        )
        (contents / "MacOS").symlink_to(
            external_contents / "MacOS", target_is_directory=True
        )

    with pytest.raises(smoke.SmokeFailure, match="native executable path"):
        smoke.find_native_executable(app)


@pytest.mark.parametrize("replacement", ["Contents", "MacOS", "leaf"])
def test_bundle_smoke_fails_closed_on_native_path_replacement_at_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "openevo-desktop"
    executable.parent.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": executable.name})
    )
    executable.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{smoke.RENDERER_READY_MARKER}'\nsleep 30\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    escaped_marker = tmp_path / f"{replacement.lower()}-escaped"
    external_contents = tmp_path / f"external-{replacement}" / "Contents"
    external_executable = external_contents / "MacOS" / executable.name
    external_executable.parent.mkdir(parents=True)
    (external_contents / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": external_executable.name})
    )
    external_executable.write_text(
        f"#!/bin/sh\ntouch '{escaped_marker}'\n"
        f"printf '%s\\n' '{smoke.RENDERER_READY_MARKER}'\nsleep 30\n",
        encoding="utf-8",
    )
    external_executable.chmod(0o755)
    real_popen = smoke.subprocess.Popen

    def replacing_popen(*args: object, **kwargs: object):
        if replacement == "Contents":
            contents = app / "Contents"
            contents.rename(app / "Contents.original")
            contents.symlink_to(external_contents, target_is_directory=True)
        elif replacement == "MacOS":
            macos = executable.parent
            macos.rename(macos.with_name("MacOS.original"))
            macos.symlink_to(external_executable.parent, target_is_directory=True)
        else:
            executable.unlink()
            executable.symlink_to(external_executable)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(smoke.subprocess, "Popen", replacing_popen)

    with pytest.raises(smoke.SmokeFailure, match="native executable"):
        smoke.smoke_native_app(app, timeout_seconds=1)
    assert not escaped_marker.exists()


def test_bundle_smoke_requires_an_exact_complete_renderer_marker_line(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "openevo-desktop"
    executable.parent.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": executable.name})
    )
    executable.write_text(
        f"#!/bin/sh\nprintf 'prefix%s-suffix\\n' '{smoke.RENDERER_READY_MARKER}'\nsleep 30\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    with pytest.raises(smoke.SmokeFailure, match="renderer readiness"):
        smoke.smoke_native_app(app, timeout_seconds=0.5)


def test_bundle_smoke_rejects_an_unterminated_renderer_marker_line(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "openevo-desktop"
    executable.parent.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": executable.name})
    )
    executable.write_text(
        f"#!/bin/sh\nprintf '%s' '{smoke.RENDERER_READY_MARKER}'\nsleep 30\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    with pytest.raises(smoke.SmokeFailure, match="renderer readiness"):
        smoke.smoke_native_app(app, timeout_seconds=0.5)


def test_bundle_smoke_keeps_an_early_marker_beyond_the_log_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "openevo-desktop"
    executable.parent.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": executable.name})
    )
    executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    executable.chmod(0o755)

    class FakeProcess:
        pid = 424242

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return None

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        output = kwargs["stdout"]
        output.write(
            (
                f"{smoke.SIDECAR_PROCESS_PREFIX} {'a' * 32} "
                "515151 515151 515151 linux:12345\n"
            ).encode()
        )
        output.write((smoke.RENDERER_READY_MARKER + "\n").encode())
        output.write(b"x" * (smoke.NATIVE_LOG_LIMIT + 1))
        output.flush()
        return FakeProcess()

    monkeypatch.setattr(smoke.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        smoke, "_validate_sidecar_process_evidence", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(smoke, "_verify_sidecar_process_group_exit", lambda evidence: None)
    monkeypatch.setattr(smoke, "_terminate_native_process", lambda process: None)
    monkeypatch.setattr(smoke.time, "sleep", lambda seconds: None)

    assert smoke.smoke_native_app(app, timeout_seconds=0.1) == executable


def test_bundle_smoke_rejects_a_native_process_without_renderer_readiness(
    tmp_path: Path,
) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "openevo-desktop"
    executable.parent.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": executable.name})
    )
    executable.write_text("#!/bin/sh\nprintf 'renderer failed\\n'\n", encoding="utf-8")
    executable.chmod(0o755)

    with pytest.raises(smoke.SmokeFailure, match="renderer readiness"):
        smoke.smoke_native_app(app, timeout_seconds=1)


def test_bundle_smoke_rejects_non_dictionary_info_plist(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    info = app / "Contents" / "Info.plist"
    info.parent.mkdir(parents=True)
    info.write_bytes(plistlib.dumps(["not", "a", "dictionary"]))

    with pytest.raises(smoke.SmokeFailure, match="top-level dictionary"):
        smoke.find_native_executable(app)


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_bundle_smoke_always_cleans_the_native_process_group(
    tmp_path: Path,
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "openevo-desktop"
    child_pid_path = tmp_path / f"{outcome}-child.pid"
    executable.parent.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": executable.name})
    )
    marker = smoke.RENDERER_READY_MARKER if outcome == "success" else "not-ready"
    final_command = "exit 7" if outcome == "failure" else "wait"
    executable.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "sh -c 'trap \"\" TERM; while :; do sleep 1; done' &\n"
        f"echo $! > '{child_pid_path}'\n"
        f"printf '%s\\n' '{smoke.SIDECAR_PROCESS_PREFIX} {'a' * 32} 515151 515151 515151 linux:12345'\n"
        f"printf '%s\\n' '{marker}'\n"
        f"{final_command}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setattr(
        smoke, "_validate_sidecar_process_evidence", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(smoke, "_verify_sidecar_process_group_exit", lambda evidence: None)

    if outcome == "success":
        assert smoke.smoke_native_app(app, timeout_seconds=1) == executable
    else:
        with pytest.raises(smoke.SmokeFailure):
            smoke.smoke_native_app(app, timeout_seconds=0.3)

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _process_is_live(child_pid):
        time.sleep(0.02)
    if _process_is_live(child_pid):
        try:
            os.killpg(os.getpgid(child_pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        pytest.fail(f"native smoke left child process {child_pid} running after {outcome}")


def test_accepts_valid_openevo_release_wheel(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert errors == []


def test_rejects_wheel_metadata_version_mismatch(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        metadata=GOOD_METADATA.replace("Version: 0.1.0", "Version: 0.2.0"),
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("METADATA Version should be `0.1.0`" in error for error in errors)


def test_requires_packaged_remote_install_wheel(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        include_nested_remote_wheel=False,
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("openevo/wheels/openevo-0.1.0-" in error for error in errors)


def test_validates_packaged_remote_install_wheel_metadata(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        nested_remote_wheel_metadata=GOOD_METADATA.replace(
            "Version: 0.1.0",
            "Version: 0.2.0",
        ),
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any(
        "Nested remote-install wheel METADATA Version should be `0.1.0`" in error
        for error in errors
    )


def test_requires_exact_openevo_wheel_artifact(tmp_path: Path) -> None:
    checker = _load_module()
    openevo_wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"not a real dmg; release list validation only checks presence")
    artifacts = [_write_release_notes(tmp_path)]

    assert checker.validate_release_artifacts(
        artifacts,
        expected_version="0.1.0",
    ) == [
        "Release artifacts must include an exact OpenEvo wheel for remote install: "
        "openevo-0.1.0-*.whl.",
        "Release artifacts must include an OpenEvo Desktop macOS .dmg.",
    ]
    assert checker.validate_release_artifacts(
        artifacts + [openevo_wheel, _write_checksum(openevo_wheel)],
        expected_version="0.1.0",
    ) == ["Release artifacts must include an OpenEvo Desktop macOS .dmg."]
    assert (
        checker.validate_release_artifacts(
            artifacts + [openevo_wheel, _write_checksum(openevo_wheel), dmg, _write_checksum(dmg)],
            expected_version="0.1.0",
        )
        == []
    )


def test_release_dmg_name_uses_canonical_hyphenated_format() -> None:
    checker = _load_module()

    assert checker._allowed_dmg_name(
        "OpenEvo-Desktop-0.1.0-aarch64.dmg",
        expected_version="0.1.0",
    )
    assert not checker._allowed_dmg_name(
        "OpenEvo Desktop_0.1.0_aarch64.dmg",
        expected_version="0.1.0",
    )


def test_release_artifact_list_rejects_unknown_files_and_non_openevo_wheels(
    tmp_path: Path,
) -> None:
    checker = _load_module()
    openevo_wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    polar_wheel = _write_wheel(tmp_path / "polar-0.1.0-py3-none-any.whl")
    dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"dmg bytes")
    debug_dmg = tmp_path / "debug.dmg"
    debug_dmg.write_bytes(b"debug dmg bytes")
    mislabeled_dmg = tmp_path / "OpenEvo-Desktop-0.1.0-debug.dmg"
    mislabeled_dmg.write_bytes(b"mislabeled dmg bytes")
    unexpected = tmp_path / "debug.log"
    unexpected.write_text("not a release artifact\n", encoding="utf-8")

    errors = checker.validate_release_artifacts(
        [
            openevo_wheel,
            _write_checksum(openevo_wheel),
            polar_wheel,
            _write_checksum(polar_wheel),
            dmg,
            _write_checksum(dmg),
            debug_dmg,
            _write_checksum(debug_dmg),
            mislabeled_dmg,
            _write_checksum(mislabeled_dmg),
            _write_release_notes(tmp_path),
            unexpected,
        ],
        expected_version="0.1.0",
    )

    assert "Unexpected release artifact: polar-0.1.0-py3-none-any.whl" in errors
    assert "Unexpected release artifact: polar-0.1.0-py3-none-any.whl.sha256" in errors
    assert "Unexpected release artifact: debug.dmg" in errors
    assert "Unexpected release artifact: debug.dmg.sha256" in errors
    assert "Unexpected release artifact: OpenEvo-Desktop-0.1.0-debug.dmg" in errors
    assert "Unexpected release artifact: OpenEvo-Desktop-0.1.0-debug.dmg.sha256" in errors
    assert "Unexpected release artifact: debug.log" in errors


def test_release_artifact_list_rejects_multiple_openevo_wheels(tmp_path: Path) -> None:
    checker = _load_module()
    py3_wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    cp311_wheel = _write_wheel(tmp_path / "openevo-0.1.0-cp311-cp311-macosx_14_0_arm64.whl")
    dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"dmg bytes")

    errors = checker.validate_release_artifacts(
        [
            py3_wheel,
            _write_checksum(py3_wheel),
            cp311_wheel,
            _write_checksum(cp311_wheel),
            dmg,
            _write_checksum(dmg),
            _write_release_notes(tmp_path),
        ],
        expected_version="0.1.0",
    )

    assert (
        "Release artifacts must include exactly one exact OpenEvo wheel for remote "
        "install, found: openevo-0.1.0-py3-none-any.whl, "
        "openevo-0.1.0-cp311-cp311-macosx_14_0_arm64.whl."
    ) in errors


def test_release_artifact_list_rejects_multiple_desktop_dmgs(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    arm_dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    x64_dmg = tmp_path / "OpenEvo-Desktop-0.1.0-x64.dmg"
    arm_dmg.write_bytes(b"arm dmg bytes")
    x64_dmg.write_bytes(b"x64 dmg bytes")

    errors = checker.validate_release_artifacts(
        [
            wheel,
            _write_checksum(wheel),
            arm_dmg,
            _write_checksum(arm_dmg),
            x64_dmg,
            _write_checksum(x64_dmg),
            _write_release_notes(tmp_path),
        ],
        expected_version="0.1.0",
    )

    assert (
        "Release artifacts must include exactly one OpenEvo Desktop macOS .dmg, "
        "found: OpenEvo-Desktop-0.1.0-aarch64.dmg, OpenEvo-Desktop-0.1.0-x64.dmg."
    ) in errors


def test_cli_wheel_only_requires_exact_openevo_wheel_artifact_name(
    tmp_path: Path,
    capsys,
) -> None:
    checker = _load_module()
    wheel = _write_wheel(tmp_path / "polar-0.1.0-py3-none-any.whl")

    result = checker.main(["--wheel", str(wheel)])

    assert result == 1
    assert (
        "Release artifacts must include an exact OpenEvo wheel for remote install: "
        "openevo-0.1.0-*.whl."
    ) in capsys.readouterr().err


def test_release_artifact_list_rejects_nonexistent_paths(tmp_path: Path) -> None:
    checker = _load_module()
    openevo_wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    missing_dmg = tmp_path / "release-artifacts" / "openevo-desktop-dmg" / "*.dmg"

    assert checker.validate_release_artifacts(
        [
            openevo_wheel,
            _write_checksum(openevo_wheel),
            _write_release_notes(tmp_path),
            missing_dmg,
        ],
        expected_version="0.1.0",
    ) == [
        f"Release artifact does not exist: {missing_dmg}",
        "Release artifacts must include an OpenEvo Desktop macOS .dmg.",
    ]


def test_release_artifact_list_requires_checksums_and_release_notes(tmp_path: Path) -> None:
    checker = _load_module()
    openevo_wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"dmg bytes")

    assert checker.validate_release_artifacts(
        [openevo_wheel, dmg],
        expected_version="0.1.0",
    ) == [
        "Release artifacts must include release-notes.md.",
        "Release artifact openevo-0.1.0-py3-none-any.whl must have a sibling "
        "openevo-0.1.0-py3-none-any.whl.sha256 checksum.",
        "Release artifact OpenEvo-Desktop-0.1.0-aarch64.dmg must have a sibling "
        "OpenEvo-Desktop-0.1.0-aarch64.dmg.sha256 checksum.",
    ]

    bad_checksum = tmp_path / f"{dmg.name}.sha256"
    bad_checksum.write_text("0" * 64 + "  wrong.dmg\n", encoding="utf-8")
    empty_notes = tmp_path / "release-notes.md"
    empty_notes.write_text("", encoding="utf-8")

    errors = checker.validate_release_artifacts(
        [
            openevo_wheel,
            _write_checksum(openevo_wheel),
            dmg,
            bad_checksum,
            empty_notes,
        ],
        expected_version="0.1.0",
    )

    assert f"{empty_notes} must contain non-empty OpenEvo release notes." in errors
    assert (f"{bad_checksum} should reference `{dmg.name}`, got `wrong.dmg`.") in errors


def test_release_artifact_checksums_must_be_siblings(tmp_path: Path) -> None:
    checker = _load_module()
    wheel_dir = tmp_path / "openevo-wheel"
    checksum_dir = tmp_path / "checksums"
    dmg_dir = tmp_path / "openevo-desktop-dmg"
    wheel_dir.mkdir()
    checksum_dir.mkdir()
    dmg_dir.mkdir()
    openevo_wheel = _write_wheel(wheel_dir / "openevo-0.1.0-py3-none-any.whl")
    misplaced_checksum = checksum_dir / f"{openevo_wheel.name}.sha256"
    misplaced_checksum.write_text(
        _write_checksum(openevo_wheel).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    openevo_wheel.with_name(f"{openevo_wheel.name}.sha256").unlink()
    dmg = dmg_dir / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"dmg bytes")

    errors = checker.validate_release_artifacts(
        [
            openevo_wheel,
            misplaced_checksum,
            dmg,
            _write_checksum(dmg),
            _write_release_notes(tmp_path),
        ],
        expected_version="0.1.0",
    )

    assert (
        "Release artifact openevo-0.1.0-py3-none-any.whl must have a sibling "
        "openevo-0.1.0-py3-none-any.whl.sha256 checksum."
    ) in errors
    assert (
        "Checksum artifact openevo-0.1.0-py3-none-any.whl.sha256 must have a sibling "
        "openevo-0.1.0-py3-none-any.whl artifact."
    ) in errors


def test_rejects_non_openevo_project_metadata(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "polar-0.1.0-py3-none-any.whl",
        metadata=GOOD_METADATA.replace("Name: openevo", "Name: polar"),
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("METADATA Name should be `openevo`" in error for error in errors)


def test_requires_expected_console_scripts(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        entry_points="\n".join(
            [
                "[console_scripts]",
                "openevo = openevo.cli:main",
                "",
            ]
        ),
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("openevo-backend = openevo.backend.launcher:main" in error for error in errors)
    assert any("openevo-core-service = openevo.backend.service:main" in error for error in errors)


def test_accepts_only_the_two_published_core_service_scripts(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")

    assert checker.validate_wheel(wheel, expected_version="0.1.0") == []


def test_rejects_unexpected_console_scripts(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        entry_points="\n".join(
            [
                "[console_scripts]",
                "openevo-backend = openevo.backend.launcher:main",
                "openevo = openevo.evolution.cli:main",
                "",
            ]
        ),
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("unexpected script(s): openevo" in error for error in errors)


def test_rejects_core_wheel_packaging_desktop_control_plane(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        extra_files={
            "openevo_terminal_bench/cli.py": "",
            "benchmarks/terminal_bench/README.md": "",
            "openevo/desktop/web/index.html": "<title>OpenEvo Desktop</title>",
            "openevo/sidecar/api.py": "",
            "openevo/cli.py": "",
            "desktop/server/app.py": "",
            "desktop/sidecar/api.py": "",
            "desktop/src/App.tsx": "",
            "desktop/src-tauri/tauri.conf.json": "",
            "desktop/packaging/web/index.html": "<title>OpenEvo Desktop</title>",
        },
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("openevo_terminal_bench/" in error for error in errors)
    assert any("benchmarks/terminal_bench/" in error for error in errors)
    assert any("openevo/desktop/" in error for error in errors)
    assert any("openevo/sidecar/" in error for error in errors)
    assert any("openevo/cli.py" in error for error in errors)
    assert any("desktop/server/" in error for error in errors)
    assert any("desktop/sidecar/" in error for error in errors)
    assert any("desktop/src/" in error for error in errors)
    assert any("desktop/src-tauri/" in error for error in errors)
    assert any("desktop/packaging/web/" in error for error in errors)


def test_rejects_removed_terminal_bench_modules_in_core_wheel(tmp_path: Path) -> None:
    checker = _load_module()
    legacy_modules = {
        "openevo/evolution/terminal_bench_bridge.py": "",
        "openevo/evolution/terminal_bench_local_parametric.py": "",
        "openevo/evolution/terminal_bench_per_task.py": "",
        "openevo/evolution/terminal_bench_task_local_parametric.py": "",
    }
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        extra_files=legacy_modules,
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    boundary_error = next(error for error in errors if "removed Terminal Bench modules" in error)
    assert all(path in boundary_error for path in legacy_modules)


def test_rejects_removed_terminal_bench_modules_in_nested_core_wheel(
    tmp_path: Path,
) -> None:
    checker = _load_module()
    legacy_path = "openevo/evolution/terminal_bench_per_task.py"
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        nested_remote_wheel_extra_files={legacy_path: ""},
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any(
        "openevo/wheels/openevo-0.1.0-py3-none-any.whl" in error and legacy_path in error
        for error in errors
    )


def test_core_wheel_boundary_allows_unrelated_similar_module_name(
    tmp_path: Path,
) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        extra_files={"openevo/evolution/terminal_bench_bridge_v2.py": ""},
    )

    assert checker.validate_wheel(wheel, expected_version="0.1.0") == []


def test_rejects_shared_dashboard_static_assets(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        extra_files={
            "openevo/platform/desktop/dist/index.html": ("<title>OpenEvo Observability</title>")
        },
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("openevo/platform/desktop/dist" in error for error in errors)


def test_local_version_validation_reads_top_level_desktop_metadata() -> None:
    checker = _load_module()

    root = Path(__file__).resolve().parents[2]
    paths = {
        path.relative_to(root).as_posix() for path in checker._desktop_package_metadata_paths()
    }

    assert "desktop/package.json" in paths
    assert "desktop/src-tauri/tauri.conf.json" in paths
    assert not any(path.startswith("web/") for path in paths)


def test_release_smoke_workflow_builds_packaged_assets_and_validates_wheel() -> None:
    workflow = Path(".github/workflows/openevo-release-smoke.yml")
    framework_smoke = Path("scripts/ci/smoke_evolution_framework_wheel.py")
    capability_smoke = Path("scripts/ci/smoke_openevo_remote_capabilities.py")
    desktop_smoke = Path("scripts/ci/smoke_openevo_desktop_wheel.py")

    text = workflow.read_text(encoding="utf-8")
    framework_smoke_text = framework_smoke.read_text(encoding="utf-8")
    capability_smoke_text = capability_smoke.read_text(encoding="utf-8")
    desktop_smoke_text = desktop_smoke.read_text(encoding="utf-8")

    assert text.startswith("name: OpenEvo packaged sidecar + installed Core release smoke")
    assert 'node-version: "22"' in text
    assert "npm test -- --run" in text
    assert "npm run typecheck" in text
    assert "npm audit --audit-level=high" in text
    assert "npm run build:openevo" in text
    assert "diff -qr desktop/dist desktop/packaging/web" in text
    assert '"src/slime_bridge/**"' in text
    assert '"desktop/**"' in text
    assert '- "scripts/ci/**"' in text
    assert '"tests/**"' in text
    assert "astral-sh/setup-uv@v6" in text
    assert "uv sync --frozen --group dev" in text
    assert "tests/ci/test_build_sidecar.py" in text
    assert "tests/ci/test_check_openevo_release.py" in text
    assert "name: Build and smoke packaged Desktop sidecar" in text
    assert "uv run python desktop/packaging/build_sidecar.py" in text
    assert "--core-wheel-output-dir .openevo-remote-wheel" in text
    assert "test -f .openevo-remote-wheel/framework-lock.json" in text
    assert (
        "find .openevo-remote-wheel -mindepth 1 -maxdepth 1 -type f | wc -l"
        in text
    )
    assert "scripts/ci/smoke_openevo_desktop_sidecar.py" in text
    assert "name: Build outer smoke wheel from isolated source" in text
    assert "python -m build --wheel --outdir .openevo-remote-wheel" not in text
    assert "rm -rf src/openevo/wheels" not in text
    assert "mkdir -p src/openevo/wheels" not in text
    assert 'mkdir -p "$outer_source/src/openevo/wheels"' in text
    assert 'src/ "$outer_source/src/"' in text
    assert "uv run python -m build --wheel --no-isolation" in text
    assert "scripts/ci/check_openevo_release.py --wheel dist/*.whl" in text
    assert "name: Smoke exact remote Core wheel" in text
    assert "python -m venv .openevo-remote-wheel-smoke" in text
    assert (
        ".openevo-remote-wheel-smoke/bin/python -m pip install .openevo-remote-wheel/*.whl"
    ) in text
    assert ".openevo-remote-wheel-smoke/bin/openevo-backend --help" in text
    assert ".openevo-remote-wheel-smoke/bin/openevo-backend serve --help" in text
    assert ".openevo-remote-wheel-smoke/bin/openevo-backend run --help" in text
    assert ".openevo-remote-wheel-smoke/bin/openevo-core-service --help" in text
    assert (
        "PYTHONPATH= .openevo-remote-wheel-smoke/bin/python "
        "scripts/ci/smoke_evolution_framework_wheel.py "
        "--wheel .openevo-remote-wheel/*.whl"
    ) in text
    assert (
        "PYTHONPATH= .openevo-remote-wheel-smoke/bin/python "
        "scripts/ci/smoke_openevo_remote_capabilities.py"
    ) in text
    assert "--wheel .openevo-remote-wheel/*.whl" in text
    assert '--sidecar "$sidecar"' in text
    assert '--source-commit "$(git rev-parse HEAD)"' in text
    assert (
        'sidecar="desktop/src-tauri/binaries/openevo-desktop-sidecar-$(rustc --print host-tuple)"'
    ) in text
    assert "ensure_core_service" in capability_smoke_text
    assert "stop_core_service" in capability_smoke_text
    assert 'parser.add_argument("--source-commit", required=True)' in capability_smoke_text
    assert '"--host"' not in capability_smoke_text
    assert '"--port"' not in capability_smoke_text
    assert "sidecar_smoke.smoke_sidecar" in capability_smoke_text
    assert "TestClient" not in capability_smoke_text
    assert "create_sidecar_app" not in capability_smoke_text
    assert "BackendConnection" not in capability_smoke_text
    assert "backend_client_factory" not in capability_smoke_text
    assert "subprocess.Popen" not in capability_smoke_text
    assert "name: Smoke installed Core with source Desktop harness" in text
    assert "python -m venv .openevo-wheel-smoke" in text
    assert ".openevo-wheel-smoke/bin/python -m pip install dist/*.whl" in text
    assert (
        "PYTHONPATH=. .openevo-wheel-smoke/bin/python scripts/ci/smoke_openevo_desktop_wheel.py"
    ) in text
    assert "source Desktop harness, not a packaged app" in desktop_smoke_text
    assert "EXPECTED_METHOD_IDS" in framework_smoke_text
    assert "EXPECTED_TARGET_IDS" in framework_smoke_text
    assert "EXPECTED_HANDLER_IDS" in framework_smoke_text
    assert framework_smoke_text.index("verified = verify_distribution_install(") < (
        framework_smoke_text.index("from openevo.evolution.framework import (")
    )
    assert "FrameworkDistributionLock" in framework_smoke_text
    assert "load_verified_framework_registry" in framework_smoke_text
    assert framework_smoke_text.index("FrameworkDistributionLock(") < (
        framework_smoke_text.index("loaded = load_verified_framework_registry(lock_path)")
    )

    assert text.index("npm ci") < text.index("npm test -- --run")
    assert text.index("npm ci") < text.index("npm audit --audit-level=high")
    assert text.index("npm audit --audit-level=high") < text.index("npm run build:openevo")
    assert text.index("npm test -- --run") < text.index("npm run build:openevo")
    assert text.index("npm run typecheck") < text.index("npm run build:openevo")
    assert text.index("name: Build and smoke packaged Desktop sidecar") < text.index(
        "name: Build outer smoke wheel from isolated source"
    )
    assert text.index("name: Build outer smoke wheel from isolated source") < text.index(
        "name: Validate OpenEvo wheel"
    )
    assert text.index("name: Validate OpenEvo wheel") < text.index(
        "name: Smoke exact remote Core wheel"
    )
    assert text.index("name: Smoke exact remote Core wheel") < text.index(
        "name: Smoke installed Core with source Desktop harness"
    )


def test_remote_capability_smoke_stops_core_when_ensure_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_remote_capability_smoke_module()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    framework_lock = tmp_path / "framework-lock.json"
    sidecar = tmp_path / "openevo-desktop-sidecar"
    for path in (wheel, framework_lock, sidecar):
        path.write_bytes(path.name.encode("ascii"))
    imported = tmp_path / "installed/openevo/__init__.py"
    imported.parent.mkdir(parents=True)
    imported.write_text("", encoding="utf-8")
    executable = tmp_path / "bin/python"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")
    executable.with_name("openevo-core-service").write_text("", encoding="utf-8")
    digest = "a" * 64

    class LockedIdentity:
        distribution_version = "0.1.0"
        distribution_digest = digest
        wheel_filename = wheel.name

    class SidecarSmoke:
        @staticmethod
        def smoke_sidecar(path: Path, *, timeout_seconds: float) -> None:
            assert path == sidecar
            assert timeout_seconds == 1.0

    calls: list[tuple[str, ...]] = []

    def run_core_service(_executable, *arguments, **kwargs):
        del kwargs
        calls.append(arguments)
        if arguments[0] == "ensure":
            raise RuntimeError("injected ensure failure")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    import openevo.backend.runtime_identity as runtime_identity
    import openevo.evolution.framework as framework

    monkeypatch.setattr(smoke.openevo, "__file__", str(imported))
    monkeypatch.setattr(smoke.metadata, "version", lambda _: "0.1.0")
    monkeypatch.setattr(smoke, "_sha256", lambda _: digest)
    monkeypatch.setattr(
        framework,
        "load_framework_distribution_lock",
        lambda _: (LockedIdentity(), wheel.resolve()),
    )
    monkeypatch.setattr(smoke, "_load_sidecar_smoke", lambda: SidecarSmoke())
    monkeypatch.setattr(runtime_identity, "default_core_service_root", lambda: tmp_path / "core")
    monkeypatch.setattr(smoke.sys, "executable", str(executable))
    monkeypatch.setattr(smoke, "_run_core_service", run_core_service)

    with pytest.raises(RuntimeError, match="injected ensure failure"):
        smoke.smoke(
            wheel,
            framework_lock,
            sidecar,
            source_commit="b" * 40,
            timeout_seconds=1.0,
        )

    assert calls[0][0] == "ensure"
    assert calls[-1][0] == "stop"


def test_pre_external_beta_release_artifact_workflow_is_disabled() -> None:
    workflow = Path(".github/workflows/openevo-release-artifact.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "pre-external-beta release artifact path disabled" in text
    assert "build," in text
    assert "redownload, and verify the exact Core and DMG" in text
    assert "docs/maintainer/productization/spec.md" in text
    assert "tags:" not in text
    assert '"v*"' not in text
    assert "actions/upload-artifact@v4" not in text
    assert "python -m build --wheel" not in text
    assert "npm run build:desktop" not in text
    assert "desktop-dmg-artifact:" not in text


def test_desktop_candidate_workflow_builds_and_smokes_unsigned_dmg_without_publishing() -> None:
    workflow = Path(".github/workflows/openevo-desktop-candidate.yml")

    text = workflow.read_text(encoding="utf-8")

    for marker in (
        "workflow_dispatch:",
        "runs-on: macos-14",
        "timeout-minutes:",
        "uv sync --frozen --group dev",
        "tests/ci/test_build_sidecar.py",
        "scripts/ci/audit_openevo_identity.py",
        "npm ci",
        "npm audit --audit-level=high",
        "npm test -- --run",
        "npm run typecheck",
        "packaging/build_sidecar.py",
        "--core-wheel-output-dir",
        "framework-lock.json",
        "smoke_openevo_desktop_sidecar.py",
        "cargo fmt --check",
        "cargo clippy --locked --release --all-targets -- -D warnings",
        "cargo test --locked --release",
        "npm run tauri:build -- --ci",
        "hdiutil attach",
        "smoke_openevo_desktop_bundle.py",
        "--native-app",
        "scripts/ci/write_sha256.py",
        "scripts/ci/check_openevo_release.py",
        "actions/upload-artifact@v4",
        "retention-days: 14",
        "unsigned and not notarized",
    ):
        assert marker in text
    assert "smoke_openevo_remote_capabilities.py" not in text

    assert text.index("npm ci") < text.index("npm run tauri:build -- --ci")
    assert text.index("hdiutil attach") < text.index("actions/upload-artifact@v4")
    assert "contents: write" not in text
    assert "gh release" not in text
    assert "softprops/action-gh-release" not in text
    assert "tags:" not in text

    desktop_checks = Path(".github/workflows/openevo-desktop.yml").read_text(encoding="utf-8")
    assert '".github/workflows/openevo-desktop-candidate.yml"' in desktop_checks


def test_desktop_package_defines_tauri_desktop_scripts_and_cli_dependency() -> None:
    package = json.loads(Path("desktop/package.json").read_text(encoding="utf-8"))

    assert package["name"] == "openevo-desktop"
    assert package["scripts"]["dev:openevo"] == "vite --mode openevo-desktop"
    assert package["scripts"]["tauri:dev"] == "tauri dev"
    assert package["scripts"]["tauri:build"] == "tauri build"
    assert package["scripts"]["build:sidecar"] == "python packaging/build_sidecar.py"
    assert package["scripts"]["build:desktop"] == ("npm run build:sidecar && npm run tauri:build")
    assert "@tauri-apps/cli" in package["devDependencies"]


def test_desktop_tailwind_sources_are_explicit_and_exclude_packaged_web() -> None:
    styles = Path("desktop/src/styles.css").read_text(encoding="utf-8")

    assert '@import "tailwindcss" source(none);' in styles
    assert '@source "../index.html";' in styles
    assert '@source "./**/*.{ts,tsx}";' in styles
    assert "packaging/web" not in styles


def test_tauri_macos_config_declares_unreleased_dmg_target() -> None:
    config = json.loads(Path("desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    cargo = Path("desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    main = Path("desktop/src-tauri/src/main.rs").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/openevo-desktop.yml").read_text(encoding="utf-8")
    sidecar_builder = Path("desktop/packaging/build_sidecar.py")
    sidecar_entry = Path("desktop/packaging/sidecar_entry.py")
    gitignore = Path(".gitignore").read_text(encoding="utf-8")

    assert config["productName"] == "OpenEvo Desktop"
    assert config["version"] == "0.1.0"
    assert config["identifier"] == "org.openevo.desktop"
    assert config["build"]["beforeBuildCommand"] == "npm run build:openevo"
    assert config["build"]["beforeDevCommand"] == "npm run dev:openevo"
    assert config["build"]["frontendDist"] == "../dist"
    assert config["bundle"]["active"] is True
    assert config["bundle"]["targets"] == ["dmg"]
    assert config["bundle"]["externalBin"] == ["binaries/openevo-desktop-sidecar"]
    assert config["bundle"]["macOS"]["minimumSystemVersion"] == "12.0"
    assert sidecar_builder.is_file()
    assert sidecar_entry.is_file()
    assert "desktop/src-tauri/binaries/openevo-desktop-sidecar-*" in gitignore
    sidecar_builder_text = sidecar_builder.read_text(encoding="utf-8")
    sidecar_entry_text = sidecar_entry.read_text(encoding="utf-8")
    assert "PyInstaller" in sidecar_builder_text
    assert "_build_core_wheel" in sidecar_builder_text
    assert "_validate_embedded_core_wheel" in sidecar_builder_text
    assert "_write_core_framework_lock" in sidecar_builder_text
    assert "_validate_embedded_core_framework_lock" in sidecar_builder_text
    assert "--add-data" in sidecar_builder_text
    assert "desktop/packaging/web" in sidecar_builder_text
    assert "sidecar-build-metadata.json" in sidecar_builder_text
    assert '"rev-parse", "--verify", "HEAD^{commit}"' in sidecar_builder_text
    assert "_write_sidecar_build_metadata" in sidecar_builder_text
    assert "desktop.server.launcher" in sidecar_entry_text
    assert "_load_packaged_build_metadata" in sidecar_entry_text
    assert "os.close(4)" not in sidecar_entry_text
    assert 'name = "openevo-desktop"' in cargo
    assert 'serde = { version = "1", features = ["derive"] }' in cargo
    assert "tauri = " in cargo
    assert "struct ManagedSidecar" in main
    assert "struct DesktopHostState" in main
    assert "fn allocate_sidecar_listener()" in main
    assert "fn prepare_packaged_sidecar(" in main
    assert "libc::O_NOFOLLOW" in main
    assert "acl_get_fd_np" in main
    assert "struct SpawnHandoff" in main
    assert "run_parent_liveness_watchdog" in main
    assert "libc::WNOWAIT" in main
    assert "GroupSignalAuthority::Finalizing" in main
    assert main.count("const DESKTOP_LOCAL_API_OPENAPI_SHA256") == 1
    assert "3a86582d04dcd233096337c737ba91d75854746848aedc319025d86213a03d36" in main
    assert "fn macos_proc_listpgrppids_call(" in main
    assert "fn sanitize_pyinstaller_launch_environment(" in main
    assert 'command.env(PYINSTALLER_RESET_ENVIRONMENT, "1")' in main
    assert "fn monitor_running_sidecar(" in main
    assert "launch_gate" not in main
    assert "emergency_process_group" not in main
    assert "fn terminate_process_group(" in main
    assert "openevo-desktop-sidecar" in main
    assert "check_sidecar_health" in main
    assert "wait_for_sidecar_ready" in main
    assert "fn host_status(" in main
    assert "fn start_sidecar(" in main
    assert "fn stop_sidecar(" in main
    assert "fn create_ssh_tunnel(" not in main
    assert "fn keychain_reference(" not in main
    assert "fn app_logs(" not in main
    assert "desktop.server.launcher" in main
    assert "Command::new" in main
    assert "Stdio::null()" in main
    assert "tauri::generate_handler!" in main
    assert "tauri::RunEvent::ExitRequested" in main
    assert "cargo check --locked --release --all-targets" in workflow
    assert "cargo clippy --locked --release --all-targets -- -D warnings" in workflow
    assert "cargo test --locked --release" in workflow
    assert workflow.index("npm run build:sidecar") < workflow.index(
        "tests::packaged_external_bin_native_launch_smoke"
    )
    assert "macOS FD-bound packaged sidecar launch smoke" in workflow
    assert "tests::macos_release_uses_private_path_and_keeps_the_verified_fd" in workflow
    assert "if: always()" in workflow
    assert 'rm -f "$OPENEVO_PACKAGED_SIDECAR_PATH"' in workflow
    assert "cargo build --locked --release" in workflow
    assert "release binary contains the debug source launcher fallback" in workflow
    assert "release binary contains debug sidecar override code" in workflow


def test_sidecar_bootloader_separates_verified_archive_fd_from_macos_exec_path(
    tmp_path: Path,
) -> None:
    builder = Path("desktop/packaging/build_sidecar.py").read_text(encoding="utf-8")

    assert 'NATIVE_LISTENER_FD_ENV = "OPENEVO_NATIVE_LISTENER_FD"' in builder
    assert 'NATIVE_EXECUTABLE_FD_ENV = "OPENEVO_NATIVE_EXECUTABLE_FD"' in builder
    assert 'NATIVE_EXECUTABLE_PATH_ENV = "OPENEVO_NATIVE_EXECUTABLE_PATH"' in builder
    assert "/dev/fd/{NATIVE_EXECUTABLE_FD}" in builder
    assert "pyi_ctx->archive = pyi_archive_open(openevo_archive_path);" in builder
    assert "snprintf(pyi_ctx->executable_filename, PYI_PATH_MAX" in builder
    assert "realpath(openevo_native_path, openevo_resolved_path)" in builder
    assert "fstat({NATIVE_EXECUTABLE_FD}, &openevo_fd_stat)" in builder
    assert "lstat(openevo_native_path, &openevo_path_stat)" in builder
    assert "openevo_fd_stat.st_ino != openevo_path_stat.st_ino" in builder
    assert "openevo_path_stat.st_nlink != 1" in builder
    assert "openevo_path_stat.st_uid != geteuid()" in builder
    assert "NATIVE_EXECUTABLE_PATH_ENV.encode" in builder

    path = Path("desktop/packaging/build_sidecar.py").resolve()
    spec = importlib.util.spec_from_file_location("openevo_sidecar_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "bootloader/src/pyi_main.c"
    source.parent.mkdir(parents=True)
    source.write_text(
        module._BOOTLOADER_MACOS_INCLUDE_NEEDLE
        + module._BOOTLOADER_RESOLVER_NEEDLE
        + module._BOOTLOADER_ARCHIVE_NEEDLE
        + module._BOOTLOADER_RESTART_NEEDLE
        + module._BOOTLOADER_CHILD_MAIN_NEEDLE,
        encoding="utf-8",
    )
    utils_source = tmp_path / "bootloader/src/pyi_utils_posix.c"
    utils_source.write_text(
        module._BOOTLOADER_POSIX_INCLUDE_NEEDLE
        + module._BOOTLOADER_NATIVE_HANDOFF_NEEDLE
        + module._BOOTLOADER_CHILD_EXEC_NEEDLE,
        encoding="utf-8",
    )
    utils_header = tmp_path / "bootloader/src/pyi_utils.h"
    utils_header.write_text(module._BOOTLOADER_UTILS_HEADER_NEEDLE, encoding="utf-8")

    module._patch_fd_bound_bootloader(tmp_path)

    patched = source.read_text(encoding="utf-8")
    patched_utils = utils_source.read_text(encoding="utf-8")
    assert patched.count('getenv("OPENEVO_NATIVE_EXECUTABLE_PATH")') == 1
    assert patched.count('getenv("OPENEVO_NATIVE_LISTENER_FD")') == 1
    assert patched.count("pyi_archive_open(openevo_archive_path)") == 1
    assert patched.count("fstat(4, &openevo_fd_stat)") == 1
    assert patched.count("lstat(openevo_native_path, &openevo_path_stat)") == 1
    assert patched.count("lstat(openevo_resolved_path, &openevo_resolved_stat)") == 1
    assert "SO_ACCEPTCONN" in patched_utils
    assert "pyi_utils_openevo_native_handoff_restore()" in patched_utils


def test_pre_external_beta_pypi_publish_workflow_is_disabled() -> None:
    workflow = Path(".github/workflows/openevo-publish-pypi.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "contents: read" in text
    assert "name: PyPI publishing disabled" in text
    assert "PyPI is not an External Beta release surface" in text
    assert "Any future PyPI release requires a separate product" in text
    assert "completing the External" in text
    assert "Beta gates must not enable publication here" in text
    assert "release:" not in text
    assert "types: [published]" not in text
    assert "id-token: write" not in text
    assert "name: pypi" not in text
    assert "python -m build --wheel" not in text
    assert "twine check --strict dist/*.whl" not in text
    assert "pypa/gh-action-pypi-publish@release/v1" not in text
    assert "password:" not in text.casefold()
    assert "api-token" not in text.casefold()


def test_disabled_release_artifact_workflow_does_not_upload_checksums_or_notes() -> None:
    workflow = Path(".github/workflows/openevo-release-artifact.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "pre-External-Beta release artifact workflow is disabled" in text
    assert "name: Write release notes" not in text
    assert "actions/upload-artifact@v4" not in text
    assert "openevo-release-notes" not in text
    assert "release-artifacts/openevo-wheel/*" not in text
    assert "release-artifacts/openevo-desktop-dmg/*" not in text


def test_desktop_science_release_doc_matches_remote_lifecycle_state() -> None:
    doc = Path("docs/architecture/openevo-desktop-science-foundation.md")

    text = doc.read_text(encoding="utf-8")

    assert "a remote backend implementation" not in text
    assert "sidecar process supervision" not in text
    assert "remote workspace preparation" in text
    assert "`POST /openevo-api/desktop/bootstrap`" in text
    assert "`POST /openevo-api/desktop/services`" in text
    assert "`POST /openevo-api/desktop/run`" in text
    assert "GET /openevo-api/backend/runs/{run_id}/timeline" in text
    assert "GET /openevo-api/backend/runs/{run_id}/artifacts" in text
    assert "GET /openevo-api/backend/artifacts/{artifact_id}/content" in text


def test_readme_release_checklist_matches_frontend_audit_gate() -> None:
    readme = Path("README.md")

    text = readme.read_text(encoding="utf-8")
    smoke_section = text[text.index("## Pre-External-Beta Release Smoke") :]

    assert "npm ci" in text
    assert "npm audit --audit-level=high" in text
    assert text.index("npm ci") < text.index("npm audit --audit-level=high")
    assert text.index("npm audit --audit-level=high") < text.index("npm test -- --run")
    assert "npm run typecheck" in text
    assert smoke_section.startswith("## Pre-External-Beta Release Smoke")
    assert "maintainer-only" in smoke_section
    assert "GitHub Release" in smoke_section
    assert "PyPI" in smoke_section
    assert "docs/maintainer/productization/spec.md" in smoke_section
    assert "scripts/ci/smoke_openevo_desktop_wheel.py" not in smoke_section
    assert ".openevo-wheel-smoke/bin/openevo-backend run --help" not in smoke_section
    assert "PyPI trusted publishing" not in text
    assert "pypa/gh-action-pypi-publish@release/v1" not in text


def _write_wheel(
    path: Path,
    *,
    metadata: str = GOOD_METADATA,
    entry_points: str = GOOD_ENTRY_POINTS,
    include_nested_remote_wheel: bool = True,
    nested_remote_wheel_metadata: str = GOOD_METADATA,
    nested_remote_wheel_extra_files: dict[str, str] | None = None,
    extra_files: dict[str, str] | None = None,
) -> Path:
    dist_info = "openevo-0.1.0.dist-info"
    with ZipFile(path, "w") as wheel:
        wheel.writestr(f"{dist_info}/METADATA", metadata)
        wheel.writestr(f"{dist_info}/entry_points.txt", entry_points)
        if include_nested_remote_wheel:
            wheel.writestr(
                "openevo/wheels/openevo-0.1.0-py3-none-any.whl",
                _nested_wheel_bytes(
                    metadata=nested_remote_wheel_metadata,
                    extra_files=nested_remote_wheel_extra_files,
                ),
            )
        for name, content in (extra_files or {}).items():
            wheel.writestr(name, content)
    return path


def _nested_wheel_bytes(
    *,
    metadata: str,
    extra_files: dict[str, str] | None = None,
) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as wheel:
        wheel.writestr("openevo-0.1.0.dist-info/METADATA", metadata)
        for name, content in (extra_files or {}).items():
            wheel.writestr(name, content)
    return buffer.getvalue()


def _write_checksum(path: Path) -> Path:
    checksum_path = path.with_name(f"{path.name}.sha256")
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    checksum_path.write_text(f"{checksum}  {path.name}\n", encoding="utf-8")
    return checksum_path


def _write_release_notes(directory: Path) -> Path:
    notes = directory / "release-notes.md"
    notes.write_text("# OpenEvo 0.1.0\n\nRelease smoke notes.\n", encoding="utf-8")
    return notes


def _write_fake_sidecar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from http.server import BaseHTTPRequestHandler, HTTPServer",
                "import argparse",
                "import hashlib",
                "import hmac",
                "import json",
                "import socket",
                "import sys",
                "frame = json.loads(sys.stdin.readline())",
                "assert set(frame) == {'protocol', 'instance_id', 'readiness_key', 'session_token', 'handoff_token'}",
                "assert frame['protocol'] == 'openevo-native-sidecar-v1'",
                "",
                "class Handler(BaseHTTPRequestHandler):",
                "    def do_GET(self):",
                "        if self.path == '/health':",
                "            challenge = self.headers.get('X-OpenEvo-Native-Challenge', '')",
                "            domain = f\"{frame['protocol']}\\0{frame['instance_id']}\\0{challenge}\".encode()",
                "            proof = hmac.new(bytes.fromhex(frame['readiness_key']), domain, hashlib.sha256).hexdigest()",
                "            body = json.dumps({'service': 'openevo-sidecar', 'status': 'ok', 'protocol': frame['protocol'], 'instance_id': frame['instance_id'], 'instance_proof': proof}).encode()",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'application/json')",
                "            self.send_header('Content-Length', str(len(body)))",
                "            self.end_headers()",
                "            self.wfile.write(body)",
                "            return",
                "        if self.path == '/version':",
                "            body = json.dumps({",
                "                'schema_version': '1',",
                "                'api_name': 'openevo-desktop-local-api',",
                "                'preferred_major': 1,",
                "                'supported_majors': [1],",
                "                'openapi_sha256': 'a' * 64,",
                "                'build_version': '0.1.0',",
                "                'source_commit': '89baeb26',",
                "                'build_channel': 'release',",
                "                'provider_kind': 'desktop_sidecar',",
                "                'feature_flags': [],",
                "            }).encode()",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'application/json')",
                "            self.send_header('Content-Length', str(len(body)))",
                "            self.end_headers()",
                "            self.wfile.write(body)",
                "            return",
                "        if self.path == '/desktop/v1/state':",
                "            if self.headers.get('X-OpenEvo-Desktop-Session') != frame['session_token']:",
                "                self.send_response(401)",
                "                self.end_headers()",
                "                return",
                "            body = json.dumps({'schema_version': '1'}).encode()",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'application/json')",
                "            self.send_header('Content-Length', str(len(body)))",
                "            self.end_headers()",
                "            self.wfile.write(body)",
                "            return",
                "        if self.path == '/openevo-native/session':",
                "            status = 204 if self.headers.get('X-OpenEvo-Desktop-Session') == frame['session_token'] else 403",
                "            self.send_response(status)",
                "            self.end_headers()",
                "            return",
                "        if self.path == '/openevo':",
                "            body = b'<script src=\"/assets/index.js\"></script>'",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'text/html')",
                "            self.send_header('Content-Length', str(len(body)))",
                "            self.end_headers()",
                "            self.wfile.write(body)",
                "            return",
                "        if self.path == '/assets/index.js':",
                "            body = b'console.log(\"openevo\")'",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'text/javascript')",
                "            self.send_header('Content-Length', str(len(body)))",
                "            self.end_headers()",
                "            self.wfile.write(body)",
                "            return",
                "        self.send_response(404)",
                "        self.end_headers()",
                "",
                "    def log_message(self, format, *args):",
                "        return",
                "",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--listener-fd', type=int, required=True)",
                "parser.add_argument('--native-instance-stdin', action='store_true', required=True)",
                "parser.add_argument('--desktop-config-root')",
                "args = parser.parse_args()",
                "server = HTTPServer(('127.0.0.1', 0), Handler, bind_and_activate=False)",
                "server.socket.close()",
                "server.socket = socket.socket(fileno=args.listener_fd)",
                "server.server_address = server.socket.getsockname()",
                "server.serve_forever()",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
