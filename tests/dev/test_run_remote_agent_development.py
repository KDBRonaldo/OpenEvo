from __future__ import annotations

import importlib.util
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "dev" / "run_remote_agent_development.py"
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


def test_web_layer_is_an_explicit_opt_in() -> None:
    regular = remote_launcher.parse_args(["--host", "example.com", "--user", "root"])
    web = remote_launcher.parse_args(["--host", "example.com", "--user", "root", "--web-layer"])

    assert regular.web_layer is False
    assert web.web_layer is True
    assert web.web_port == 8766


def test_self_hosted_webui_is_an_explicit_opt_in() -> None:
    args = remote_launcher.parse_args(
        ["--host", "example.com", "--user", "root", "--self-hosted-webui"]
    )

    assert args.self_hosted_webui is True
    assert args.remote_web_port == 8788


@pytest.mark.parametrize("action", ["auto", "install", "update", "start"])
def test_source_delivery_actions_are_explicit(action: str) -> None:
    args = remote_launcher.parse_args(
        [
            "--host",
            "example.com",
            "--user",
            "root",
            "--source-action",
            action,
        ]
    )

    assert args.source_action == action


def test_browser_e2e_is_an_explicit_self_hosted_acceptance_mode() -> None:
    args = remote_launcher.parse_args(
        [
            "--host",
            "example.com",
            "--user",
            "root",
            "--self-hosted-webui",
            "--browser-e2e",
        ]
    )

    assert args.self_hosted_webui is True
    assert args.browser_e2e is True


@pytest.mark.parametrize("flag", ["--status", "--logs", "--stop"])
def test_remote_lifecycle_actions_are_explicit_and_do_not_deploy(flag: str) -> None:
    args = remote_launcher.parse_args(["--host", "example.com", "--user", "root", flag])

    assert getattr(args, flag.removeprefix("--")) is True
    assert args.deploy_only is False
    assert args.browser_e2e is False


@pytest.mark.parametrize("action", ["status", "logs", "stop"])
def test_remote_lifecycle_scripts_are_bounded_and_shell_valid(action: str) -> None:
    script = remote_launcher.build_remote_lifecycle_script(
        action=action,
        tail_lines=37,
    )

    assert 'state_root="$HOME/.openevo/dev-agent"' in script
    if action != "logs":
        assert "openevo.daemon.product_app" in script
        assert "scripts/dev/live_agent_daemon.py" in script
        assert "scripts/dev/development_agent_web_layer.py" in script
    if action == "logs":
        assert "tail -n 37" in script
    if action == "stop":
        assert "refusing to signal PID" in script
        assert script.index('stop_managed_process "web-layer"') < script.index(
            'stop_managed_process "daemon"'
        )

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


@pytest.mark.parametrize("tail", [0, 2001])
def test_remote_log_tail_is_bounded(tail: int) -> None:
    with pytest.raises(remote_launcher.LauncherError, match="--tail"):
        remote_launcher.build_remote_lifecycle_script(
            action="logs",
            tail_lines=tail,
        )


def test_status_runs_without_checkout_validation_or_token_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(remote_launcher.shutil, "which", lambda _: "ssh")
    monkeypatch.setattr(
        remote_launcher,
        "validate_checkout_for_deployment",
        lambda **_: pytest.fail("status must not inspect or deploy the checkout"),
    )

    def run_remote(
        ssh_binary: str,
        connection: object,
        script: str,
    ) -> None:
        captured.update(
            ssh_binary=ssh_binary,
            connection=connection,
            script=script,
        )

    monkeypatch.setattr(remote_launcher, "_run_remote", run_remote)

    result = remote_launcher.main(["--host", "example.com", "--user", "root", "--status"])

    assert result == 0
    assert captured["ssh_binary"] == "ssh"
    assert "OpenEvo remote development stack" in str(captured["script"])


def test_port_preflight_rejects_an_existing_launcher_before_deployment() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        with pytest.raises(remote_launcher.LauncherError, match="remote daemon was not changed"):
            remote_launcher.ensure_local_ports_available([port])
    finally:
        listener.close()


def test_port_preflight_accepts_a_free_local_port() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    remote_launcher.ensure_local_ports_available([port])


def test_port_preflight_does_not_treat_a_closed_socket_as_a_listener() -> None:
    previous = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    previous.bind(("127.0.0.1", 0))
    port = previous.getsockname()[1]
    previous.listen()
    previous.close()

    remote_launcher.ensure_local_ports_available([port])


def test_web_layer_allows_only_local_development_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote_launcher,
        "changed_checkout_paths",
        lambda: (
            "desktop/src/product/preview.tsx",
            "scripts/dev/development_agent_web_layer.py",
            "tests/dev/test_development_agent_web_layer.py",
        ),
    )

    assert remote_launcher.validate_checkout_for_deployment(web_layer=True, deploy_only=False)


@pytest.mark.parametrize(
    "changed_path",
    [
        "scripts/dev/live_agent_daemon.py",
        "src/openevo/evolution/framework/builtin_handlers.py",
        "pyproject.toml",
    ],
)
def test_web_layer_rejects_uncommitted_remote_runtime_changes(
    monkeypatch: pytest.MonkeyPatch,
    changed_path: str,
) -> None:
    monkeypatch.setattr(remote_launcher, "changed_checkout_paths", lambda: (changed_path,))

    with pytest.raises(remote_launcher.LauncherError, match="remote daemon"):
        remote_launcher.validate_checkout_for_deployment(web_layer=True, deploy_only=False)


def test_direct_launcher_still_requires_a_clean_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote_launcher,
        "changed_checkout_paths",
        lambda: ("desktop/src/product/preview.tsx",),
    )

    with pytest.raises(remote_launcher.LauncherError, match="remote daemon"):
        remote_launcher.validate_checkout_for_deployment(web_layer=False, deploy_only=False)


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
    assert "ConnectTimeout=15" in command
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


def test_remote_script_quotes_values_and_uses_private_managed_paths() -> None:
    token = "A-token-with-'quote-and-more-than-32-characters"
    script = remote_launcher.build_remote_script(
        branch="feature/desktop-e2e",
        expected_commit="a" * 40,
        source_bundle_sha256="b" * 64,
        token=token,
        remote_port=8787,
        evolution_model="gpt-5.5",
    )

    assert 'state_root="$HOME/.openevo/dev-agent"' in script
    assert 'source_marker="$state_root/managed-source-v1"' in script
    assert 'source_bundle="$state_root/incoming/source-$expected_commit.bundle"' in script
    assert 'bundle verify "$source_bundle"' in script
    assert 'verify_root="$state_root/incoming/bundle-verifier.git"' in script
    assert 'git init --quiet --bare "$verify_root"' in script
    assert 'install_parent="$(mktemp -d "$state_root/incoming/install.XXXXXX")"' in script
    assert "refs/remotes/openevo-local/$branch" in script
    assert 'git -C "$source_root" merge --ff-only' in script
    assert 'git -C "$source_root" remote remove origin' in script
    assert "refusing to run non-committed source" in script
    assert script.index("status --porcelain") < script.index(
        "Installed OpenEvo source already matches"
    )
    assert "fetch origin" not in script
    assert "github.com" not in script
    assert 'deployed_commit="$(git -C "$source_root" rev-parse HEAD)"' in script
    assert '"$HOME/.local/bin/uv"' in script
    assert '"$HOME/.cargo/bin/uv"' in script
    assert script.index('"$HOME/.local/bin/uv"') < script.index("https://astral.sh/uv/install.sh")
    assert 'timeout 300 "$uv_bin" sync --frozen --python 3.11' in script
    assert '"$uv_bin" run --frozen --no-sync --python 3.11 python' in script
    assert 'runtime_marker="$state_root/runtime-commit-v1"' in script
    assert "curl --connect-timeout 15 --max-time 120" in script
    assert "timeout 30 env" in script
    assert "curl --connect-timeout 1 --max-time 2" in script
    assert "bash -lc 'command -v codex'" in script
    assert '"$HOME/.npm-global/bin/codex"' in script
    assert '"$HOME"/.nvm/versions/node/*/bin/codex' in script
    assert '"PATH=$codex_dir:$PATH"' in script
    assert '--codex-binary "$codex_bin"' in script
    assert "-m openevo.daemon.product_app" in script
    assert "*scripts/dev/live_agent_daemon.py*" in script
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


def test_remote_script_can_start_the_unchanged_desktop_ui_beside_daemon() -> None:
    script = remote_launcher.build_remote_script(
        branch="nanobot-webui-architecture",
        expected_commit="a" * 40,
        source_bundle_sha256="b" * 64,
        token="daemon-token",
        remote_port=8787,
        evolution_model="gpt-5.5",
        self_hosted_webui=True,
        web_session_token="b" * 64,
        web_bootstrap_token="c" * 64,
        remote_web_port=8788,
        browser_endpoint="http://127.0.0.1:8765",
    )

    assert "scripts/dev/development_agent_web_layer.py" in script
    assert '--static-root "$source_root/src/openevo/web_gateway/static"' in script
    assert '"OPENEVO_DEV_WEB_SESSION_TOKEN=$web_session_token"' in script
    assert '"OPENEVO_DEV_WEB_BOOTSTRAP_TOKEN=$web_bootstrap_token"' in script
    assert "curl --connect-timeout 1 --max-time 2 --silent --fail" in script
    assert '"http://127.0.0.1:$remote_web_port/openevo"' in script

    shell = shutil.which("sh")
    if shell is not None:
        syntax = subprocess.run(
            [shell, "-n"], input=script, text=True, capture_output=True, check=False
        )
        assert syntax.returncode == 0, syntax.stderr


def test_remote_source_probe_never_accesses_a_git_remote() -> None:
    script = remote_launcher.build_remote_source_probe_script()

    assert 'git -C "$source_root" rev-parse HEAD' in script
    assert "fetch" not in script
    assert "github.com" not in script


@pytest.mark.parametrize(
    ("response", "expected"),
    [("absent\n", None), (f"managed:{'a' * 40}\n", "a" * 40)],
)
def test_remote_source_probe_parses_bounded_receipts(
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    expected: str | None,
) -> None:
    monkeypatch.setattr(
        remote_launcher,
        "_run_remote_capture",
        lambda *_args, **_kwargs: response,
    )
    connection = remote_launcher.SshConnection((), "server", "server")

    assert remote_launcher.probe_remote_source_commit("ssh", connection) == expected


def test_remote_source_probe_rejects_an_unmanaged_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote_launcher,
        "_run_remote_capture",
        lambda *_args, **_kwargs: "unrecognized\n",
    )
    connection = remote_launcher.SshConnection((), "server", "server")

    with pytest.raises(remote_launcher.LauncherError, match="not owned"):
        remote_launcher.probe_remote_source_commit("ssh", connection)


def test_remote_capture_reports_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["ssh"], timeout=17)

    monkeypatch.setattr(remote_launcher.subprocess, "run", run)
    connection = remote_launcher.SshConnection((), "server", "server")

    with pytest.raises(remote_launcher.LauncherError, match="exceeded 17 seconds"):
        remote_launcher._run_remote_capture(
            "ssh",
            connection,
            "true",
            timeout=17,
        )


def test_remote_script_can_only_install_source_without_starting_services() -> None:
    script = remote_launcher.build_remote_script(
        branch="stable",
        expected_commit="a" * 40,
        source_bundle_sha256="b" * 64,
        start_services=False,
        token="daemon-token",
        remote_port=8787,
        evolution_model="gpt-5.5",
    )

    assert "start_services=0" in script
    assert "prepare_runtime=1" in script
    assert 'if [ "$start_services" -ne 1 ]; then' in script
    assert script.index('if [ "$start_services" -ne 1 ]; then') > script.index(
        'timeout 300 "$uv_bin" sync --frozen --python 3.11'
    )


def test_remote_start_requires_a_prepared_runtime_and_never_syncs() -> None:
    script = remote_launcher.build_remote_script(
        branch="stable",
        expected_commit="a" * 40,
        prepare_runtime=False,
        token="daemon-token",
        remote_port=8787,
        evolution_model="gpt-5.5",
    )

    assert "prepare_runtime=0" in script
    assert "run --source-action install or update first" in script
    assert '"$uv_bin" run --frozen --no-sync --python 3.11 python' in script


@pytest.mark.parametrize("branch", ["feature/desktop-e2e", "fix/daemon_2", "stable"])
def test_accepts_normal_branch_names(branch: str) -> None:
    assert remote_launcher.validate_branch(branch) == branch


@pytest.mark.parametrize("branch", ["../stable", "branch@{1}", "-bad", "bad/", ""])
def test_rejects_unsafe_branch_names(branch: str) -> None:
    with pytest.raises(remote_launcher.LauncherError):
        remote_launcher.validate_branch(branch)
