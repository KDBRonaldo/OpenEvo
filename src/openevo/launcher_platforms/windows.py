"""Native Windows SSH discovery, including optional WSL-owned configurations."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Callable

from openevo.launcher_platforms.common import (
    LauncherError,
    SshClientEnvironment,
    SshCommand,
    SshConnection,
    SshHostProfile,
    SshResolutionServices,
)


WSL_DISTRIBUTION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,127}")


def decode_subprocess_output(value: bytes) -> str:
    """Decode ordinary output plus WSL's UTF-16 distribution listing."""

    if b"\x00" in value[:256]:
        return value.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    return value.decode("utf-8", errors="replace")


def validate_wsl_distribution(value: str) -> str:
    distribution = value.strip()
    if not WSL_DISTRIBUTION_PATTERN.fullmatch(distribution):
        raise LauncherError("WSL distribution name contains unsupported characters")
    return distribution


def resolve_wsl_distribution(
    wsl_binary: str,
    explicit: str | None,
    *,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> str:
    if explicit:
        return validate_wsl_distribution(explicit)
    try:
        completed = run(
            [wsl_binary, "--list", "--quiet"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LauncherError(f"could not enumerate WSL distributions: {exc}") from exc
    if completed.returncode != 0:
        detail = decode_subprocess_output(completed.stderr).strip()
        raise LauncherError(
            f"could not enumerate WSL distributions: {detail or completed.returncode}"
        )
    distributions = [
        line.strip().rstrip("\x00")
        for line in decode_subprocess_output(completed.stdout).splitlines()
        if line.strip().rstrip("\x00")
        and not line.strip().rstrip("\x00").casefold().startswith("docker-desktop")
    ]
    ubuntu = next((item for item in distributions if item.casefold() == "ubuntu"), None)
    if ubuntu is not None:
        return validate_wsl_distribution(ubuntu)
    if len(distributions) == 1:
        return validate_wsl_distribution(distributions[0])
    if not distributions:
        raise LauncherError("no ordinary Linux WSL distribution is installed")
    raise LauncherError(
        "multiple WSL distributions are installed; pass --wsl-distribution with the one "
        "that owns your SSH config"
    )


def wsl_config_windows_path(
    wsl_binary: str,
    distribution: str,
    linux_config_path: str | None,
    *,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> Path:
    if linux_config_path:
        command = [
            wsl_binary,
            "-d",
            distribution,
            "--",
            "sh",
            "-c",
            'wslpath -w -- "$1"',
            "openevo-wsl-config",
            linux_config_path,
        ]
    else:
        command = [
            wsl_binary,
            "-d",
            distribution,
            "--",
            "sh",
            "-lc",
            'wslpath -w "$HOME/.ssh/config"',
        ]
    try:
        completed = run(
            command,
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LauncherError(
            f"could not locate the {distribution} WSL SSH config: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = decode_subprocess_output(completed.stderr).strip()
        raise LauncherError(
            f"could not locate the {distribution} WSL SSH config: "
            f"{detail or completed.returncode}"
        )
    rendered = decode_subprocess_output(completed.stdout).strip()
    if not rendered:
        raise LauncherError(f"{distribution} WSL returned an empty SSH config path")
    return Path(rendered)


def wsl_ssh_command(
    args: argparse.Namespace,
    *,
    which: Callable[[str], str | None] = shutil.which,
    distribution_resolver: Callable[[str, str | None], str] = resolve_wsl_distribution,
) -> tuple[tuple[str, ...], str]:
    """Resolve WSL OpenSSH without opening or translating its config file."""

    wsl_binary = which("wsl.exe") or which("wsl")
    if not wsl_binary:
        raise LauncherError("wsl.exe was not found; install WSL or use --ssh-client system")
    distribution = distribution_resolver(wsl_binary, args.wsl_distribution)
    return (wsl_binary, "-d", distribution, "--", "ssh"), distribution


def build_wsl_environment(
    args: argparse.Namespace,
    *,
    command_resolver: Callable[[argparse.Namespace], tuple[tuple[str, ...], str]],
    config_path_resolver: Callable[[str, str, str | None], Path],
    discover_hosts: Callable[..., tuple[SshHostProfile, ...]],
) -> SshClientEnvironment:
    command, distribution = command_resolver(args)
    config_path = config_path_resolver(command[0], distribution, args.ssh_config)
    return SshClientEnvironment(
        command=command,
        profiles=discover_hosts(
            config_path,
            config_home=config_path.parent.parent,
            filesystem_root=Path(config_path.anchor),
        ),
        config_path=config_path,
        label=f"OpenSSH in WSL {distribution}",
    )


def resolve_launcher_ssh(
    args: argparse.Namespace,
    services: SshResolutionServices,
) -> tuple[SshConnection, SshCommand]:
    """Apply native Windows policy across Windows and optional WSL OpenSSH."""

    requested_client = args.ssh_client
    if requested_client == "system":
        environment = services.system_environment(args)
        return services.select_from_environment(args, environment), environment.command
    if requested_client == "wsl":
        if args.ssh_alias or args.host or args.user or args.ssh_port != 22:
            command, distribution = services.wsl_command(args)
            connection = services.resolve_connection(args)
            if args.ssh_config:
                connection = SshConnection(
                    options=("-F", args.ssh_config, *connection.options),
                    destination=connection.destination,
                    display_name=connection.display_name,
                )
            print(f"Using OpenSSH in WSL {distribution}.", flush=True)
            return connection, command
        environment = services.wsl_environment(args)
        return services.select_from_environment(args, environment), environment.command

    if args.host or args.user or args.ssh_port != 22:
        environment = services.system_environment(args)
        return services.select_from_environment(args, environment), environment.command

    system_environment: SshClientEnvironment | None = None
    system_error: LauncherError | None = None
    try:
        system_environment = services.system_environment(args)
    except LauncherError as exc:
        system_error = exc
    wsl_environment: SshClientEnvironment | None = None
    wsl_error: LauncherError | None = None
    try:
        wsl_environment = services.wsl_environment(args)
    except LauncherError as exc:
        wsl_error = exc

    if args.ssh_alias:
        alias = services.validate_alias(args.ssh_alias)
        for environment in (system_environment, wsl_environment):
            if environment is not None and any(item.alias == alias for item in environment.profiles):
                connection = services.connection_with_config(args, environment)
                print(f"Using {environment.label} for SSH alias {alias}.", flush=True)
                return connection, environment.command
        if system_environment is not None:
            return services.connection_with_config(args, system_environment), system_environment.command
        if wsl_environment is not None:
            return services.connection_with_config(args, wsl_environment), wsl_environment.command

    environments = [item for item in (system_environment, wsl_environment) if item is not None]
    owners: dict[str, SshClientEnvironment] = {}
    profiles: dict[str, SshHostProfile] = {}
    for environment in environments:
        for profile in environment.profiles:
            if profile.alias not in profiles:
                profiles[profile.alias] = profile
                owners[profile.alias] = environment
    if profiles:
        preferences_path = Path(args.preferences_file).expanduser() if args.preferences_file else None
        last_alias = None if args.no_remember else services.load_last_alias(preferences_path)
        alias = services.select_alias(
            tuple(sorted(profiles.values(), key=lambda item: item.alias.casefold())),
            last_alias=last_alias,
            interactive=not args.non_interactive and sys.stdin.isatty(),
        )
        args.ssh_alias = alias
        if not args.no_remember:
            services.save_last_alias(alias, preferences_path)
        environment = owners[alias]
        print(f"Using {environment.label} for SSH alias {alias}.", flush=True)
        return services.connection_with_config(args, environment), environment.command

    details = "; ".join(str(error) for error in (system_error, wsl_error) if error is not None)
    raise LauncherError(
        "no literal Host aliases were found in the Windows or WSL SSH configs"
        + (f": {details}" if details else "")
    )
