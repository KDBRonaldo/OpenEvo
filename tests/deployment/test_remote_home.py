from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
import os
from pathlib import Path
import pickle
import shlex
import subprocess
import sys

import pytest

from openevo.deployment.remote_home import (
    REMOTE_HOME_PROBE_OUTPUT_LIMIT,
    RemoteHomeAuthority,
    RemoteHomeAuthorityError,
    build_remote_home_guarded_command,
    build_remote_home_guarded_rsync_path,
    build_remote_home_probe_command,
    parse_remote_home_probe,
)
from openevo.deployment.system_executables import RSYNC_EXECUTABLE


def _record(
    *,
    id_user: str = "researcher",
    id_uid: str = "1001",
    nss_user: str | None = None,
    nss_uid: str | None = None,
    home: str = "/srv/research/alice",
    physical_home: str | None = None,
    owner_uid: str | None = None,
    writable: str = "1",
) -> bytes:
    return (
        "openevo-remote-home-v1\n"
        f"{id_user}\n"
        f"{id_uid}\n"
        f"{nss_user if nss_user is not None else id_user}\n"
        f"{nss_uid if nss_uid is not None else id_uid}\n"
        f"{home}\n"
        f"{physical_home if physical_home is not None else home}\n"
        f"{owner_uid if owner_uid is not None else id_uid}\n"
        f"{writable}\n"
    ).encode("utf-8")


def _authority(
    *,
    profile_id: str = "profile-1",
    connection_generation: int = 7,
    user: str = "researcher",
    uid: int = 1001,
    home: str = "/srv/research/alice",
) -> RemoteHomeAuthority:
    return parse_remote_home_probe(
        profile_id=profile_id,
        connection_generation=connection_generation,
        return_code=0,
        stdout=_record(id_user=user, id_uid=str(uid), home=home),
        stderr=b"",
    )


@pytest.mark.parametrize(
    ("user", "uid", "home"),
    [
        ("root", 0, "/root"),
        ("researcher", 1001, "/home/researcher"),
        ("researcher", 1001, "/srv/research/alice"),
    ],
)
def test_verified_probe_seals_exact_derived_roots(
    user: str,
    uid: int,
    home: str,
) -> None:
    authority = _authority(user=user, uid=uid, home=home)

    assert authority.profile_id == "profile-1"
    assert authority.connection_generation == 7
    assert authority.remote_user == user
    assert authority.uid == uid
    assert authority.workspace_root == f"{home}/.openevo/workspaces"
    assert authority.daemon_bundle_root == f"{home}/.openevo/daemon-bundles"


def test_authority_requires_the_private_seal_and_is_immutable() -> None:
    with pytest.raises(TypeError):
        RemoteHomeAuthority(  # type: ignore[call-arg]
            profile_id="profile-1",
            connection_generation=7,
            remote_user="researcher",
            uid=1001,
            _home="/srv/research/alice",
        )
    with pytest.raises((TypeError, RemoteHomeAuthorityError)):
        RemoteHomeAuthority(
            profile_id="profile-1",
            connection_generation=7,
            remote_user="researcher",
            uid=1001,
            _home="/srv/research/alice",
            _seal=object(),
        )

    authority = _authority()
    with pytest.raises(FrozenInstanceError):
        authority.uid = 1002  # type: ignore[misc]


def test_authority_cannot_be_forged_by_dataclass_replace_or_serialized() -> None:
    authority = _authority()

    with pytest.raises((TypeError, ValueError, RemoteHomeAuthorityError)):
        replace(authority, _home="/srv/research/forged")
    with pytest.raises(TypeError, match="Remote account authority cannot be serialized"):
        pickle.dumps(authority)


def test_authority_binding_is_exact_and_errors_are_private() -> None:
    authority = _authority()

    assert authority.matches(
        profile_id="profile-1",
        connection_generation=7,
        remote_user="researcher",
    )
    assert not authority.matches(
        profile_id="profile-1",
        connection_generation=8,
        remote_user="researcher",
    )
    authority.require_binding(
        profile_id="profile-1",
        connection_generation=7,
        remote_user="researcher",
        workspace_root="/srv/research/alice/.openevo/workspaces",
    )

    with pytest.raises(RemoteHomeAuthorityError) as captured:
        authority.require_binding(
            profile_id="profile-1",
            connection_generation=7,
            remote_user="researcher",
            workspace_root="/home/researcher/.openevo/workspaces",
        )
    rendered = f"{authority!r} {captured.value!r} {captured.value}"
    assert rendered == (
        "RemoteHomeAuthority(<sealed>) "
        "RemoteHomeAuthorityError('Remote account authority binding is invalid.') "
        "Remote account authority binding is invalid."
    )
    assert "/srv/research/alice" not in rendered
    assert "/home/researcher" not in rendered


@pytest.mark.parametrize(
    "kwargs",
    [
        {"return_code": 1},
        {"stderr": b"private diagnostic"},
        {"stdout": b""},
        {"stdout": _record()[:-1]},
        {"stdout": _record() + b"extra\n"},
        {"stdout": _record().replace(b"researcher\n", b"researcher\x00\n", 1)},
        {"stdout": _record().replace(b"researcher\n", b"researcher\r\n", 1)},
        {"stdout": b"\xff\n"},
        {"stdout": b"x" * (REMOTE_HOME_PROBE_OUTPUT_LIMIT + 1)},
    ],
)
def test_probe_rejects_invalid_process_results_without_echoing_them(
    kwargs: dict[str, object],
) -> None:
    arguments: dict[str, object] = {
        "profile_id": "profile-1",
        "connection_generation": 7,
        "return_code": 0,
        "stdout": _record(),
        "stderr": b"",
    }
    arguments.update(kwargs)

    with pytest.raises(RemoteHomeAuthorityError) as captured:
        parse_remote_home_probe(**arguments)  # type: ignore[arg-type]

    assert str(captured.value) == "Remote account home probe is invalid."
    assert captured.value.__cause__ is None
    assert "private diagnostic" not in repr(captured.value)


@pytest.mark.parametrize(
    "record",
    [
        _record(id_user="-researcher"),
        _record(id_user="researcher!"),
        _record(id_uid="-1"),
        _record(id_uid="+1"),
        _record(id_uid="01"),
        _record(id_uid="4294967295"),
        _record(nss_user="another"),
        _record(nss_uid="1002"),
        _record(owner_uid="1002"),
        _record(writable="0"),
        _record(writable="true"),
    ],
)
def test_probe_rejects_account_identity_mismatch(record: bytes) -> None:
    with pytest.raises(RemoteHomeAuthorityError, match="Remote account home probe is invalid"):
        parse_remote_home_probe(
            profile_id="profile-1",
            connection_generation=7,
            return_code=0,
            stdout=record,
            stderr=b"",
        )


@pytest.mark.parametrize(
    "home",
    [
        "",
        ".",
        "relative/home",
        "/",
        "/srv/./alice",
        "/srv/../alice",
        "/srv//alice",
        "/srv/alice/",
        "/srv/research alice",
        "/srv/research:alice",
        "/srv/research$alice",
        "/" + "a" * 4096,
    ],
)
def test_probe_rejects_unsafe_or_noncanonical_home(home: str) -> None:
    with pytest.raises(RemoteHomeAuthorityError, match="Remote account home probe is invalid"):
        parse_remote_home_probe(
            profile_id="profile-1",
            connection_generation=7,
            return_code=0,
            stdout=_record(home=home),
            stderr=b"",
        )


def test_probe_rejects_physical_home_mismatch_without_disclosing_paths() -> None:
    with pytest.raises(RemoteHomeAuthorityError) as captured:
        parse_remote_home_probe(
            profile_id="profile-1",
            connection_generation=7,
            return_code=0,
            stdout=_record(
                home="/srv/research/alice",
                physical_home="/private/sensitive/target",
            ),
            stderr=b"",
        )

    rendered = f"{captured.value!r} {captured.value}"
    assert rendered == (
        "RemoteHomeAuthorityError('Remote account home probe is invalid.') "
        "Remote account home probe is invalid."
    )
    assert "/srv/research/alice" not in rendered
    assert "/private/sensitive/target" not in rendered


@pytest.mark.parametrize(
    ("profile_id", "generation"),
    [
        ("", 7),
        ("-profile", 7),
        ("profile/one", 7),
        ("p" * 129, 7),
        ("profile-1", 0),
        ("profile-1", -1),
        ("profile-1", True),
        ("profile-1", 9_007_199_254_740_992),
    ],
)
def test_probe_rejects_invalid_local_binding(profile_id: str, generation: int) -> None:
    with pytest.raises(RemoteHomeAuthorityError, match="Remote account home probe is invalid"):
        parse_remote_home_probe(
            profile_id=profile_id,
            connection_generation=generation,
            return_code=0,
            stdout=_record(),
            stderr=b"",
        )


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


def _fake_account_tools(
    root: Path,
    *,
    user: str,
    uid: int,
    home: Path,
    nss_home: Path | None = None,
) -> Path:
    tools = root / "tools"
    tools.mkdir()
    _write_executable(
        tools / "id",
        "#!/bin/sh\n"
        f"case \"$1\" in -un) printf '%s\\n' {shlex.quote(user)} ;; "
        f"-u) printf '%s\\n' {uid} ;; *) exit 64 ;; esac\n",
    )
    selected_home = nss_home if nss_home is not None else home
    passwd_record = f"{user}:x:{uid}:100:Researcher:{selected_home}:/bin/sh"
    _write_executable(
        tools / "getent",
        "#!/bin/sh\n"
        f"[ \"$1\" = passwd ] && [ \"$2\" = {uid} ] || exit 64\n"
        f"printf '%s\\n' {shlex.quote(passwd_record)}\n",
    )
    _write_executable(
        tools / "stat",
        "#!/bin/sh\n"
        f"printf '%s\\n' {uid}\n",
    )
    return tools


def test_fixed_probe_command_emits_the_private_versioned_record(tmp_path: Path) -> None:
    home = tmp_path / "account-home"
    home.mkdir()
    tools = _fake_account_tools(
        tmp_path,
        user="researcher",
        uid=os.getuid(),
        home=home,
    )
    command = build_remote_home_probe_command()

    completed = subprocess.run(
        command,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": os.fspath(tools)},
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == _record(
        id_user="researcher",
        id_uid=str(os.getuid()),
        home=os.fspath(home),
    )
    assert completed.stderr == b""
    assert command == build_remote_home_probe_command()
    assert "$HOME" not in command
    assert "${HOME" not in command
    assert 'getent passwd "$uid"' in command


def test_guard_revalidates_account_then_preserves_multiline_command(tmp_path: Path) -> None:
    home = tmp_path / "account-home"
    home.mkdir()
    uid = os.getuid()
    tools = _fake_account_tools(
        tmp_path,
        user="researcher",
        uid=uid,
        home=home,
    )
    authority = _authority(user="researcher", uid=uid, home=os.fspath(home))
    remote_command = "printf 'first\\n'\nprintf 'second\\n'"

    completed = subprocess.run(
        build_remote_home_guarded_command(authority, remote_command),
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": os.fspath(tools)},
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == b"first\nsecond\n"
    assert completed.stderr == b""


def test_guard_refuses_nss_home_drift_before_running_command(tmp_path: Path) -> None:
    home = tmp_path / "account-home"
    home.mkdir()
    changed_home = tmp_path / "changed-home"
    changed_home.mkdir()
    uid = os.getuid()
    tools = _fake_account_tools(
        tmp_path,
        user="researcher",
        uid=uid,
        home=home,
        nss_home=changed_home,
    )
    authority = _authority(user="researcher", uid=uid, home=os.fspath(home))
    marker = tmp_path / "must-not-exist"

    completed = subprocess.run(
        build_remote_home_guarded_command(
            authority,
            f": > {shlex.quote(os.fspath(marker))}",
        ),
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": os.fspath(tools)},
        check=False,
    )

    assert completed.returncode != 0
    assert not marker.exists()


def test_rsync_guard_forwards_server_arguments_after_account_revalidation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "account-home"
    home.mkdir()
    uid = os.getuid()
    tools = _fake_account_tools(
        tmp_path,
        user="researcher",
        uid=uid,
        home=home,
    )
    authority = _authority(user="researcher", uid=uid, home=os.fspath(home))
    marker = tmp_path / "rsync-started"
    remote_rsync = tmp_path / "remote-rsync"
    _write_executable(
        remote_rsync,
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {shlex.quote(os.fspath(marker))}\n",
    )
    guarded = build_remote_home_guarded_rsync_path(
        authority,
        shlex.quote(os.fspath(remote_rsync)),
    )

    completed = subprocess.run(
        " ".join(
            (
                *shlex.split(guarded),
                "--server",
                "--sender",
                ".",
                "/remote/target",
            )
        ),
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": os.fspath(tools)},
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert marker.read_text(encoding="utf-8") == ("--server\n--sender\n.\n/remote/target\n")


def test_rsync_guard_refuses_nss_home_drift_before_starting_rsync(
    tmp_path: Path,
) -> None:
    home = tmp_path / "account-home"
    home.mkdir()
    changed_home = tmp_path / "changed-home"
    changed_home.mkdir()
    uid = os.getuid()
    tools = _fake_account_tools(
        tmp_path,
        user="researcher",
        uid=uid,
        home=home,
        nss_home=changed_home,
    )
    authority = _authority(user="researcher", uid=uid, home=os.fspath(home))
    marker = tmp_path / "rsync-must-not-start"
    remote_rsync = tmp_path / "remote-rsync"
    _write_executable(
        remote_rsync,
        f"#!/bin/sh\n: > {shlex.quote(os.fspath(marker))}\n",
    )

    guarded = build_remote_home_guarded_rsync_path(
        authority,
        shlex.quote(os.fspath(remote_rsync)),
    )
    completed = subprocess.run(
        " ".join(
            (
                *shlex.split(guarded),
                "--server",
                ".",
                "/remote/target",
            )
        ),
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": os.fspath(tools)},
        check=False,
    )

    assert completed.returncode != 0
    assert not marker.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS openrsync transport contract")
@pytest.mark.parametrize(
    "remote_rsync_command",
    [
        RSYNC_EXECUTABLE,
        f"/usr/bin/env 'OPENEVO_RSYNC_GUARD=value with spaces' {RSYNC_EXECUTABLE}",
    ],
)
def test_macos_openrsync_preserves_the_guard_through_openssh_argv_join(
    tmp_path: Path,
    remote_rsync_command: str,
) -> None:
    home = tmp_path / "account-home"
    home.mkdir()
    uid = os.getuid()
    tools = _fake_account_tools(
        tmp_path,
        user="researcher",
        uid=uid,
        home=home,
    )
    authority = _authority(user="researcher", uid=uid, home=os.fspath(home))
    fake_ssh = tmp_path / "fake-ssh"
    _write_executable(
        fake_ssh,
        "#!/usr/bin/python3\n"
        "import os\n"
        "import sys\n"
        "os.execv('/bin/sh', ['/bin/sh', '-c', ' '.join(sys.argv[2:])])\n",
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.txt").write_text("guarded transfer\n", encoding="utf-8")
    target = tmp_path / "target"

    completed = subprocess.run(
        [
            RSYNC_EXECUTABLE,
            "--archive",
            "--rsync-path",
            build_remote_home_guarded_rsync_path(authority, remote_rsync_command),
            "-e",
            os.fspath(fake_ssh),
            f"{source}/",
            f"fixture:{target}/",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": os.fspath(tools)},
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (target / "payload.txt").read_text(encoding="utf-8") == "guarded transfer\n"


@pytest.mark.parametrize(
    ("builder", "command"),
    [
        (builder, command)
        for builder in (
            build_remote_home_guarded_command,
            build_remote_home_guarded_rsync_path,
        )
        for command in (
            "",
            "contains\x00nul",
            "contains-unpaired-surrogate-\ud800",
            "x" * 1_048_577,
        )
    ],
)
def test_guard_rejects_invalid_trusted_command(
    builder: Callable[[RemoteHomeAuthority, str], str],
    command: str,
) -> None:
    with pytest.raises(RemoteHomeAuthorityError, match="Remote account command is invalid"):
        builder(_authority(), command)
