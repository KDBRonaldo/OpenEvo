from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from pydantic import ValidationError
import pytest

from openevo.deployment import SystemOpenSshAliasProfile
from openevo.deployment.ssh import (
    SystemOpenSshAskpassEnvironment,
    build_system_openssh_command_argv,
    build_system_openssh_control_argv,
    build_system_openssh_core_tunnel_argv,
    build_system_openssh_environment,
    build_system_openssh_master_argv,
    build_system_openssh_probe_argv,
)
from openevo.deployment.system_executables import (
    MACOS_SYSTEM_COMMAND_PATH,
    SSH_EXECUTABLE,
)


def _profile(alias: str = "evolab") -> SystemOpenSshAliasProfile:
    return SystemOpenSshAliasProfile(
        profile_id="profile-01",
        ssh_host_alias=alias,
    )


def _control_path(tmp_path: Path) -> Path:
    suffix = tmp_path.name[-24:]
    return Path("/tmp") / f"oe-{suffix}" / "m"


def _assert_user_authority_not_flattened(argv: list[str]) -> None:
    assert argv[0] == SSH_EXECUTABLE
    forbidden_tokens = {"-F", "-p", "-l", "-i"}
    forbidden_options = (
        "IdentityFile=",
        "IdentitiesOnly=",
        "IdentityAgent=",
        "PasswordAuthentication=",
        "KbdInteractiveAuthentication=",
        "StrictHostKeyChecking=",
        "UserKnownHostsFile=",
        "GlobalKnownHostsFile=",
        "KnownHostsCommand=",
        "ProxyCommand=",
        "ProxyJump=",
        "Hostname=",
    )
    assert forbidden_tokens.isdisjoint(argv)
    assert not any(
        token.startswith(forbidden_options)
        for token in argv
    )


def test_alias_profile_contains_no_flattened_connection_or_auth_fields() -> None:
    profile = _profile()
    assert profile.model_dump(mode="json") == {
        "schema_version": 2,
        "profile_id": "profile-01",
        "connection_authority": "system_openssh",
        "ssh_host_alias": "evolab",
    }

    for field, value in {
        "host": "10.0.0.2",
        "user": "alice",
        "port": 2222,
        "identity_file": "/tmp/key",
        "proxy_jump": "bastion",
    }.items():
        with pytest.raises(ValidationError):
            SystemOpenSshAliasProfile.model_validate(
                {**profile.model_dump(mode="json"), field: value}
            )


@pytest.mark.parametrize(
    "alias",
    [
        "-oProxyCommand=bad",
        "user@host",
        "host name",
        "host*",
        "!host",
        "/tmp/host",
        "x" * 129,
    ],
)
def test_alias_profile_rejects_non_literal_argv_values(alias: str) -> None:
    with pytest.raises(ValidationError):
        _profile(alias)


def test_probe_builder_is_exact_system_ssh_plus_literal_alias() -> None:
    assert build_system_openssh_probe_argv(_profile()) == [
        SSH_EXECUTABLE,
        "-G",
        "--",
        "evolab",
    ]


def test_explicit_managed_config_is_used_by_every_system_ssh_process(
    tmp_path: Path,
) -> None:
    config = (tmp_path / "managed" / "config").resolve()
    control = _control_path(tmp_path)
    config_prefix = [SSH_EXECUTABLE, "-F", str(config)]

    argv_sets = (
        build_system_openssh_probe_argv(_profile(), config_path=config),
        build_system_openssh_master_argv(
            _profile(), control_path=control, config_path=config
        ),
        build_system_openssh_command_argv(
            _profile(),
            control_path=control,
            remote_command="true",
            config_path=config,
        ),
        build_system_openssh_control_argv(
            _profile(),
            control_path=control,
            operation="check",
            config_path=config,
        ),
        build_system_openssh_core_tunnel_argv(
            _profile(),
            control_path=control,
            remote_port=8765,
            config_path=config,
        ),
    )

    assert all(argv[:3] == config_prefix for argv in argv_sets)


def test_explicit_managed_config_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="config path"):
        build_system_openssh_probe_argv(
            _profile(),
            config_path=Path("relative/config"),
        )


def test_owned_master_builder_clears_ambient_sessions_and_forwards(tmp_path: Path) -> None:
    control = _control_path(tmp_path)
    argv = build_system_openssh_master_argv(
        _profile(),
        control_path=control,
        connection_attempts=3,
        connect_timeout_seconds=15,
        keepalive_interval_seconds=20,
        keepalive_count=3,
    )

    assert argv == [
        SSH_EXECUTABLE,
        "-M",
        "-S",
        str(control),
        "-o",
        "ControlPersist=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ForwardAgent=no",
        "-o",
        "RequestTTY=no",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ConnectionAttempts=3",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=20",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "TCPKeepAlive=yes",
        "-N",
        "-T",
        "--",
        "evolab",
    ]
    _assert_user_authority_not_flattened(argv)


@pytest.mark.parametrize("connection_attempts", [0, 6, True])
def test_owned_master_builder_rejects_invalid_connection_attempts(
    tmp_path: Path,
    connection_attempts: object,
) -> None:
    with pytest.raises(ValueError, match="connection attempts"):
        build_system_openssh_master_argv(
            _profile(),
            control_path=_control_path(tmp_path),
            connection_attempts=connection_attempts,  # type: ignore[arg-type]
        )


def test_command_builder_reuses_exact_socket_without_auth_or_route_options(
    tmp_path: Path,
) -> None:
    control = _control_path(tmp_path)
    argv = build_system_openssh_command_argv(
        _profile(),
        control_path=control,
        remote_command="printf '%s' hello",
    )

    assert argv[-3:] == ["--", "evolab", "printf '%s' hello"]
    assert argv[0:4] == [SSH_EXECUTABLE, "-S", str(control), "-o"]
    for option in (
        "ControlMaster=no",
        "ControlPersist=no",
        "ClearAllForwardings=yes",
        "PermitLocalCommand=no",
        "ForwardAgent=no",
        "RequestTTY=no",
        "RemoteCommand=none",
    ):
        assert option in argv
    _assert_user_authority_not_flattened(argv)


@pytest.mark.parametrize("operation", ["check", "exit", "stop"])
def test_control_builder_has_a_closed_operation_set(
    tmp_path: Path,
    operation: str,
) -> None:
    control = _control_path(tmp_path)
    argv = build_system_openssh_control_argv(
        _profile(),
        control_path=control,
        operation=operation,
    )

    assert argv == [
        SSH_EXECUTABLE,
        "-S",
        str(control),
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPersist=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-O",
        operation,
        "--",
        "evolab",
    ]


def test_control_builder_rejects_arbitrary_operations(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="control operation"):
        build_system_openssh_control_argv(
            _profile(),
            control_path=_control_path(tmp_path),
            operation="proxy",
        )


def test_core_tunnel_uses_owned_mux_and_stdio_forwarding(tmp_path: Path) -> None:
    control = _control_path(tmp_path)
    argv = build_system_openssh_core_tunnel_argv(
        _profile(),
        control_path=control,
        remote_port=8765,
    )

    assert argv[-5:] == ["-W", "127.0.0.1:8765", "-T", "--", "evolab"]
    assert "ClearAllForwardings=yes" in argv
    assert "ExitOnForwardFailure=yes" in argv
    assert "-L" not in argv
    _assert_user_authority_not_flattened(argv)


def test_child_environment_is_closed_and_keeps_only_user_authorities() -> None:
    askpass = SystemOpenSshAskpassEnvironment(
        helper_path="/Applications/OpenEvo Desktop.app/Contents/MacOS/openevo-ssh-askpass",
        broker_socket="/private/tmp/oe-askpass/socket",
        capability="a" * 64,
        connection_generation=7,
    )
    environment = build_system_openssh_environment(
        home="/Users/alice",
        inherited={
            "LANG": "en_US.UTF-8",
            "LC_CTYPE": "UTF-8",
            "SSH_AUTH_SOCK": "/private/tmp/com.apple.launchd.agent/Listeners",
            "AWS_SECRET_ACCESS_KEY": "must-not-leak",
            "OPENEVO_BACKEND_TOKEN": "must-not-leak",
            "PATH": "/attacker/bin",
            "DYLD_INSERT_LIBRARIES": "/attacker/lib.dylib",
        },
        askpass=askpass,
    )

    assert environment == {
        "HOME": "/Users/alice",
        "PATH": MACOS_SYSTEM_COMMAND_PATH,
        "LANG": "en_US.UTF-8",
        "LC_CTYPE": "UTF-8",
        "SSH_AUTH_SOCK": "/private/tmp/com.apple.launchd.agent/Listeners",
        "SSH_ASKPASS": askpass.helper_path,
        "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": "openevo-ssh-askpass",
        "OPENEVO_SSH_ASKPASS_SOCKET": askpass.broker_socket,
        "OPENEVO_SSH_ASKPASS_CAPABILITY": askpass.capability,
        "OPENEVO_SSH_CONNECTION_GENERATION": "7",
    }
    assert "must-not-leak" not in repr(environment)
    assert askpass.capability not in repr(askpass)
    assert askpass.helper_path not in repr(askpass)
    assert askpass.broker_socket not in repr(askpass)


def test_child_environment_rejects_invalid_authority_values() -> None:
    with pytest.raises(ValueError):
        build_system_openssh_environment(home="relative/home", inherited={})
    with pytest.raises(ValueError):
        build_system_openssh_environment(
            home="/Users/alice",
            inherited={"SSH_AUTH_SOCK": "relative/socket"},
        )
    with pytest.raises(ValueError):
        SystemOpenSshAskpassEnvironment(
            helper_path="relative/helper",
            broker_socket="/private/tmp/socket",
            capability="a" * 64,
            connection_generation=1,
        )


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path(SSH_EXECUTABLE).is_file(),
    reason="requires the supported macOS system OpenSSH",
)
def test_supported_openssh_clears_configured_forwards_and_l_but_keeps_w(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.write_text(
        """
        Host controlled
          HostName localhost
          LocalForward 49110 127.0.0.1:22
          RemoteForward 49112 127.0.0.1:22
          DynamicForward 49113
        """,
        encoding="utf-8",
    )
    config.chmod(0o600)

    with_l = subprocess.run(
        [
            SSH_EXECUTABLE,
            "-G",
            "-F",
            str(config),
            "-o",
            "ClearAllForwardings=yes",
            "-L",
            "49111:127.0.0.1:22",
            "controlled",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={},
    ).stdout
    with_w = subprocess.run(
        [
            SSH_EXECUTABLE,
            "-G",
            "-F",
            str(config),
            "-W",
            "127.0.0.1:22",
            "controlled",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={},
    ).stdout

    assert "clearallforwardings yes" in with_l
    assert not any(
        line.startswith(("localforward ", "remoteforward ", "dynamicforward "))
        for line in with_l.splitlines()
    )
    assert "clearallforwardings yes" in with_w
    assert "sessiontype none" in with_w
