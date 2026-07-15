from __future__ import annotations

import importlib.util
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

    assert evidence._npm_vulnerabilities(
        {"metadata": {"vulnerabilities": {"high": 2, "critical": 1}}}
    ) == 3
    assert evidence._pip_vulnerabilities(
        {
            "dependencies": [
                {"name": "one", "vulns": [{"id": "A"}]},
                {"name": "two", "vulns": []},
            ]
        }
    ) == 1
    assert evidence._cargo_vulnerabilities(
        {"vulnerabilities": {"found": True, "list": [{"advisory": {"id": "RUSTSEC"}}]}}
    ) == 1
    assert evidence._cargo_vulnerabilities({"vulnerabilities": {"found": False}}) == 0


def test_audit_parsers_fail_closed_on_missing_totals() -> None:
    evidence = _load_module()

    with pytest.raises(evidence.EvidenceError):
        evidence._npm_vulnerabilities({})
    with pytest.raises(evidence.EvidenceError):
        evidence._pip_vulnerabilities({"dependencies": [{"name": "missing-vulns"}]})
    with pytest.raises(evidence.EvidenceError):
        evidence._cargo_vulnerabilities({"vulnerabilities": {}})


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
