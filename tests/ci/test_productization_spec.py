from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTIZATION = REPO_ROOT / "docs" / "maintainer" / "productization"
SPEC = PRODUCTIZATION / "spec.md"
PLAN = PRODUCTIZATION / "implementation-plan.md"
REMOVED_SPEC_NAME = "external-beta-release-spec.md"
RETIRED_MARKERS = (
    REMOVED_SPEC_NAME,
    "External Beta Requirement Ledger",
    "Release Readiness Scoreboard",
    "check-evidence.json",
    "draft-release-asset-manifest.json",
    "resolve_release_tag.py",
    "release-facing-docs-manifest.schema.json",
    "science-canary-inputs.schema.json",
    "science-task.schema.json",
    "science-workflow-canary-report.schema.json",
)
PLUG_REQUIREMENTS = tuple(f"PLUG-{number}" for number in range(1, 8))
A2_STEPS = tuple(f"A2.{number}" for number in range(1, 9))


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _bullet_ids(text: str, prefix: str) -> tuple[str, ...]:
    return tuple(
        re.findall(rf"^- `({re.escape(prefix)}-\d+)\b", text, flags=re.MULTILINE)
    )


def _plug_requirement_bodies(text: str) -> dict[str, str]:
    pattern = re.compile(
        r"^- `(PLUG-\d+) [^`]+`: (?P<body>.*?)(?=^- `PLUG-\d+ |\n\n)",
        flags=re.MULTILINE | re.DOTALL,
    )
    return {
        match.group(1): " ".join(match.group("body").split())
        for match in pattern.finditer(text)
    }


def _a2_rows(text: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    rows = re.findall(
        r"^\| `(A2\.\d+)` \| (?P<requirements>[^|]+) \| [^|]+ \|$",
        text,
        flags=re.MULTILINE,
    )
    return tuple(
        (step, tuple(re.findall(r"PLUG-\d+", requirements)))
        for step, requirements in rows
    )


def test_productization_has_one_concise_canonical_spec() -> None:
    spec = _text(SPEC)
    plan = _text(PLAN)

    assert not (PRODUCTIZATION / REMOVED_SPEC_NAME).exists()
    assert "canonical product and release specification" in spec
    assert "Canonical design: `docs/maintainer/productization/spec.md`" in plan
    assert len(spec.splitlines()) <= 650
    assert len(plan.splitlines()) <= 450


def test_spec_preserves_stable_product_boundaries() -> None:
    spec = _text(SPEC)
    for marker in (
        "OpenEvo Core Backend",
        "OpenEvo Desktop",
        "benchmarks/terminal_bench/",
        "CLI or Dev Kit product",
        "call a model API directly",
        "Polar-derived architecture",
        "gateway, rollout, runtime",
        "OPENEVO_*",
        "/openevo/session",
    ):
        assert marker in spec


def test_spec_preserves_validated_methods_and_gates() -> None:
    spec = _text(SPEC)
    expected = {
        "text_memory_expel_reflector": "12/21",
        "skill_bundle_reflector": "14/25",
        "agent_system_gepa_reflector": "17/25",
    }
    for method_id, threshold in expected.items():
        assert method_id in spec
        assert threshold in spec


def test_spec_requires_pluggable_targets_methods_and_registry() -> None:
    spec = _text(SPEC)
    assert _bullet_ids(spec, "PLUG") == PLUG_REQUIREMENTS
    requirement_bodies = _plug_requirement_bodies(spec)
    assert tuple(requirement_bodies) == PLUG_REQUIREMENTS

    plan = _text(PLAN)
    rows = _a2_rows(plan)
    assert tuple(step for step, _ in rows) == A2_STEPS
    mapped_requirements = {requirement for _, requirements in rows for requirement in requirements}
    assert mapped_requirements == set(PLUG_REQUIREMENTS)
    assert dict(rows) == {
        "A2.1": ("PLUG-1", "PLUG-2", "PLUG-3", "PLUG-4", "PLUG-5", "PLUG-6"),
        "A2.2": ("PLUG-2", "PLUG-7"),
        "A2.3": ("PLUG-3", "PLUG-4", "PLUG-7"),
        "A2.4": ("PLUG-2", "PLUG-3", "PLUG-4"),
        "A2.5": ("PLUG-1", "PLUG-2", "PLUG-5"),
        "A2.6": ("PLUG-7",),
        "A2.7": ("PLUG-6",),
        "A2.8": ("PLUG-6", "PLUG-7"),
    }

    ordered_sections = ("### A1.", "### A2.", "### A3.", "## B.", "## C.")
    positions = [plan.index(section) for section in ordered_sections]
    assert positions == sorted(positions)


def test_spec_covers_modes_bootstrap_and_artifact_matrix() -> None:
    combined = _text(SPEC) + _text(PLAN)
    for marker in (
        "Codex Subscription Transcript",
        "Self-Deployed Reference",
        "token_level_metrics_available=false",
        "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "self-deployed-reference-v1.json",
        "HTTP/HTTPS proxy",
        "model download",
        "pre-Core SSH transport/install/start",
        "text_memory",
        "skill_bundle",
        "agent_system",
        "all six mode/family cells",
    ):
        assert marker in combined


def test_spec_covers_release_security_and_privacy_boundaries() -> None:
    spec = _text(SPEC)
    for marker in (
        "unsigned/not-notarized",
        "PyPI is not an External Beta release surface",
        "No analytics, crash reporting, telemetry",
        "bind only to local interfaces",
        "Diagnostics export is explicit",
        "Deletion is path-contained",
        "redacted before persistence",
        "transcript-derived datasets",
        "any part of an\nartifact record",
        "registration requests",
        "URIs, payloads",
        "GitHub Releases",
    ):
        assert marker in spec


def test_plan_has_five_executable_workstreams_without_old_governance_model() -> None:
    plan = _text(PLAN)
    assert tuple(re.findall(r"^## ([A-E])\.", plan, flags=re.MULTILINE)) == (
        "A",
        "B",
        "C",
        "D",
        "E",
    )
    assert "## Immediate Execution Order" in plan

    for marker in (
        "gpt-5.6-sol",
        "ivowang <ziyiwang@ieee.org>",
        "git diff --check",
    ):
        assert marker in plan

    combined = _text(SPEC) + plan
    for removed_model in RETIRED_MARKERS[1:]:
        assert removed_model not in combined
    assert "EXT-1" not in combined
    assert "GAP-1" not in combined


def test_active_files_do_not_reference_retired_productization_contracts() -> None:
    roots = (
        REPO_ROOT / "README.md",
        REPO_ROOT / ".github",
        REPO_ROOT / "docs" / "architecture",
        REPO_ROOT / "docs" / "core",
        REPO_ROOT / "docs" / "maintainer",
        REPO_ROOT / "docs" / "user",
        REPO_ROOT / "scripts",
        REPO_ROOT / "src",
        REPO_ROOT / "tests",
    )
    ignored_roots = (
        REPO_ROOT / "docs" / "maintainer" / "development-history",
        REPO_ROOT / "docs" / "dev",
    )
    offenders: list[str] = []
    for root in roots:
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or any(parent in path.parents for parent in ignored_roots):
                continue
            if path == Path(__file__).resolve():
                continue
            if path.suffix not in {".md", ".py", ".yml", ".yaml", ".toml", ".json"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in RETIRED_MARKERS:
                if marker in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {marker}")

    assert offenders == []
