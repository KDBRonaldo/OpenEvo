"""Native macOS launcher policy."""

from __future__ import annotations

import argparse

from openevo.launcher_platforms.common import (
    LauncherError,
    SshCommand,
    SshConnection,
    SshResolutionServices,
)


def resolve_launcher_ssh(
    args: argparse.Namespace,
    services: SshResolutionServices,
) -> tuple[SshConnection, SshCommand]:
    """Resolve only the native macOS OpenSSH installation and config."""

    if args.ssh_client == "wsl":
        raise LauncherError("--ssh-client wsl is available only from native Windows")
    environment = services.system_environment(args)
    return services.select_from_environment(args, environment), environment.command
