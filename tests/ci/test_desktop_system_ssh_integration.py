from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shlex
import sys
from types import ModuleType

import pytest

from openevo.deployment import SystemOpenSshAliasProfile
from openevo.deployment.ssh import (
    build_system_openssh_command_argv,
    build_system_openssh_control_argv,
    build_system_openssh_core_tunnel_argv,
    build_system_openssh_master_argv,
    build_system_openssh_upload_argv,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "run_desktop_system_ssh_integration.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "openevo-desktop-candidate.yml"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_desktop_system_ssh_integration",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


integration = _load_script()


def test_substrate_is_darwin_only_and_requires_exact_system_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(integration.IntegrationError, match="unsupported_platform"):
        integration.verify_substrate(platform_name="linux")

    monkeypatch.setattr(
        integration,
        "_verified_tool",
        lambda path: None if path == "/usr/sbin/sshd" else Path(path),
    )
    with pytest.raises(integration.IntegrationError, match="substrate_unavailable"):
        integration.verify_substrate(platform_name="darwin")


def test_fixture_configs_bind_loopback_current_user_and_literal_aliases(
    tmp_path: Path,
) -> None:
    paths = integration.FixturePaths.for_root(tmp_path)
    topology = integration.FixtureTopology(
        direct_port=42101,
        jump_port=42102,
        core_port=42103,
        user="fixture-user",
    )

    server = integration.render_sshd_config(
        paths,
        topology,
        port=topology.direct_port,
        host_key=paths.direct_host_key,
        pid_file=paths.direct_pid,
    ).decode("utf-8")
    client = integration.render_ssh_config(paths, topology).decode("utf-8")

    assert "ListenAddress 127.0.0.1" in server
    assert "AllowUsers fixture-user" in server
    assert "AuthorizedKeysFile " + str(paths.authorized_keys) in server
    assert "PermitRootLogin no" in server
    assert "PermitUserEnvironment no" in server
    assert "direct-agent" in client
    assert "identity-file" in client
    assert "encrypted-identity" in client
    assert "proxy-jump" in client
    assert "proxy-command" in client
    assert "password-only" in client
    assert "HostName 127.0.0.1" in client
    assert "ProxyJump fixture-jump" in client
    assert str(paths.proxy_command) in client
    assert "Password secret" not in client


def test_integration_plan_delegates_to_production_alias_builders(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = SystemOpenSshAliasProfile(
        profile_id="fixture-profile",
        ssh_host_alias="proxy-jump",
    )
    # Darwin's pytest temp root can itself exceed the Unix socket path budget.
    # The production sidecar also allocates this authority from a short root.
    control_path = Path("/tmp/openevo-system-ssh-integration-m")

    plan = integration.build_production_plan(
        profile,
        control_path=control_path,
        upload_root=workspace,
        remote_upload_root="/tmp/openevo-system-ssh-upload",
        core_port=42103,
    )

    assert plan.master == build_system_openssh_master_argv(
        profile,
        control_path=control_path,
    )
    assert plan.command == build_system_openssh_command_argv(
        profile,
        control_path=control_path,
        remote_command=integration.REMOTE_COMMAND,
    )
    assert plan.upload == build_system_openssh_upload_argv(
        profile,
        control_path=control_path,
        local_path=workspace,
        remote_path="/tmp/openevo-system-ssh-upload",
    )
    assert plan.tunnel == build_system_openssh_core_tunnel_argv(
        profile,
        control_path=control_path,
        remote_port=42103,
    )
    assert plan.exit_master == build_system_openssh_control_argv(
        profile,
        control_path=control_path,
        operation="exit",
    )

    original = integration.ProductionSshPlan(
        master=list(plan.master),
        command=list(plan.command),
        upload=list(plan.upload),
        tunnel=list(plan.tunnel),
        exit_master=list(plan.exit_master),
    )
    fixture_config = tmp_path / "fixture-config"
    bound = integration._bind_fixture_config(plan, fixture_config)

    for before, after in (
        (plan.master, bound.master),
        (plan.command, bound.command),
        (plan.tunnel, bound.tunnel),
        (plan.exit_master, bound.exit_master),
    ):
        assert after == [before[0], "-F", str(fixture_config), *before[1:]]
    remote_shell = shlex.split(bound.upload[bound.upload.index("-e") + 1])
    original_shell = shlex.split(plan.upload[plan.upload.index("-e") + 1])
    assert remote_shell == [
        original_shell[0],
        "-F",
        str(fixture_config),
        *original_shell[1:],
    ]
    assert plan == original


def test_evidence_is_closed_and_contains_no_fixture_path_or_secret() -> None:
    evidence = integration.IntegrationEvidence.complete(
        password_result="prompt_rejected",
    )
    payload = json.loads(evidence.to_json())
    encoded = evidence.to_json()

    assert set(payload) == {
        "checks",
        "password_result",
        "platform",
        "schema_version",
        "status",
    }
    assert payload["status"] == "passed"
    assert payload["password_result"] == "prompt_rejected"
    assert all(payload["checks"].values())
    assert "/Users/" not in encoded
    assert "/tmp/" not in encoded
    assert "passphrase" not in encoded.casefold()


def test_fixture_cleanup_never_removes_a_replacement_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    root = home / ".oe-ssh-integration-owned"
    root.mkdir(mode=0o700)
    identity = integration._private_root_identity(root)
    original = home / "original"
    root.rename(original)
    root.mkdir(mode=0o700)
    marker = root / "do-not-delete"
    marker.write_text("replacement", encoding="utf-8")

    with pytest.raises(integration.IntegrationError, match="fixture_root_authority_changed"):
        integration._remove_fixture_root(root, home=home, identity=identity)

    assert marker.read_text(encoding="utf-8") == "replacement"


def test_structural_check_is_cross_platform_and_closed(capsys: pytest.CaptureFixture[str]) -> None:
    assert integration.main(["--structural-check"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "checks": integration.IntegrationEvidence.required_checks(),
        "platform": "structural",
        "schema_version": 1,
        "status": "ready",
    }


def test_runtime_failure_output_never_exposes_raw_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        integration,
        "run_integration",
        lambda: (_ for _ in ()).throw(OSError("/Users/private/.ssh/id_secret")),
    )

    assert integration.main(["--require-complete"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "desktop_system_ssh_integration_failed:unexpected_failure\n"
    assert "/Users/" not in captured.err


def test_candidate_workflow_runs_required_real_system_ssh_gate() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "tests/ci/test_desktop_system_ssh_integration.py" in workflow
    assert "tests/deployment/test_remote_home.py" in workflow
    assert "tests/openevo/remote/test_system_host_keys.py" in workflow
    assert "tests/openevo/sidecar/test_askpass_broker.py" in workflow
    assert "tests/openevo/sidecar/test_system_ssh_session.py" in workflow
    assert "scripts/ci/run_desktop_system_ssh_integration.py" in workflow
    assert "--require-complete" in workflow
    assert "continue-on-error" not in workflow
