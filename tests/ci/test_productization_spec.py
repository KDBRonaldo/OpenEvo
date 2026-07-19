from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTIZATION = REPO_ROOT / "docs" / "maintainer" / "productization"
SPEC = PRODUCTIZATION / "spec.md"
PLAN = PRODUCTIZATION / "implementation-plan.md"
README = PRODUCTIZATION / "README.md"
RELEASE_PROCESS = REPO_ROOT / "docs" / "maintainer" / "release-process.md"
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


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_productization_has_one_canonical_target_spec() -> None:
    spec = _text(SPEC)
    plan = _text(PLAN)
    readme = _text(README)

    assert not (PRODUCTIZATION / REMOVED_SPEC_NAME).exists()
    assert "canonical target and release-acceptance specification" in spec
    assert "only canonical product specification" in readme
    assert "non-normative execution tracker" in readme
    assert "Canonical design: `docs/maintainer/productization/spec.md`" in plan
    assert "not a second specification" in plan

    for path in PRODUCTIZATION.glob("*.md"):
        if path == SPEC:
            continue
        text = _text(path)
        assert "Status: canonical product" not in text
        assert "Status: canonical target" not in text


def test_spec_defines_the_desktop_daemon_product_boundary() -> None:
    spec = _text(SPEC)
    readme = _text(REPO_ROOT / "README.md")
    for marker in (
        "OpenEvo Desktop Client",
        "**OpenEvo Daemon**",
        "`src/openevo/` is the shared Core implementation",
        "private Desktop sidecar",
        "Benchmark automation is maintainer tooling",
        "public CLI, Dev Kit, PyPI install path",
        "bypasses the harness and calls a model API directly",
        "OPENEVO_*",
        "/openevo/session",
    ):
        assert marker in spec

    assert "**Queued Task Request**" not in spec

    assert "OpenEvo Core Backend" not in spec
    assert "Polar" not in spec
    assert "Polar" not in _text(PLAN)
    assert "**OpenEvo Daemon**" in readme
    assert "**OpenEvo Core Backend**" not in readme


def test_spec_closes_the_science_task_and_workspace_state_model() -> None:
    spec = " ".join(_text(SPEC).split())
    for marker in (
        "**Task Draft**",
        "**Task**",
        "**Run Attempt**",
        "**Workspace Snapshot**",
        "**Evolution Revision**",
        "**Runtime Context Snapshot**",
        "**Project Head**",
        "distinct opaque identity types",
        "one linear active head",
        "typed not-ready response",
        "first authoritative completed attempt",
        "Restore never rewinds or forks",
        "settings-only successor head",
        "immutable execution receipt",
        "cancellation racing",
        "Creating a replacement plan is one atomic compare-and-set",
    ):
        assert marker in spec


def test_spec_supports_only_codex_harness_execution_modes() -> None:
    spec = _text(SPEC)
    for marker in (
        "Codex Subscription",
        "Self-Deployed",
        "Codex -> Core Gateway -> vLLM",
        "There is no third arbitrary API-key-and-base-URL execution mode",
        "Every subscription run MUST explicitly enable transcript capture",
        "token-level metrics are unavailable",
        "An arbitrary unvalidated model ID is not part",
    ):
        assert marker in spec


def test_spec_preserves_pluggable_evolution_and_protected_methods() -> None:
    spec = _text(SPEC)
    for marker in (
        "startup-frozen verified registry",
        "Core-owned selection resolvers",
        "accepted methods retained",
        "compiler-owned configuration field",
        "data-only handlers",
        "normalized initial evolution intent",
        "text_memory_expel_reflector",
        "skill_bundle_reflector",
        "agent_system_gepa_reflector",
        "10/21",
        "12/25",
        "15/25",
        "successor project head H+1",
        "one transition attempt can commit",
        "authoritative same-candidate no-artifact baseline",
        "Only a baseline score of `0` followed",
    ):
        assert marker in spec

    assert "PLUG-1" not in spec
    assert "REV-1" not in spec


def test_spec_has_complete_release_and_security_acceptance() -> None:
    spec = _text(SPEC)
    gate_ids = tuple(
        re.findall(r"^### (G\d+)\.", spec, flags=re.MULTILINE)
    )
    assert gate_ids == tuple(f"G{number}" for number in range(1, 13))

    for marker in (
        "Apple Silicon OpenEvo Desktop DMG",
        "Linux x86-64 OpenEvo Daemon Bundle",
        "detached final candidate evidence index binds",
        "simulator=false",
        "macOS Keychain",
        "binds only to remote loopback",
        "no analytics, crash, telemetry, or diagnostics upload occurs by default",
        "idempotent Daemon-owned asynchronous operation",
        "PyPI is not an External Beta release surface",
        "raw subscription credential",
        "non-public draft or staging release",
        "no greater than 120 seconds",
        "`not_applicable`",
        "`cn-mainland-restricted-v1`",
        "machine-controlled phase deadlines",
        "one non-circular DAG",
        "detached G12 attestation",
        "detached final candidate evidence index",
    ):
        assert marker in spec

    g12 = spec.split("### G12. Release Consistency", maxsplit=1)[1].split(
        "## 16. Explicitly Out Of Scope", maxsplit=1
    )[0]
    normalized_g12 = " ".join(g12.split())
    assert (
        "final candidate evidence index is generated only after this gate ends"
        in normalized_g12
    )
    assert "final candidate evidence index validates" not in normalized_g12


def test_release_process_matches_final_inventory_and_non_circular_order() -> None:
    process = _text(RELEASE_PROCESS)
    normalized = " ".join(process.split())
    for marker in (
        "Apple Silicon OpenEvo Desktop DMG",
        "source tag or source archive",
        "supported-environment and known-limitation statements",
        "detached G12 attestation",
        "detached final candidate evidence index",
        "current packaging workflow still emits a Core wheel",
    ):
        assert marker in normalized

    assert normalized.index(
        "Build and freeze the self-contained Daemon Bundle"
    ) < normalized.index(
        "Run G1-G11 against those exact frozen bytes"
    )
    assert normalized.index(
        "Run G1-G11 against those exact frozen bytes"
    ) < normalized.index(
        "emit the detached G12 attestation"
    )


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
        "Critical Contract Migration",
        "distinct opaque Project Head",
    ):
        assert marker in plan

    combined = _text(SPEC) + plan
    for removed_model in RETIRED_MARKERS[1:]:
        assert removed_model not in combined
    assert "EXT-1" not in combined
    assert "GAP-1" not in combined
    for duplicated_performance_value in ("12/21", "14/25", "17/25"):
        assert duplicated_performance_value not in plan


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
