from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
import pytest

from desktop.sidecar.contracts.v2.app import create_desktop_local_v2_contract_app
from desktop.sidecar.release_provider import ConfiguredSshHostCatalogProviderV2
from desktop.sidecar.ssh_config_catalog import (
    OpenSshCatalogBudgets,
    OpenSshHostCatalogLoader,
    SshManualAliasError,
    validate_manual_ssh_alias,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _codes(scan) -> set[str]:
    return {warning.code for warning in scan.warnings}


def test_catalog_discovers_literal_hosts_quoted_tokens_and_static_includes(
    tmp_path: Path,
) -> None:
    ssh_dir = tmp_path / ".ssh"
    absolute = tmp_path / "absolute.conf"
    _write(
        ssh_dir / "config",
        f"""
        Include conf.d/*.conf "quoted include.conf" {absolute}
        Host alpha beta "quoted-alias" # the rest is a comment
          HostName should-never-be-returned.example
        Host duplicate
        Host duplicate
        """,
    )
    _write(ssh_dir / "conf.d" / "20-second.conf", "Host include-z\n")
    _write(ssh_dir / "conf.d" / "10-first.conf", "Host include-a\n")
    _write(ssh_dir / "quoted include.conf", "Host quoted-include\n")
    _write(absolute, "Host absolute-include\n")

    scan = OpenSshHostCatalogLoader(
        config_path=ssh_dir / "config",
        user_ssh_dir=ssh_dir,
    ).scan()

    assert [host.ssh_host_alias for host in scan.hosts] == [
        "absolute-include",
        "alpha",
        "beta",
        "duplicate",
        "include-a",
        "include-z",
        "quoted-alias",
        "quoted-include",
    ]
    by_alias = {host.ssh_host_alias: host.source_kind for host in scan.hosts}
    assert by_alias["alpha"] == "literal_host"
    assert by_alias["include-a"] == "static_include"
    assert scan.warnings == ()
    serialized = json.dumps(scan.to_safe_dict(), sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "should-never-be-returned" not in serialized
    assert "source_path" not in serialized


def test_catalog_handles_include_cycles_without_replaying_files(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    _write(ssh_dir / "config", "Include a.conf\nHost root\n")
    _write(ssh_dir / "a.conf", "Include b.conf\nHost from-a\n")
    _write(ssh_dir / "b.conf", "Include a.conf\nHost from-b\n")

    scan = OpenSshHostCatalogLoader(
        config_path=ssh_dir / "config",
        user_ssh_dir=ssh_dir,
    ).scan()

    assert [host.ssh_host_alias for host in scan.hosts] == [
        "from-a",
        "from-b",
        "root",
    ]
    assert _codes(scan) == {"include_cycle_skipped"}


def test_catalog_keeps_literal_tokens_but_warns_for_dynamic_and_conditional_hosts(
    tmp_path: Path,
) -> None:
    ssh_dir = tmp_path / ".ssh"
    _write(
        ssh_dir / "config",
        """
        Host literal *.corp !blocked host?
        Match exec "printf should-not-run"
          Include conditional.conf
        Host after-match
        """,
    )
    _write(ssh_dir / "conditional.conf", "Host hidden-conditional\n")

    scan = OpenSshHostCatalogLoader(
        config_path=ssh_dir / "config",
        user_ssh_dir=ssh_dir,
    ).scan()

    assert [host.ssh_host_alias for host in scan.hosts] == ["after-match", "literal"]
    assert _codes(scan) == {
        "conditional_hosts_not_enumerated",
        "dynamic_hosts_not_enumerated",
    }


def test_catalog_load_never_invokes_ssh_external_commands_or_a_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssh_dir = tmp_path / ".ssh"
    _write(
        ssh_dir / "config",
        """
        Match exec "touch /tmp/must-not-exist"
        Host safe
          ProxyCommand shell-that-must-not-run %h
        """,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("catalog discovery invoked an external command")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "check_output", forbidden)

    scan = OpenSshHostCatalogLoader(
        config_path=ssh_dir / "config",
        user_ssh_dir=ssh_dir,
    ).scan()

    assert [host.ssh_host_alias for host in scan.hosts] == ["safe"]


def test_catalog_budgets_return_a_safe_partial_catalog(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    _write(
        ssh_dir / "config",
        "Include conf.d/*.conf\nHost one two three\n" + ("x" * 80) + "\nHost tail\n",
    )
    _write(ssh_dir / "conf.d" / "a.conf", "Host included-a\n")
    _write(ssh_dir / "conf.d" / "b.conf", "Host included-b\n")
    _write(ssh_dir / "conf.d" / "c.conf", "Host included-c\n")

    scan = OpenSshHostCatalogLoader(
        config_path=ssh_dir / "config",
        user_ssh_dir=ssh_dir,
        budgets=OpenSshCatalogBudgets(
            max_files=3,
            max_total_bytes=4_096,
            max_file_bytes=2_048,
            max_include_depth=4,
            max_glob_matches=8,
            max_line_bytes=32,
            max_aliases=2,
            max_include_patterns=8,
        ),
    ).scan()

    assert len(scan.hosts) <= 2
    assert "catalog_budget_exhausted" in _codes(scan)
    assert all(len(warning.model_dump_json()) < 512 for warning in scan.warnings)


def test_catalog_skips_oversized_unreadable_and_hostile_utf8_includes(
    tmp_path: Path,
) -> None:
    ssh_dir = tmp_path / ".ssh"
    _write(
        ssh_dir / "config",
        "Include oversized.conf missing.conf hostile.conf\nHost retained\n",
    )
    (ssh_dir / "oversized.conf").write_bytes(b"Host " + (b"x" * 300))
    (ssh_dir / "hostile.conf").write_bytes(
        b"Host before-invalid\nHost \xff\xfe\nHost after-invalid\n"
    )

    scan = OpenSshHostCatalogLoader(
        config_path=ssh_dir / "config",
        user_ssh_dir=ssh_dir,
        budgets=OpenSshCatalogBudgets(max_file_bytes=128),
    ).scan()

    assert [host.ssh_host_alias for host in scan.hosts] == [
        "after-invalid",
        "before-invalid",
        "retained",
    ]
    assert _codes(scan) == {
        "catalog_budget_exhausted",
        "include_unreadable",
        "invalid_config_text_skipped",
    }


@pytest.mark.parametrize(
    "alias",
    [
        "evolab",
        "gpu-lab.example",
        "alias_01",
    ],
)
def test_manual_alias_validation_accepts_only_bounded_literals(alias: str) -> None:
    assert validate_manual_ssh_alias(alias) == alias


@pytest.mark.parametrize(
    "alias",
    [
        "",
        "-oProxyCommand=bad",
        "user@host",
        "ssh://host",
        "host name",
        "host*",
        "host?",
        "!host",
        "../host",
        "x" * 129,
    ],
)
def test_manual_alias_validation_rejects_non_literals(alias: str) -> None:
    with pytest.raises(SshManualAliasError):
        validate_manual_ssh_alias(alias)


def test_authenticated_v2_catalog_provider_has_stable_idempotent_generations(
    tmp_path: Path,
) -> None:
    ssh_dir = tmp_path / ".ssh"
    config = ssh_dir / "config"
    _write(config, "Host alpha\n")
    moments = iter(
        [
            datetime(2026, 7, 23, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 23, 0, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 23, 0, 0, 2, tzinfo=timezone.utc),
        ]
    )
    provider = ConfiguredSshHostCatalogProviderV2(
        OpenSshHostCatalogLoader(config_path=config, user_ssh_dir=ssh_dir),
        clock=lambda: next(moments),
    )
    client = TestClient(create_desktop_local_v2_contract_app(provider=provider))
    auth = {"X-OpenEvo-Desktop-Session": "test-session"}

    assert client.get("/desktop/v2/ssh-hosts").status_code in {401, 403}
    first = client.get("/desktop/v2/ssh-hosts", headers=auth)
    assert first.status_code == 200
    assert first.json()["catalog_generation"] == 1
    assert [item["ssh_host_alias"] for item in first.json()["hosts"]] == ["alpha"]

    action_headers = {
        **auth,
        "X-OpenEvo-Resource-Generation": "1",
        "Idempotency-Key": "catalog-rescan-action-0001",
    }
    unchanged = client.post(
        "/desktop/v2/ssh-hosts/rescan",
        headers=action_headers,
        json={"schema_version": "2"},
    )
    assert unchanged.status_code == 202
    assert unchanged.json()["catalog_generation"] == 1

    exact_retry = client.post(
        "/desktop/v2/ssh-hosts/rescan",
        headers=action_headers,
        json={"schema_version": "2"},
    )
    assert exact_retry.status_code == 202
    assert exact_retry.json() == unchanged.json()

    _write(config, "Host alpha beta\n")
    changed = client.post(
        "/desktop/v2/ssh-hosts/rescan",
        headers={
            **auth,
            "X-OpenEvo-Resource-Generation": "1",
            "Idempotency-Key": "catalog-rescan-action-0002",
        },
        json={"schema_version": "2"},
    )
    assert changed.status_code == 202
    assert changed.json()["catalog_generation"] == 2
    assert [item["ssh_host_alias"] for item in changed.json()["hosts"]] == [
        "alpha",
        "beta",
    ]

    stale = client.post(
        "/desktop/v2/ssh-hosts/rescan",
        headers={
            **auth,
            "X-OpenEvo-Resource-Generation": "1",
            "Idempotency-Key": "catalog-rescan-action-0003",
        },
        json={"schema_version": "2"},
    )
    assert stale.status_code == 412
    assert stale.json()["code"] == "ssh_catalog_generation_changed"
