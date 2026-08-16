from __future__ import annotations

from openevo.backend.evolution_runtime import (
    DaemonRuntimeIntent,
    DeclarativeHarnessRuntimeAdapter,
    HarnessRuntimeCapabilities,
    RuntimeControlTranslationError,
    RuntimeControlTranslatorRegistry,
    RuntimeIntentStatus,
    codex_development_runtime_adapter,
)


def test_codex_adapter_reports_active_delegated_and_unsupported_intents() -> None:
    adapter = codex_development_runtime_adapter()

    report = adapter.reconcile([
        {
            "kind": "memory",
            "contract_version": "1",
            "read_timing": "session_start",
            "write_timing": "session_closed",
        },
        {
            "kind": "agent_system",
            "contract_version": "1",
            "spawn_plan": {
                "agents": [{
                    "agent_id": "reviewer",
                    "role": "Reviewer",
                    "instructions": "Review the candidate result.",
                }],
            },
        },
    ])

    decisions = {decision.intent.feature_id: decision for decision in report.decisions}
    assert decisions["memory.read.session_start"].status is RuntimeIntentStatus.ACTIVE
    assert decisions["memory.write.session_closed"].status is RuntimeIntentStatus.DELEGATED
    assert (
        decisions["agent_system.instruction.native_harness_file"].status
        is RuntimeIntentStatus.ACTIVE
    )
    assert (
        decisions["agent_system.spawn.harness_managed"].status
        is RuntimeIntentStatus.UNSUPPORTED
    )
    assert report.fully_supported is False


def test_memory_on_demand_and_manual_write_are_not_silently_claimed() -> None:
    report = codex_development_runtime_adapter().reconcile([{
        "kind": "memory",
        "contract_version": "1",
        "read_timing": "on_demand",
        "write_timing": "manual",
    }])

    assert {
        decision.intent.feature_id: decision.status
        for decision in report.decisions
    } == {
        "memory.read.on_demand": RuntimeIntentStatus.UNSUPPORTED,
        "memory.write.manual": RuntimeIntentStatus.UNSUPPORTED,
    }


def test_unknown_core_contract_fails_closed() -> None:
    adapter = codex_development_runtime_adapter()

    try:
        adapter.reconcile([{"kind": "tools", "contract_version": "1"}])
    except RuntimeControlTranslationError as exc:
        assert "no Daemon translator" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("unknown runtime control was accepted")


def test_future_contract_only_needs_a_translator_and_capability_registration() -> None:
    translators = RuntimeControlTranslatorRegistry()

    def translate_tools(value: object) -> tuple[DaemonRuntimeIntent, ...]:
        assert isinstance(value, dict)
        desired = value.get("desired_capabilities")
        if not isinstance(desired, list) or not all(isinstance(item, str) for item in desired):
            raise ValueError("desired_capabilities must be a string list")
        return tuple(
            DaemonRuntimeIntent(
                intent_id=f"tool-{tool_id}",
                feature_id=f"tools.capability.{tool_id}",
                source_kind="tools",
                source_contract_version="1",
                parameters={},
            )
            for tool_id in desired
        )

    translators.register(
        kind="tools",
        contract_version="1",
        translator=translate_tools,
    )
    adapter = DeclarativeHarnessRuntimeAdapter(
        capabilities=HarnessRuntimeCapabilities(
            adapter_id="test-harness-v1",
            active_features=frozenset({"tools.capability.web_search"}),
            delegated_features={},
        ),
        translators=translators,
    )

    report = adapter.reconcile([{
        "kind": "tools",
        "contract_version": "1",
        "desired_capabilities": ["web_search", "shell"],
    }])

    assert [decision.status for decision in report.decisions] == [
        RuntimeIntentStatus.ACTIVE,
        RuntimeIntentStatus.UNSUPPORTED,
    ]

