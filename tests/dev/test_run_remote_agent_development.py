from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "dev"
    / "run_remote_agent_development.py"
)
SPEC = importlib.util.spec_from_file_location("run_remote_agent_development", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
remote_launcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = remote_launcher
SPEC.loader.exec_module(remote_launcher)


@pytest.mark.parametrize("alias", ["openevo-lab", "gpu_lab.2", "server_01"])
def test_accepts_literal_ssh_alias(alias: str) -> None:
    assert remote_launcher.validate_ssh_alias(alias) == alias


@pytest.mark.parametrize(
    "alias",
    ["root@example.com", "gpu lab", "-oProxyCommand=bad", "lab;echo bad", ""],
)
def test_rejects_ssh_command_syntax(alias: str) -> None:
    with pytest.raises(remote_launcher.LauncherError):
        remote_launcher.validate_ssh_alias(alias)


def test_resolves_direct_ssh_connection_without_config_alias() -> None:
    args = remote_launcher.parse_args(
        [
            "--host",
            "js4.blockelite.cn",
            "--user",
            "root",
            "--ssh-port",
            "27104",
        ]
    )

    connection = remote_launcher.resolve_ssh_connection(args)

    assert connection.options == ("-p", "27104")
    assert connection.destination == "root@js4.blockelite.cn"
    assert connection.display_name == "root@js4.blockelite.cn:27104"


def test_tunnel_enables_keepalive_so_dead_forwarding_does_not_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        def poll(self) -> None:
            return None

    def popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(remote_launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(remote_launcher.time, "sleep", lambda _: None)
    connection = remote_launcher.SshConnection(
        options=("-p", "27104"),
        destination="root@js4.blockelite.cn",
        display_name="lab",
    )

    remote_launcher._start_tunnel("ssh", connection, 8765, 8787)

    command = captured["command"]
    assert isinstance(command, list)
    assert "ServerAliveInterval=10" in command
    assert "ServerAliveCountMax=3" in command
    assert "TCPKeepAlive=yes" in command


@pytest.mark.parametrize(
    ("host", "user"),
    [
        ("host;bad", "root"),
        ("-oProxyCommand=bad", "root"),
        ("js4.blockelite.cn", "root;bad"),
    ],
)
def test_rejects_direct_ssh_command_syntax(host: str, user: str) -> None:
    with pytest.raises(remote_launcher.LauncherError):
        remote_launcher.validate_ssh_host(host)
        remote_launcher.validate_ssh_user(user)


def test_rejects_mixing_alias_and_direct_ssh_inputs() -> None:
    args = remote_launcher.parse_args(
        ["--ssh-alias", "openevo-lab", "--host", "js4.blockelite.cn", "--user", "root"]
    )
    with pytest.raises(remote_launcher.LauncherError):
        remote_launcher.resolve_ssh_connection(args)


def test_normalizes_public_github_fork_urls() -> None:
    assert (
        remote_launcher.normalize_repository_url("git@github.com:KDBRonaldo/OpenEvo.git")
        == "https://github.com/KDBRonaldo/OpenEvo.git"
    )
    assert (
        remote_launcher.normalize_repository_url("https://github.com/KDBRonaldo/OpenEvo")
        == "https://github.com/KDBRonaldo/OpenEvo.git"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://token@github.com/KDBRonaldo/OpenEvo.git",
        "https://example.com/KDBRonaldo/OpenEvo.git",
        "file:///tmp/OpenEvo",
        "https://github.com/KDBRonaldo/OpenEvo/extra",
    ],
)
def test_rejects_credentialed_or_non_github_repository_urls(url: str) -> None:
    with pytest.raises(remote_launcher.LauncherError):
        remote_launcher.normalize_repository_url(url)


def test_remote_script_quotes_values_and_uses_private_managed_paths() -> None:
    token = "A-token-with-'quote-and-more-than-32-characters"
    script = remote_launcher.build_remote_script(
        repository_url="https://github.com/KDBRonaldo/OpenEvo.git",
        branch="feature/desktop-e2e",
        expected_commit="a" * 40,
        token=token,
        remote_port=8787,
        evolution_model="gpt-5.5",
    )

    assert "state_root=\"$HOME/.openevo/dev-agent\"" in script
    assert "source_marker=\"$state_root/managed-source-v1\"" in script
    assert "git -C \"$source_root\" merge --ff-only" in script
    assert 'deployed_commit="$(git -C "$source_root" rev-parse HEAD)"' in script
    assert '"$uv_bin" sync --frozen --python 3.11' in script
    assert "bash -lc 'command -v codex'" in script
    assert '"$HOME/.npm-global/bin/codex"' in script
    assert '"$HOME"/.nvm/versions/node/*/bin/codex' in script
    assert '"PATH=$codex_dir:$PATH"' in script
    assert '--codex-binary "$codex_bin"' in script
    assert "scripts/dev/live_agent_daemon.py" in script
    assert "Refusing to modify an unrecognized path" in script
    assert "'\"'\"'" in script

    shell = shutil.which("sh")
    if shell is not None:
        syntax = subprocess.run(
            [shell, "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        assert syntax.returncode == 0, syntax.stderr


@pytest.mark.parametrize("branch", ["feature/desktop-e2e", "fix/daemon_2", "stable"])
def test_accepts_normal_branch_names(branch: str) -> None:
    assert remote_launcher.validate_branch(branch) == branch


@pytest.mark.parametrize("branch", ["../stable", "branch@{1}", "-bad", "bad/", ""])
def test_rejects_unsafe_branch_names(branch: str) -> None:
    with pytest.raises(remote_launcher.LauncherError):
        remote_launcher.validate_branch(branch)
