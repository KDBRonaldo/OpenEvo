"""Daemon-owned reconciliation for declarative evolution runtime controls.

Evolution methods and Core target handlers own the transport contracts.  The
Daemon translates those versioned contracts into stable, harness-independent
feature intents, then reconciles the intents against one concrete harness
adapter.  Unknown contracts are never interpreted as commands.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol


class RuntimeControlTranslationError(ValueError):
    """A Core runtime-control payload cannot be translated safely."""


class RuntimeIntentStatus(StrEnum):
    ACTIVE = "active"
    DELEGATED = "delegated"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class DaemonRuntimeIntent:
    """One closed desired-state feature independent of a Core JSON shape."""

    intent_id: str
    feature_id: str
    source_kind: str
    source_contract_version: str
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.intent_id or not self.feature_id:
            raise ValueError("runtime intent identity must not be empty")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "feature_id": self.feature_id,
            "source_kind": self.source_kind,
            "source_contract_version": self.source_contract_version,
            "parameters": dict(self.parameters),
        }


RuntimeControlTranslator = Callable[[object], tuple[DaemonRuntimeIntent, ...]]


class RuntimeControlTranslatorRegistry:
    """Versioned anti-corruption layer between Core contracts and the Daemon."""

    def __init__(self) -> None:
        self._translators: dict[tuple[str, str], RuntimeControlTranslator] = {}

    def register(
        self,
        *,
        kind: str,
        contract_version: str,
        translator: RuntimeControlTranslator,
    ) -> None:
        key = (kind, contract_version)
        if not kind or not contract_version:
            raise ValueError("runtime-control translator identity must not be empty")
        if key in self._translators:
            raise ValueError(f"runtime-control translator {kind!r} v{contract_version} already exists")
        self._translators[key] = translator

    def translate(self, value: object) -> tuple[DaemonRuntimeIntent, ...]:
        if hasattr(value, "model_dump"):
            raw = value.model_dump(mode="json")
        else:
            raw = value
        if not isinstance(raw, dict):
            raise RuntimeControlTranslationError("runtime control must be an object")
        kind = raw.get("kind")
        contract_version = raw.get("contract_version", "1")
        if not isinstance(kind, str) or not isinstance(contract_version, str):
            raise RuntimeControlTranslationError(
                "runtime control requires string kind and contract_version"
            )
        translator = self._translators.get((kind, contract_version))
        if translator is None:
            raise RuntimeControlTranslationError(
                f"no Daemon translator is installed for {kind!r} v{contract_version}"
            )
        try:
            return translator(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeControlTranslationError(
                f"runtime control {kind!r} v{contract_version} is invalid: {exc}"
            ) from exc

    def translate_all(self, values: Iterable[object]) -> tuple[DaemonRuntimeIntent, ...]:
        return tuple(intent for value in values for intent in self.translate(value))


@dataclass(frozen=True, slots=True)
class HarnessRuntimeCapabilities:
    adapter_id: str
    active_features: frozenset[str]
    delegated_features: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.adapter_id:
            raise ValueError("harness runtime adapter ID must not be empty")
        overlap = self.active_features.intersection(self.delegated_features)
        if overlap:
            raise ValueError(f"runtime features cannot be active and delegated: {sorted(overlap)!r}")
        object.__setattr__(
            self,
            "delegated_features",
            MappingProxyType(dict(self.delegated_features)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "active_features": sorted(self.active_features),
            "delegated_features": dict(sorted(self.delegated_features.items())),
        }


@dataclass(frozen=True, slots=True)
class RuntimeIntentDecision:
    intent: DaemonRuntimeIntent
    status: RuntimeIntentStatus
    owner: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.intent.to_dict(),
            "status": self.status.value,
            "owner": self.owner,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class RuntimeActivationReport:
    adapter_id: str
    decisions: tuple[RuntimeIntentDecision, ...]

    @property
    def fully_supported(self) -> bool:
        return all(
            decision.status is not RuntimeIntentStatus.UNSUPPORTED
            for decision in self.decisions
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "adapter_id": self.adapter_id,
            "fully_supported": self.fully_supported,
            "decisions": [decision.to_dict() for decision in self.decisions],
        }


class EvolutionHarnessRuntimeAdapter(Protocol):
    @property
    def capabilities(self) -> HarnessRuntimeCapabilities: ...

    def reconcile(self, controls: Iterable[object]) -> RuntimeActivationReport: ...


class DeclarativeHarnessRuntimeAdapter:
    """Reconcile desired features without accepting imperative hooks or commands."""

    def __init__(
        self,
        *,
        capabilities: HarnessRuntimeCapabilities,
        translators: RuntimeControlTranslatorRegistry,
    ) -> None:
        self._capabilities = capabilities
        self._translators = translators

    @property
    def capabilities(self) -> HarnessRuntimeCapabilities:
        return self._capabilities

    def reconcile(self, controls: Iterable[object]) -> RuntimeActivationReport:
        intents = self._translators.translate_all(controls)
        decisions: list[RuntimeIntentDecision] = []
        for intent in intents:
            if intent.feature_id in self._capabilities.active_features:
                status = RuntimeIntentStatus.ACTIVE
                owner = self._capabilities.adapter_id
                message = "The harness adapter applies this desired state for the current session."
            elif intent.feature_id in self._capabilities.delegated_features:
                status = RuntimeIntentStatus.DELEGATED
                owner = self._capabilities.delegated_features[intent.feature_id]
                message = "The Daemon delegates this desired state to its lifecycle coordinator."
            else:
                status = RuntimeIntentStatus.UNSUPPORTED
                owner = self._capabilities.adapter_id
                message = "No verified executor is installed for this desired state."
            decisions.append(
                RuntimeIntentDecision(
                    intent=intent,
                    status=status,
                    owner=owner,
                    message=message,
                )
            )
        return RuntimeActivationReport(
            adapter_id=self._capabilities.adapter_id,
            decisions=tuple(decisions),
        )


def _memory_v1(value: object) -> tuple[DaemonRuntimeIntent, ...]:
    from openevo.evolution.framework.runtime_controls import (
        MemoryRuntimeControlV1,
        validate_runtime_control,
    )

    control = validate_runtime_control(value)
    if not isinstance(control, MemoryRuntimeControlV1):
        raise ValueError("control is not memory v1")
    common = {
        "source_kind": control.kind,
        "source_contract_version": control.contract_version,
    }
    return (
        DaemonRuntimeIntent(
            intent_id="memory-read",
            feature_id=f"memory.read.{control.read_timing}",
            parameters={"timing": control.read_timing},
            **common,
        ),
        DaemonRuntimeIntent(
            intent_id="memory-write",
            feature_id=f"memory.write.{control.write_timing}",
            parameters={
                "timing": control.write_timing,
                "visibility": control.update_visibility,
            },
            **common,
        ),
    )


def _skill_v1(value: object) -> tuple[DaemonRuntimeIntent, ...]:
    from openevo.evolution.framework.runtime_controls import (
        SkillRuntimeControlV1,
        validate_runtime_control,
    )

    control = validate_runtime_control(value)
    if not isinstance(control, SkillRuntimeControlV1):
        raise ValueError("control is not skill v1")
    return (
        DaemonRuntimeIntent(
            intent_id="skill-load",
            feature_id=(
                f"skill.load.{control.load_timing}.{control.selection_mode}"
            ),
            source_kind=control.kind,
            source_contract_version=control.contract_version,
            parameters={
                "timing": control.load_timing,
                "selection_mode": control.selection_mode,
                "visibility": control.update_visibility,
            },
        ),
    )


def _agent_system_v1(value: object) -> tuple[DaemonRuntimeIntent, ...]:
    from openevo.evolution.framework.runtime_controls import (
        AgentSystemRuntimeControlV1,
        validate_runtime_control,
    )

    control = validate_runtime_control(value)
    if not isinstance(control, AgentSystemRuntimeControlV1):
        raise ValueError("control is not agent_system v1")
    intents = [
        DaemonRuntimeIntent(
            intent_id="agent-system-instruction",
            feature_id=f"agent_system.instruction.{control.instruction_mode}",
            source_kind=control.kind,
            source_contract_version=control.contract_version,
            parameters={"visibility": control.update_visibility},
        )
    ]
    if control.spawn_plan is not None:
        intents.append(
            DaemonRuntimeIntent(
                intent_id="agent-system-spawn",
                feature_id="agent_system.spawn.harness_managed",
                source_kind=control.kind,
                source_contract_version=control.contract_version,
                parameters=control.spawn_plan.model_dump(mode="json"),
            )
        )
    return tuple(intents)


def default_runtime_control_translators() -> RuntimeControlTranslatorRegistry:
    registry = RuntimeControlTranslatorRegistry()
    registry.register(kind="memory", contract_version="1", translator=_memory_v1)
    registry.register(kind="skill", contract_version="1", translator=_skill_v1)
    registry.register(
        kind="agent_system",
        contract_version="1",
        translator=_agent_system_v1,
    )
    return registry


def codex_development_runtime_adapter() -> DeclarativeHarnessRuntimeAdapter:
    """Capabilities that the current loopback Codex development bridge truly owns."""

    return DeclarativeHarnessRuntimeAdapter(
        capabilities=HarnessRuntimeCapabilities(
            adapter_id="codex-development-v1",
            active_features=frozenset({
                "memory.read.session_start",
                "skill.load.session_start.harness_discovery",
                "agent_system.instruction.native_harness_file",
            }),
            delegated_features={
                "memory.write.session_closed": "development-evolution-runner",
            },
        ),
        translators=default_runtime_control_translators(),
    )


__all__ = [
    "DaemonRuntimeIntent",
    "DeclarativeHarnessRuntimeAdapter",
    "EvolutionHarnessRuntimeAdapter",
    "HarnessRuntimeCapabilities",
    "RuntimeActivationReport",
    "RuntimeControlTranslationError",
    "RuntimeControlTranslatorRegistry",
    "RuntimeIntentDecision",
    "RuntimeIntentStatus",
    "codex_development_runtime_adapter",
    "default_runtime_control_translators",
]
