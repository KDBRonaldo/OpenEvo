from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


HASH_ONE = "1" * 64
HASH_TWO = "2" * 64
HASH_THREE = "3" * 64


def _hashed_requirement(
    name: str,
    version: str,
    *hashes: str,
    marker: str | None = None,
) -> str:
    requirement = f"{name}=={version}"
    if marker is not None:
        requirement += f" ; {marker}"
    lines = [f"{requirement} \\"]
    for index, digest in enumerate(hashes):
        continuation = " \\" if index < len(hashes) - 1 else ""
        lines.append(f"    --hash=sha256:{digest}{continuation}")
    return "\n".join(lines) + "\n"


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/collect_openevo_release_evidence.py"
    spec = importlib.util.spec_from_file_location("collect_openevo_release_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audit_parsers_count_actionable_vulnerabilities() -> None:
    evidence = _load_module()
    requirements = _hashed_requirement("one", "1.0", HASH_ONE) + _hashed_requirement(
        "two", "2.0", HASH_TWO
    )

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
            _hashed_requirement("one", "1.0", HASH_ONE)
            + _hashed_requirement("two", "2.0", HASH_TWO),
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
            _hashed_requirement(
                "openevo",
                "0.1.0",
                HASH_ONE,
                marker='python_version < "0"',
            ),
        ),
        (
            {
                "dependencies": [
                    {"name": "one", "version": "1.0", "vulns": []},
                    {"name": "extra", "version": "1.0", "vulns": []},
                ],
                "fixes": [],
            },
            _hashed_requirement("one", "1.0", HASH_ONE),
        ),
        (
            {
                "dependencies": [
                    {"name": "one", "version": "wrong", "vulns": []},
                ],
                "fixes": [],
            },
            _hashed_requirement("one", "1.0", HASH_ONE),
        ),
        (
            {
                "dependencies": [
                    {"name": "one", "version": "1.0", "vulns": []},
                ],
                "fixes": [{"name": "one"}],
            },
            _hashed_requirement("one", "1.0", HASH_ONE),
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


def test_exported_requirements_validate_every_block_before_evaluating_markers() -> None:
    evidence = _load_module()
    requirements = _hashed_requirement("one", "1.0", HASH_ONE, HASH_TWO)
    requirements += _hashed_requirement(
        "inactive",
        "2.0",
        HASH_THREE,
        marker='python_version < "0"',
    )

    assert evidence._exported_requirements(requirements) == {"one": "1.0"}


@pytest.mark.parametrize(
    ("requirements", "error"),
    [
        ("one==1.0\n", "block"),
        ("one==1.0 \\\n", "hash block is incomplete"),
        ("one>=1.0 \\\n    --hash=sha256:" + HASH_ONE + "\n", "exact version"),
        ("one[extra]==1.0 \\\n    --hash=sha256:" + HASH_ONE + "\n", "exact third-party"),
        (
            "one==1.0 \\\n    --index-url=https://example.invalid/simple\n",
            "invalid option",
        ),
        (
            "one==1.0 \\\n    --hash=sha256:not-a-hash\n",
            "invalid option",
        ),
        (
            _hashed_requirement("one", "1.0", HASH_ONE, HASH_ONE),
            "duplicate hash",
        ),
        (
            _hashed_requirement("one", "1.0", HASH_ONE)
            + _hashed_requirement(
                "One",
                "2.0",
                HASH_TWO,
                marker='python_version < "0"',
            ),
            "duplicate package",
        ),
        (
            _hashed_requirement("one", "1.0", HASH_ONE)
            + _hashed_requirement("two", "2.0", HASH_ONE),
            "duplicate hash",
        ),
    ],
)
def test_exported_requirements_reject_unbound_or_ambiguous_inputs(
    requirements: str,
    error: str,
) -> None:
    evidence = _load_module()

    with pytest.raises(evidence.EvidenceError, match=error):
        evidence._exported_requirements(requirements)


def test_candidate_workflow_pins_runtimes_and_hash_bound_wheel_smokes() -> None:
    workflow = Path(".github/workflows/openevo-desktop-candidate.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.count("runs-on: ubuntu-24.04") == 3
    assert "runs-on: ubuntu-latest" not in workflow
    assert workflow.count('node-version: "22.23.1"') == 2
    assert workflow.count('python-version: "3.11.15"') == 3
    assert workflow.count('python-version: "3.11.9"') == 1
    macos_job = workflow.split("  macos-candidate:\n", maxsplit=1)[1].split(
        "  linux-core-candidate:\n", maxsplit=1
    )[0]
    assert 'python-version: "3.11.9"' in macos_job
    assert 'node-version: "22"' not in workflow
    assert 'python-version: "3.11"' not in workflow
    assert "--no-hashes" not in workflow
    assert "pip install --upgrade pip" not in workflow
    assert workflow.count("--require-hashes") == 3
    assert workflow.count("--requirement candidate-artifacts/python-requirements.txt") == 3
    assert workflow.count("--no-deps") == 2

    macos_job, linux_jobs = workflow.split("  linux-core-candidate:\n", maxsplit=1)
    linux_job = linux_jobs.split("  draft-prerelease-roundtrip:\n", maxsplit=1)[0]
    for job in (macos_job, linux_job):
        dependency_install = job.index(
            "--requirement candidate-artifacts/python-requirements.txt"
        )
        wheel_install = job.index("--no-deps")
        assert dependency_install < wheel_install


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
    requirements.write_text(
        _hashed_requirement("one", "1.0", HASH_ONE),
        encoding="utf-8",
    )
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
