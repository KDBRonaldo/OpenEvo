from __future__ import annotations

import json
from pathlib import Path
import re


GATE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "openevo_terminal_bench"
    / "data"
    / "gates"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _load(name: str) -> dict[str, object]:
    return json.loads((GATE_ROOT / name).read_text(encoding="utf-8"))


def test_frozen_gate_profiles_record_sources_and_historical_thresholds_only() -> None:
    baseline = _load("baseline_failed_tasks.json")
    text_memory = _load("text_memory.json")
    skill_bundle = _load("skill_bundle.json")
    agent_system = _load("agent_system.json")

    assert len(baseline["tasks"]) == 25
    assert len(set(baseline["tasks"])) == 25
    assert baseline["source"] == "docs/dev/tb21_codex_gpt55_failed_tasks.md#failed-task-list"
    baseline_source = (
        REPOSITORY_ROOT / "docs" / "dev" / "tb21_codex_gpt55_failed_tasks.md"
    ).read_text(encoding="utf-8")
    baseline_block = baseline_source.split("## Failed Task List", maxsplit=1)[1]
    baseline_block = baseline_block.split("```text", maxsplit=1)[1].split("```", maxsplit=1)[0]
    assert baseline["tasks"] == baseline_block.split()

    assert len(text_memory["tasks"]) == 21
    assert len(set(text_memory["tasks"])) == 21
    assert text_memory["task_source"] == (
        "docs/dev/terminal-bench-memory-eval.md#live-evidence-so-far"
    )
    memory_source = (
        REPOSITORY_ROOT / "docs" / "dev" / "terminal-bench-memory-eval.md"
    ).read_text(encoding="utf-8")
    live_evidence = memory_source.split("## Live Evidence So Far", maxsplit=1)[1]
    live_evidence = live_evidence.split("## Parametric Memory", maxsplit=1)[0]
    assert text_memory["tasks"] == re.findall(
        r"^- `([^`]+)`: baseline reward",
        live_evidence,
        flags=re.MULTILINE,
    )

    expected = {
        "text_memory.json": ("text_memory_expel_reflector", 12, 21, 1),
        "skill_bundle.json": ("skill_bundle_reflector", 14, 25, 2),
        "agent_system.json": ("agent_system_gepa_reflector", 17, 25, 1),
    }
    for name, (method, minimum, denominator, schema_version) in expected.items():
        profile = _load(name)
        assert profile["schema_version"] == schema_version
        assert profile["benchmark"] == "Terminal Bench 2.1"
        assert profile["harness"] == "Codex subscription"
        assert profile["model"] == "gpt-5.5"
        assert profile["method"] == method
        assert profile["historical_threshold"] == {
            "metric": "pass_at_1_rescue_count",
            "minimum": minimum,
            "denominator": denominator,
        }
        assert profile["status"] == "historical_configuration_only"
        assert profile["executed_by_this_change"] is False
        assert profile["proves_release_gate"] is False
        assert "results" not in profile

    assert skill_bundle["task_manifest"] == "baseline_failed_tasks.json"
    assert set(skill_bundle) == {
        "schema_version",
        "profile_id",
        "status",
        "benchmark",
        "harness",
        "model",
        "method",
        "task_manifest",
        "historical_threshold",
        "evidence",
        "executed_by_this_change",
        "proves_release_gate",
    }
    assert skill_bundle["evidence"] == {
        "status": "unverified_historical_aggregate",
        "source": "user_provided",
        "aggregate": {
            "metric": "pass_at_1_rescue_count",
            "rescued": 14,
            "denominator": 25,
        },
        "per_task_evidence_status": "unavailable",
        "gate_execution_status": "not_executed_by_this_change",
    }
    assert agent_system["task_manifest"] == "baseline_failed_tasks.json"
