from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo import cli, launcher


def test_discovers_literal_hosts_and_included_configs(tmp_path: Path) -> None:
    included = tmp_path / "conf.d"
    included.mkdir()
    (included / "gpu.conf").write_text(
        "Host gpu-lab\n  HostName 10.0.0.7\n  User researcher\n  Port 2207\n",
        encoding="utf-8",
    )
    config = tmp_path / "config"
    config.write_text(
        "Include conf.d/*.conf\n"
        "Host *\n  ServerAliveInterval 10\n"
        "Host !blocked wildcard-*\n  HostName ignored\n"
        "Host cpu-lab\n  HostName cpu.example.test\n  User alice\n",
        encoding="utf-8",
    )

    profiles = launcher.discover_ssh_hosts(config)

    assert [profile.alias for profile in profiles] == ["cpu-lab", "gpu-lab"]
    gpu = profiles[1]
    assert (gpu.hostname, gpu.user, gpu.port) == ("10.0.0.7", "researcher", 2207)


def test_selects_and_remembers_last_workspace(tmp_path: Path) -> None:
    preferences = tmp_path / "launcher.json"
    launcher.save_last_ssh_alias("gpu-lab", preferences)
    profiles = (
        launcher.SshHostProfile("cpu-lab", None, None, None, tmp_path),
        launcher.SshHostProfile("gpu-lab", None, None, None, tmp_path),
    )

    selected = launcher.select_ssh_alias(
        profiles,
        last_alias=launcher.load_last_ssh_alias(preferences),
        interactive=True,
        input_fn=lambda _: "",
        output_fn=lambda _: None,
    )

    assert selected == "gpu-lab"
    assert json.loads(preferences.read_text(encoding="utf-8"))["schema_version"] == 1


def test_noninteractive_selection_requires_an_unambiguous_workspace(tmp_path: Path) -> None:
    profiles = (
        launcher.SshHostProfile("one", None, None, None, tmp_path),
        launcher.SshHostProfile("two", None, None, None, tmp_path),
    )

    with pytest.raises(launcher.LauncherError, match="multiple SSH hosts"):
        launcher.select_ssh_alias(profiles, last_alias=None, interactive=False)


def test_cli_dispatches_webui_to_formal_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(cli, "launcher_main", lambda args: captured.extend(args) or 0)

    assert cli.main(["webui", "--ssh-alias", "gpu-lab", "--no-open"]) == 0
    assert captured == [
        "--self-hosted-webui",
        "--ssh-alias",
        "gpu-lab",
        "--no-open",
    ]


def test_wsl_browser_open_uses_the_windows_url_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_url = (
        "http://127.0.0.1:8765/openevo"
        "#browser-bootstrap=25b017319452639d41d7ce13c2fcabdc3"
    )
    calls: list[list[str]] = []

    monkeypatch.setattr(launcher.os, "name", "posix")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda executable: (
            "/mnt/c/Windows/system32/rundll32.exe"
            if executable == "rundll32.exe"
            else None
        ),
    )

    class Result:
        returncode = 0

    def run(command: list[str], **_: object) -> Result:
        calls.append(command)
        return Result()

    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(
        launcher.webbrowser,
        "open",
        lambda *_args, **_kwargs: pytest.fail("WSL must use the Windows URL handler"),
    )

    assert launcher.open_browser(browser_url) is True
    assert calls == [
        [
            "/mnt/c/Windows/system32/rundll32.exe",
            "url.dll,FileProtocolHandler",
            browser_url,
        ]
    ]


def test_historical_launcher_is_a_thin_compatibility_entrypoint() -> None:
    wrapper = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "dev"
        / "run_remote_agent_development.py"
    )
    source = wrapper.read_text(encoding="utf-8")

    assert "openevo.launcher import command_main" in source
    assert len(source.splitlines()) < 15
