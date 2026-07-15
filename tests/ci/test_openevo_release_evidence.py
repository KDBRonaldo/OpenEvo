from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/collect_openevo_release_evidence.py"
    spec = importlib.util.spec_from_file_location("collect_openevo_release_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audit_parsers_count_actionable_vulnerabilities() -> None:
    evidence = _load_module()
    requirements = "one==1.0\ntwo==2.0\n"

    assert evidence._npm_vulnerabilities(
        {"metadata": {"vulnerabilities": {"high": 2, "critical": 1}}}
    ) == 3
    assert evidence._pip_vulnerabilities(
        {
            "dependencies": [
                {"name": "one", "version": "1.0", "vulns": [{"id": "A"}]},
                {"name": "two", "version": "2.0", "vulns": []},
            ],
            "fixes": [],
        },
        requirements,
    ) == (1, 2)
    assert evidence._cargo_vulnerabilities(
        {
            "database": {"advisory-count": 1},
            "lockfile": {"dependency-count": 2},
            "settings": {"ignore": []},
            "vulnerabilities": {
                "found": True,
                "count": 1,
                "list": [{"advisory": {"id": "RUSTSEC"}}],
            },
        }
    ) == 1
    assert evidence._cargo_vulnerabilities(
        {
            "database": {"advisory-count": 1},
            "lockfile": {"dependency-count": 2},
            "settings": {"ignore": []},
            "vulnerabilities": {"found": False, "count": 0, "list": []},
        }
    ) == 0


def test_audit_parsers_fail_closed_on_missing_totals() -> None:
    evidence = _load_module()

    with pytest.raises(evidence.EvidenceError):
        evidence._npm_vulnerabilities({})
    with pytest.raises(evidence.EvidenceError):
        evidence._pip_vulnerabilities(
            {
                "dependencies": [
                    {"name": "one", "version": "1.0", "vulns": []},
                ],
                "fixes": [],
            },
            "one==1.0\ntwo==2.0\n",
        )
    with pytest.raises(evidence.EvidenceError):
        evidence._cargo_vulnerabilities({"vulnerabilities": {}})


def test_cargo_audit_report_rejects_ignored_advisories() -> None:
    evidence = _load_module()

    with pytest.raises(evidence.EvidenceError, match="ignored"):
        evidence._cargo_vulnerabilities(
            {
                "database": {"advisory-count": 1},
                "lockfile": {"dependency-count": 2},
                "settings": {"ignore": ["RUSTSEC-2026-0001"]},
                "vulnerabilities": {"found": False, "count": 0, "list": []},
            }
        )


@pytest.mark.parametrize(
    ("report", "requirements"),
    [
        (
            {
                "dependencies": [
                    {"name": "openevo", "version": "0.1.0", "vulns": []},
                ],
                "fixes": [],
            },
            'openevo==0.1.0 ; python_version < "0"\n',
        ),
        (
            {
                "dependencies": [
                    {"name": "one", "version": "1.0", "vulns": []},
                    {"name": "extra", "version": "1.0", "vulns": []},
                ],
                "fixes": [],
            },
            "one==1.0\n",
        ),
        (
            {
                "dependencies": [
                    {"name": "one", "version": "wrong", "vulns": []},
                ],
                "fixes": [],
            },
            "one==1.0\n",
        ),
        (
            {
                "dependencies": [
                    {"name": "one", "version": "1.0", "vulns": []},
                ],
                "fixes": [{"name": "one"}],
            },
            "one==1.0\n",
        ),
    ],
)
def test_pip_audit_report_must_cover_exact_third_party_export(
    report: object,
    requirements: str,
) -> None:
    evidence = _load_module()

    with pytest.raises(evidence.EvidenceError):
        evidence._pip_vulnerabilities(report, requirements)


def test_collector_binds_pip_audit_to_exported_requirements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _load_module()
    repo = tmp_path / "repo"
    output = tmp_path / "evidence"
    (repo / "desktop/src-tauri").mkdir(parents=True)
    (repo / "uv.lock").write_text("package = []\n", encoding="utf-8")
    (repo / "desktop/package-lock.json").write_text(
        '{"packages":{"":{"name":"root"},"node_modules/one":{"license":"MIT"}}}\n',
        encoding="utf-8",
    )
    (repo / "desktop/src-tauri/Cargo.lock").write_text("lock\n", encoding="utf-8")
    (repo / "LICENSE").write_text("license\n", encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("one==1.0\n", encoding="utf-8")
    npm_audit = tmp_path / "npm.json"
    npm_audit.write_text(
        '{"metadata":{"vulnerabilities":{"high":0,"critical":0}}}\n',
        encoding="utf-8",
    )
    pip_audit = tmp_path / "pip.json"
    pip_audit.write_text(
        json.dumps(
            {
                "dependencies": [{"name": "one", "version": "1.0", "vulns": []}],
                "fixes": [],
            }
        ),
        encoding="utf-8",
    )
    cargo_audit = tmp_path / "cargo.json"
    cargo_audit.write_text(
        '{"database":{"advisory-count":1},'
        '"lockfile":{"dependency-count":1},'
        '"settings":{"ignore":[]},'
        '"vulnerabilities":{"found":false,"count":0,"list":[]}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(evidence, "_cargo_metadata", lambda _repo: [{"license": "MIT"}])
    monkeypatch.setattr(evidence, "_python_license_inventory", lambda: (1, 0))

    evidence.collect_evidence(
        repo,
        output,
        npm_audit=npm_audit,
        pip_audit=pip_audit,
        pip_requirements=requirements,
        cargo_audit=cargo_audit,
    )

    security = json.loads((output / "security-audit.json").read_text(encoding="utf-8"))
    assert security["schema_version"] == 2
    assert security["audits"]["pip-audit"] == {
        "audited_packages": 1,
        "requirements_sha256": evidence._sha256(requirements),
        "status": "passed",
        "vulnerabilities": 0,
    }


def test_npm_license_inventory_requires_every_locked_dependency(tmp_path: Path) -> None:
    evidence = _load_module()
    package_lock = tmp_path / "package-lock.json"
    package_lock.write_text(
        '{"packages":{"":{"name":"root"},'
        '"node_modules/one":{"license":"MIT"},'
        '"node_modules/two":{}}}\n',
        encoding="utf-8",
    )

    assert evidence._npm_license_inventory(package_lock) == (2, 1)
