from __future__ import annotations

import json
import tomllib
from pathlib import Path

import yaml

from openevo import cli as openevo_cli
from openevo.cli import main
from openevo.remote import RemoteCommandResult
from openevo.sidecar import RemoteProfileConfig


def _write_config(path: Path, payload: dict) -> Path:
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _minimal_payload() -> dict:
    return {
        "version": 1,
        "experiment": {"name": "biology-components"},
        "agent": {"preset": "codex", "model": "gpt-5.1-codex-mini"},
        "runtime": {"image": "runtime:latest"},
        "tasks": [{"id": "task-a", "instruction": "Do A.", "workspace": "/tmp/a"}],
    }


def _minimal_science_payload() -> dict:
    return {
        "version": 1,
        "project": {"name": "protein-design"},
        "remote_profile": "science-team",
        "task": {
            "id": "folding-baseline",
            "objective": "Improve the folding baseline.",
            "source": {
                "type": "remote_path",
                "path": "/datasets/folding-baseline",
            },
        },
    }


def _remote_profile_for_cli() -> RemoteProfileConfig:
    return RemoteProfileConfig(
        version=1,
        id="science-team",
        host="gpu.example.edu",
        user="alice",
    )


class _CliRecordingSshTransport:
    profiles = []

    def __init__(self, profile) -> None:
        self.profiles.append(profile)

    def run(
        self,
        command,
        *,
        cwd=None,
        env=None,
        timeout_seconds=30.0,
    ):
        if command == 'df -Pk "$HOME"':
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=(
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/root 100000000 1 99999999 1% /home\n"
                ),
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path, remote_path):
        return None


def test_cli_dry_run_json_outputs_compiled_plan(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _write_config(tmp_path / "experiment.yaml", _minimal_payload())

    exit_code = main(["run", str(config_path), "--dry-run", "--json", "--rounds", "2"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["round_count"] == 2
    assert payload["tasks"][0]["rounds"][1]["evolution_jobs"][2]["method"] == (
        "agent_system_history_reflector"
    )


def test_cli_dry_run_output_file_matches_reported_plan_path(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _write_config(tmp_path / "experiment.yaml", _minimal_payload())
    output_dir = tmp_path / "out"

    exit_code = main(
        [
            "run",
            str(config_path),
            "--dry-run",
            "--json",
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    file_payload = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
    assert file_payload["plan_path"] == payload["plan_path"]


def test_cli_invalid_config_returns_nonzero(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _write_config(
        tmp_path / "experiment.yaml",
        _minimal_payload() | {"unexpected": True},
    )

    exit_code = main(["run", str(config_path), "--dry-run"])

    assert exit_code == 1
    assert "error:" in capsys.readouterr().err


def test_cli_science_compile_outputs_experiment_config(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())

    exit_code = main(["science", "compile", str(config_path), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["experiment"]["name"] == "protein-design"
    assert payload["agent"]["auth"] == "subscription"
    assert payload["agent"]["settings"]["capture_mode"] == "transcript"
    assert payload["runtime"]["image"] == "openevo/science-runtime:0.1.0"
    assert payload["tasks"][0]["workspace"] == "/datasets/folding-baseline"
    assert "path" not in payload


def test_cli_science_compile_accepts_prepared_workspace(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _write_config(
        tmp_path / "science.yaml",
        _minimal_science_payload()
        | {
            "task": {
                "id": "local-task",
                "objective": "Run local workflow.",
                "source": {
                    "type": "local_folder",
                    "path": "workflows/local-task",
                },
            }
        },
    )

    exit_code = main(
        [
            "science",
            "compile",
            str(config_path),
            "--json",
            "--prepared-workspace",
            "local-task=/home/user/.openevo/workspaces/local-task",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"][0]["workspace"] == (
        "/home/user/.openevo/workspaces/local-task"
    )


def test_cli_science_compile_rejects_invalid_prepared_workspace(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())

    exit_code = main(
        [
            "science",
            "compile",
            str(config_path),
            "--prepared-workspace",
            "local-task",
        ]
    )

    assert exit_code == 1
    assert "--prepared-workspace must use task_id=/remote/path" in capsys.readouterr().err


def test_cli_sidecar_plan_outputs_workspace_and_preflight(
    tmp_path: Path,
    capsys,
) -> None:
    science_path = _write_config(
        tmp_path / "science.yaml",
        _minimal_science_payload()
        | {
            "task": {
                "id": "local-task",
                "objective": "Run local workflow.",
                "source": {
                    "type": "local_folder",
                    "path": "workflows/local-task",
                },
            }
        },
    )
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
            "proxy": {"https_proxy": "http://127.0.0.1:7890"},
        },
    )

    exit_code = main(
        [
            "sidecar",
            "plan",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_profile_id"] == "science-team"
    assert payload["preflight"]["require_codex_subscription"] is True
    assert payload["proxy_env"]["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert payload["workspace"]["actions"][0]["type"] == "upload_dir"
    assert payload["experiment"]["tasks"][0]["workspace"].startswith(
        "/home/alice/.openevo/workspaces/protein-design/local-task/"
    )


def test_cli_sidecar_execute_skip_preflight_outputs_workspace_report(
    tmp_path: Path,
    capsys,
) -> None:
    science_path = _write_config(
        tmp_path / "science.yaml",
        _minimal_science_payload()
        | {
            "task": {
                "id": "local-task",
                "objective": "Run local workflow.",
                "source": {
                    "type": "remote_path",
                    "path": "/datasets/local-task",
                },
            }
        },
    )
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(
        [
            "sidecar",
            "execute",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--skip-preflight",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["preflight"] is None
    assert payload["workspace"]["actions"][0]["status"] == "skip"


def test_cli_sidecar_execute_default_preflight_uses_parseable_dry_run_output(
    tmp_path: Path,
    capsys,
) -> None:
    science_path = _write_config(
        tmp_path / "science.yaml",
        _minimal_science_payload()
        | {
            "task": {
                "id": "local-task",
                "objective": "Run local workflow.",
                "source": {
                    "type": "remote_path",
                    "path": "/datasets/local-task",
                },
            }
        },
    )
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(
        [
            "sidecar",
            "execute",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["preflight"]["ready"] is True
    assert {check["status"] for check in payload["preflight"]["checks"]} == {"pass"}
    assert payload["workspace"]["actions"][0]["status"] == "skip"


def test_cli_sidecar_execute_transport_ssh_uses_ssh_transport(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    _CliRecordingSshTransport.profiles = []
    monkeypatch.setattr(
        "openevo.cli.SshRemoteExecutorTransport",
        _CliRecordingSshTransport,
    )
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(
        [
            "sidecar",
            "execute",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--transport",
            "ssh",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert _CliRecordingSshTransport.profiles[0].id == "science-team"


def test_cli_sidecar_bootstrap_default_dry_run_outputs_report(
    tmp_path: Path,
    capsys,
) -> None:
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(
        [
            "sidecar",
            "bootstrap",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["prepared_paths"]["state_root"].endswith(
        "/protein-design/folding-baseline"
    )
    assert payload["steps"][-1]["id"] == "docker_pull_runtime"


def test_cli_sidecar_bootstrap_transport_ssh_uses_ssh_transport(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    _CliRecordingSshTransport.profiles = []
    monkeypatch.setattr(
        "openevo.cli.SshRemoteExecutorTransport",
        _CliRecordingSshTransport,
    )
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(
        [
            "sidecar",
            "bootstrap",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--transport",
            "ssh",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert _CliRecordingSshTransport.profiles[0].id == "science-team"


def test_cli_sidecar_serve_invokes_runner(monkeypatch) -> None:
    calls = []

    def fake_runner(app, *, host: str, port: int) -> None:
        calls.append((app.title, host, port))

    monkeypatch.setattr("openevo.cli._run_sidecar_server", fake_runner)

    exit_code = main(
        [
            "sidecar",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "3766",
        ]
    )

    assert exit_code == 0
    assert calls == [("OpenEvo Desktop Sidecar", "127.0.0.1", 3766)]


def test_cli_sidecar_serve_passes_desktop_config_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    def fake_create_sidecar_app(**kwargs):
        app_calls.append(kwargs)
        return FakeApp()

    monkeypatch.setattr("openevo.cli.create_sidecar_app", fake_create_sidecar_app)
    monkeypatch.setattr("openevo.cli._run_sidecar_server", lambda app, *, host, port: None)

    exit_code = main(
        [
            "sidecar",
            "serve",
            "--desktop-config-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert app_calls[0]["config_root"] == tmp_path
    assert app_calls[0]["transport_factory"] is not None


def test_cli_sidecar_serve_loads_config_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_calls = []
    runner_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    def fake_create_project_app(
        project,
        profile,
        *,
        transport_factory,
        transport_kind,
    ):
        app_calls.append((project, profile, transport_factory, transport_kind))
        return FakeApp()

    def fake_runner(app, *, host: str, port: int) -> None:
        runner_calls.append((app.title, host, port))

    monkeypatch.setattr("openevo.cli.create_sidecar_app_for_project", fake_create_project_app)
    monkeypatch.setattr("openevo.cli._run_sidecar_server", fake_runner)
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(
        [
            "sidecar",
            "serve",
            "--config",
            str(science_path),
            "--remote-profile",
            str(profile_path),
        ]
    )

    assert exit_code == 0
    assert len(app_calls) == 1
    project, profile, transport_factory, transport_kind = app_calls[0]
    assert project.task.id == "folding-baseline"
    assert profile.id == "science-team"
    assert transport_factory(profile).__class__.__name__ == "_CliDryRunTransport"
    assert transport_kind == "dry-run"
    assert runner_calls == [("OpenEvo Desktop Sidecar", "127.0.0.1", 3766)]


def test_cli_sidecar_serve_ssh_transport_factory_is_lazy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    def fake_create_project_app(
        project,
        profile,
        *,
        transport_factory,
        transport_kind,
    ):
        app_calls.append((project, profile, transport_factory, transport_kind))
        return FakeApp()

    monkeypatch.setattr("openevo.cli.create_sidecar_app_for_project", fake_create_project_app)
    monkeypatch.setattr("openevo.cli._run_sidecar_server", lambda app, *, host, port: None)
    _CliRecordingSshTransport.profiles = []
    monkeypatch.setattr(
        "openevo.cli.SshRemoteExecutorTransport",
        _CliRecordingSshTransport,
    )
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(
        [
            "sidecar",
            "serve",
            "--config",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--transport",
            "ssh",
        ]
    )

    assert exit_code == 0
    assert _CliRecordingSshTransport.profiles == []
    _, profile, transport_factory, transport_kind = app_calls[0]
    assert transport_kind == "ssh"
    transport_factory(profile)
    assert _CliRecordingSshTransport.profiles[0].id == "science-team"


def test_cli_sidecar_serve_allows_ssh_transport_without_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    def fake_create_sidecar_app(**kwargs):
        app_calls.append(kwargs)
        return FakeApp()

    monkeypatch.setattr("openevo.cli.create_sidecar_app", fake_create_sidecar_app)
    monkeypatch.setattr("openevo.cli._run_sidecar_server", lambda app, *, host, port: None)
    _CliRecordingSshTransport.profiles = []
    monkeypatch.setattr(
        "openevo.cli.SshRemoteExecutorTransport",
        _CliRecordingSshTransport,
    )

    exit_code = main(
        [
            "sidecar",
            "serve",
            "--transport",
            "ssh",
            "--desktop-config-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert _CliRecordingSshTransport.profiles == []
    profile = _remote_profile_for_cli()
    app_calls[0]["transport_factory"](profile)
    assert _CliRecordingSshTransport.profiles[0].id == "science-team"


def test_cli_sidecar_serve_requires_config_and_profile_together(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    runner_calls = []

    def fake_runner(app, *, host: str, port: int) -> None:
        runner_calls.append((app.title, host, port))

    monkeypatch.setattr("openevo.cli._run_sidecar_server", fake_runner)
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())

    exit_code = main(["sidecar", "serve", "--config", str(science_path)])

    assert exit_code == 1
    assert "--config and --remote-profile must be used together" in capsys.readouterr().err
    assert runner_calls == []


def test_cli_sidecar_serve_requires_profile_and_config_together(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    runner_calls = []

    def fake_runner(app, *, host: str, port: int) -> None:
        runner_calls.append((app.title, host, port))

    monkeypatch.setattr("openevo.cli._run_sidecar_server", fake_runner)
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(["sidecar", "serve", "--remote-profile", str(profile_path)])

    assert exit_code == 1
    assert "--config and --remote-profile must be used together" in capsys.readouterr().err
    assert runner_calls == []


def _desktop_static_root(tmp_path: Path) -> Path:
    root = tmp_path / "desktop-web"
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text(
        "<!doctype html><title>OpenEvo CLI Desktop</title><div id='root'></div>",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("window.__openevoCliTest = true;", encoding="utf-8")
    return root


def test_cli_desktop_serve_invokes_runner_with_wrapped_app(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls = []

    def fake_runner(app, *, host: str, port: int) -> None:
        calls.append((app, host, port))

    monkeypatch.setattr("openevo.cli._run_sidecar_server", fake_runner)

    exit_code = main(
        [
            "desktop",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "3766",
            "--static-root",
            str(_desktop_static_root(tmp_path)),
        ]
    )

    assert exit_code == 0
    assert calls[0][1:] == ("127.0.0.1", 3766)
    assert calls[0][0].title == "OpenEvo Desktop Sidecar"
    assert "http://127.0.0.1:3766/openevo" in capsys.readouterr().err


def test_cli_desktop_serve_passes_desktop_config_root_and_static_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_calls = []
    desktop_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    def fake_create_sidecar_app(**kwargs):
        app_calls.append(kwargs)
        return FakeApp()

    def fake_create_desktop_app(app, *, static_root=None):
        desktop_calls.append((app, static_root))
        return app

    monkeypatch.setattr("openevo.cli.create_sidecar_app", fake_create_sidecar_app)
    monkeypatch.setattr(
        "openevo.cli.create_desktop_app",
        fake_create_desktop_app,
        raising=False,
    )
    monkeypatch.setattr("openevo.cli._run_sidecar_server", lambda app, *, host, port: None)

    exit_code = main(
        [
            "desktop",
            "serve",
            "--desktop-config-root",
            str(tmp_path / "configs"),
            "--static-root",
            str(tmp_path / "web"),
        ]
    )

    assert exit_code == 0
    assert app_calls[0]["config_root"] == tmp_path / "configs"
    assert app_calls[0]["transport_factory"] is not None
    assert len(desktop_calls) == 1
    assert desktop_calls[0][1] == tmp_path / "web"


def test_cli_desktop_serve_loads_config_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_calls = []
    desktop_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    def fake_create_project_app(
        project,
        profile,
        *,
        transport_factory,
        transport_kind,
    ):
        app_calls.append((project, profile, transport_factory, transport_kind))
        return FakeApp()

    def fake_create_desktop_app(app, *, static_root=None):
        desktop_calls.append((app, static_root))
        return app

    monkeypatch.setattr("openevo.cli.create_sidecar_app_for_project", fake_create_project_app)
    monkeypatch.setattr(
        "openevo.cli.create_desktop_app",
        fake_create_desktop_app,
        raising=False,
    )
    monkeypatch.setattr("openevo.cli._run_sidecar_server", lambda app, *, host, port: None)
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(
        [
            "desktop",
            "serve",
            "--config",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--static-root",
            str(tmp_path / "web"),
        ]
    )

    assert exit_code == 0
    project, profile, transport_factory, transport_kind = app_calls[0]
    assert project.task.id == "folding-baseline"
    assert profile.id == "science-team"
    assert transport_factory(profile).__class__.__name__ == "_CliDryRunTransport"
    assert transport_kind == "dry-run"
    assert desktop_calls[0][1] == tmp_path / "web"


def test_cli_desktop_serve_requires_config_and_profile_together(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setattr("openevo.cli._run_sidecar_server", lambda app, *, host, port: None)
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())

    exit_code = main(["desktop", "serve", "--config", str(science_path)])

    assert exit_code == 1
    assert "desktop serve --config and --remote-profile must be used together" in (
        capsys.readouterr().err
    )


def test_cli_desktop_serve_rejects_incomplete_static_root(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    runner_calls = []
    static_root = tmp_path / "desktop-web"
    static_root.mkdir()
    (static_root / "index.html").write_text(
        "<!doctype html><title>OpenEvo CLI Desktop</title><div id='root'></div>",
        encoding="utf-8",
    )

    def fake_runner(app, *, host: str, port: int) -> None:
        runner_calls.append((app, host, port))

    monkeypatch.setattr("openevo.cli._run_sidecar_server", fake_runner)

    exit_code = main(["desktop", "serve", "--static-root", str(static_root)])

    assert exit_code == 1
    assert runner_calls == []
    assert "assets" in capsys.readouterr().err


def test_cli_desktop_open_opens_browser_and_runs_desktop_app(
    tmp_path: Path,
    monkeypatch,
) -> None:
    launcher_calls = []
    desktop_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    def fake_create_sidecar_app(**kwargs):
        return FakeApp()

    def fake_create_desktop_app(app, *, static_root=None):
        desktop_calls.append((app, static_root))
        return app

    def fake_launcher(app, *, host: str, port: int, sock, url: str, open_browser: bool):
        launcher_calls.append((app, host, port, sock.getsockname()[1], url, open_browser))
        sock.close()

    monkeypatch.setattr("openevo.cli.create_sidecar_app", fake_create_sidecar_app)
    monkeypatch.setattr("openevo.cli.create_desktop_app", fake_create_desktop_app)
    monkeypatch.setattr("openevo.cli._run_desktop_open_server", fake_launcher)
    static_root = _desktop_static_root(tmp_path)

    exit_code = main(
        [
            "desktop",
            "open",
            "--host",
            "127.0.0.1",
            "--port",
            "3777",
            "--static-root",
            str(static_root),
        ]
    )

    assert exit_code == 0
    assert launcher_calls == [
        (
            desktop_calls[0][0],
            "127.0.0.1",
            3777,
            3777,
            "http://127.0.0.1:3777/openevo",
            True,
        )
    ]
    assert desktop_calls[0][1] == static_root


def test_cli_desktop_open_defaults_to_ssh_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    def fake_create_sidecar_app(**kwargs):
        app_calls.append(kwargs)
        return FakeApp()

    monkeypatch.setattr("openevo.cli.create_sidecar_app", fake_create_sidecar_app)
    monkeypatch.setattr(
        "openevo.cli.create_desktop_app",
        lambda app, *, static_root=None: app,
    )
    monkeypatch.setattr(
        "openevo.cli._run_desktop_open_server",
        lambda app, *, host, port, sock, url, open_browser: sock.close(),
    )
    _CliRecordingSshTransport.profiles = []
    monkeypatch.setattr(
        "openevo.cli.SshRemoteExecutorTransport",
        _CliRecordingSshTransport,
    )

    exit_code = main(
        [
            "desktop",
            "open",
            "--no-browser",
            "--port",
            "3778",
            "--desktop-config-root",
            str(tmp_path / "configs"),
            "--static-root",
            str(_desktop_static_root(tmp_path)),
        ]
    )

    assert exit_code == 0
    assert app_calls[0]["transport_kind"] == "ssh"
    app_calls[0]["transport_factory"](_remote_profile_for_cli())
    assert _CliRecordingSshTransport.profiles[0].id == "science-team"


def test_cli_desktop_open_allows_explicit_dry_run_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    def fake_create_sidecar_app(**kwargs):
        app_calls.append(kwargs)
        return FakeApp()

    monkeypatch.setattr("openevo.cli.create_sidecar_app", fake_create_sidecar_app)
    monkeypatch.setattr(
        "openevo.cli.create_desktop_app",
        lambda app, *, static_root=None: app,
    )
    monkeypatch.setattr(
        "openevo.cli._run_desktop_open_server",
        lambda app, *, host, port, sock, url, open_browser: sock.close(),
    )

    exit_code = main(
        [
            "desktop",
            "open",
            "--transport",
            "dry-run",
            "--no-browser",
            "--port",
            "3778",
            "--desktop-config-root",
            str(tmp_path / "configs"),
            "--static-root",
            str(_desktop_static_root(tmp_path)),
        ]
    )

    assert exit_code == 0
    assert app_calls[0]["transport_kind"] == "dry-run"
    assert (
        app_calls[0]["transport_factory"](_remote_profile_for_cli())
        .__class__.__name__
        == "_CliDryRunTransport"
    )


def test_cli_desktop_open_no_browser_suppresses_browser_open(
    tmp_path: Path,
    monkeypatch,
) -> None:
    launcher_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    monkeypatch.setattr("openevo.cli.create_sidecar_app", lambda **kwargs: FakeApp())
    monkeypatch.setattr(
        "openevo.cli.create_desktop_app",
        lambda app, *, static_root=None: app,
    )
    monkeypatch.setattr(
        "openevo.cli._run_desktop_open_server",
        lambda app, *, host, port, sock, url, open_browser: (
            launcher_calls.append((host, port, url, open_browser)),
            sock.close(),
        ),
    )

    exit_code = main(
        [
            "desktop",
            "open",
            "--no-browser",
            "--port",
            "3778",
            "--static-root",
            str(_desktop_static_root(tmp_path)),
        ]
    )

    assert exit_code == 0
    assert launcher_calls == [("127.0.0.1", 3778, "http://127.0.0.1:3778/openevo", False)]


def test_cli_desktop_open_falls_back_when_preferred_port_is_occupied(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import socket

    launcher_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    monkeypatch.setattr("openevo.cli.create_sidecar_app", lambda **kwargs: FakeApp())
    monkeypatch.setattr(
        "openevo.cli.create_desktop_app",
        lambda app, *, static_root=None: app,
    )
    monkeypatch.setattr(
        "openevo.cli._run_desktop_open_server",
        lambda app, *, host, port, sock, url, open_browser: (
            launcher_calls.append((host, port, sock.getsockname()[1], url, open_browser)),
            sock.close(),
        ),
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        occupied_port = occupied.getsockname()[1]

        exit_code = main(
            [
                "desktop",
                "open",
                "--port",
                str(occupied_port),
                "--static-root",
                str(_desktop_static_root(tmp_path)),
            ]
        )

    assert exit_code == 0
    host, port, socket_port, url, open_browser = launcher_calls[0]
    assert host == "127.0.0.1"
    assert port == socket_port
    assert port != occupied_port
    assert url == f"http://127.0.0.1:{port}/openevo"
    assert open_browser is True


def test_cli_desktop_open_port_zero_uses_reportable_ephemeral_port(
    tmp_path: Path,
    monkeypatch,
) -> None:
    launcher_calls = []

    class FakeApp:
        title = "OpenEvo Desktop Sidecar"

    monkeypatch.setattr("openevo.cli.create_sidecar_app", lambda **kwargs: FakeApp())
    monkeypatch.setattr(
        "openevo.cli.create_desktop_app",
        lambda app, *, static_root=None: app,
    )
    monkeypatch.setattr(
        "openevo.cli._run_desktop_open_server",
        lambda app, *, host, port, sock, url, open_browser: (
            launcher_calls.append((port, sock.getsockname()[1], url)),
            sock.close(),
        ),
    )

    exit_code = main(
        [
            "desktop",
            "open",
            "--port",
            "0",
            "--static-root",
            str(_desktop_static_root(tmp_path)),
        ]
    )

    assert exit_code == 0
    port, socket_port, url = launcher_calls[0]
    assert port == socket_port
    assert port > 0
    assert url == f"http://127.0.0.1:{port}/openevo"


def test_run_desktop_open_server_opens_browser_after_port_readiness(monkeypatch) -> None:
    events = []

    class FakeServer:
        should_exit = False

    class FakeThread:
        def is_alive(self) -> bool:
            return True

    fake_server = FakeServer()
    fake_thread = FakeThread()

    monkeypatch.setattr(
        "openevo.cli._create_uvicorn_server",
        lambda app, *, host, port: fake_server,
    )
    monkeypatch.setattr(
        "openevo.cli._start_uvicorn_thread",
        lambda server, *, sock: events.append("start") or fake_thread,
    )
    monkeypatch.setattr(
        "openevo.cli._wait_for_desktop_url",
        lambda *, url, thread: events.append("ready"),
    )
    monkeypatch.setattr("openevo.cli._open_desktop_url", lambda url: events.append("open"))
    monkeypatch.setattr(
        "openevo.cli._join_desktop_server_thread",
        lambda thread, timeout=None: events.append("join"),
    )

    openevo_cli._run_desktop_open_server(
        object(),
        host="127.0.0.1",
        port=3766,
        sock=object(),
        url="http://127.0.0.1:3766/openevo",
        open_browser=True,
    )

    assert events == ["start", "ready", "open", "join"]


def test_desktop_open_http_probe_requires_http_response() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        assert not openevo_cli._can_fetch_desktop_url(
            f"http://127.0.0.1:{port}/openevo"
        )


def test_desktop_open_url_uses_loopback_for_wildcard_hosts() -> None:
    assert openevo_cli._desktop_url(host="0.0.0.0", port=3766) == (
        "http://127.0.0.1:3766/openevo"
    )
    assert openevo_cli._desktop_url(host="::", port=3766) == (
        "http://[::1]:3766/openevo"
    )


def test_pyproject_packages_openevo_desktop_web_assets() -> None:
    payload = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    package_data = payload["tool"]["setuptools"]["package-data"]

    assert "openevo" in package_data
    assert "desktop/web/**/*" in package_data["openevo"]


def test_pyproject_uses_openevo_release_metadata() -> None:
    payload = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    project = payload["project"]

    assert project["name"] == "openevo"
    assert project["description"] == (
        "OpenEvo Desktop and agent evolution orchestration."
    )
    assert project["urls"]["Homepage"] == "https://github.com/CompLifeLab-ZJU/OpenEvo"
    assert project["urls"]["Repository"] == (
        "https://github.com/CompLifeLab-ZJU/OpenEvo"
    )
    assert project["urls"]["Issues"] == (
        "https://github.com/CompLifeLab-ZJU/OpenEvo/issues"
    )


def test_pyproject_keeps_openevo_and_polar_console_scripts() -> None:
    payload = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    scripts = payload["project"]["scripts"]

    assert scripts["openevo"] == "openevo.cli:main"
    assert scripts["polar"] == "polar.cli:main"
    assert scripts["polar-evolution"] == "polar_evolution.cli:main"


def test_uv_lock_uses_openevo_editable_root_package() -> None:
    payload = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))

    editable_roots = [
        package
        for package in payload["package"]
        if package.get("source") == {"editable": "."}
    ]

    assert [package["name"] for package in editable_roots] == ["openevo"]


def test_web_package_defines_openevo_desktop_build_mode() -> None:
    package = json.loads(Path("web/package.json").read_text(encoding="utf-8"))
    env_file = Path("web/.env.openevo-desktop")

    assert package["scripts"]["build:openevo"] == "vite build --mode openevo-desktop"
    assert env_file.read_text(encoding="utf-8").strip() == (
        "VITE_OPENEVO_DESKTOP_ONLY=true"
    )
