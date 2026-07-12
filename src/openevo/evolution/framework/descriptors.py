"""Target, method, and target-handler descriptors."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import (
    CaptureMode,
    ContributionKind,
    DescriptorKind,
    DestinationScope,
    ExecutionMode,
    Exposure,
    ImplementationRef,
    Maturity,
    MethodInvocationABI,
    RendererKind,
    _Contract,
    _contract_version,
    _enum_tuple,
    _environment_name,
    _json_value,
    _mime_type,
    _stable_id,
    _text,
    _unique_ids,
    _unique_strings,
    _uri_scheme,
)
from .execution import MethodInputBinding


def _immutable_json(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise TypeError("descriptor JSON values are immutable")


class _FrozenJsonDict(dict[str, Any]):
    def __init__(self, value: dict[str, Any]) -> None:
        dict.__init__(self, {key: _freeze_json(item) for key, item in value.items()})

    __setitem__ = _immutable_json
    __delitem__ = _immutable_json
    clear = _immutable_json
    pop = _immutable_json
    popitem = _immutable_json
    setdefault = _immutable_json
    update = _immutable_json
    __ior__ = _immutable_json


class _FrozenJsonList(list[Any]):
    def __init__(self, value: list[Any]) -> None:
        list.__init__(self, (_freeze_json(item) for item in value))

    __setitem__ = _immutable_json
    __delitem__ = _immutable_json
    __iadd__ = _immutable_json
    __imul__ = _immutable_json
    append = _immutable_json
    clear = _immutable_json
    extend = _immutable_json
    insert = _immutable_json
    pop = _immutable_json
    remove = _immutable_json
    reverse = _immutable_json
    sort = _immutable_json


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenJsonDict(value)
    if isinstance(value, list):
        return _FrozenJsonList(value)
    return value


class _Descriptor(_Contract):
    id: str
    exposure: Exposure = Exposure.INTERNAL
    contract_version: str = "1"
    implementation_ref: ImplementationRef | None = None

    _id = field_validator("id")(_stable_id)
    _version = field_validator("contract_version")(_contract_version)


class EvolutionSelectionResolverDescriptor(_Contract):
    """A Core-owned project selection resolved to concrete methods later."""

    selection_value: str
    display_name: str
    description: str
    resolved_method_ids: tuple[str, ...] = Field(min_length=1)

    _selection = field_validator("selection_value")(_stable_id)
    _text_fields = field_validator("display_name", "description")(_text)
    _methods = field_validator("resolved_method_ids")(_unique_ids)


class EvolutionTargetDescriptor(_Descriptor):
    kind: Literal[DescriptorKind.TARGET] = DescriptorKind.TARGET
    display_name: str
    description: str
    artifact_type: str
    handler_id: str
    renderer_kind: RendererKind
    renderer_contract_version: Literal["1"] = "1"
    default_method_id: str
    selection_resolvers: tuple[EvolutionSelectionResolverDescriptor, ...] = ()
    context_order: int = Field(default=100, ge=0, le=10_000)
    maturity: Maturity = Maturity.EXPERIMENTAL

    _text_fields = field_validator("display_name", "description")(_text)
    _ids = field_validator(
        "artifact_type", "handler_id", "default_method_id"
    )(_stable_id)

    @field_validator("selection_resolvers")
    @classmethod
    def _unique_selection_resolvers(
        cls,
        values: tuple[EvolutionSelectionResolverDescriptor, ...],
    ) -> tuple[EvolutionSelectionResolverDescriptor, ...]:
        selection_values = tuple(value.selection_value for value in values)
        if len(selection_values) != len(set(selection_values)):
            raise ValueError("target selection resolver values must be unique")
        return values


class EvolutionMethodDescriptor(_Descriptor):
    kind: Literal[DescriptorKind.METHOD] = DescriptorKind.METHOD
    display_name: str
    description: str
    target_id: str
    invocation_abi: MethodInvocationABI
    execution_modes: tuple[ExecutionMode, ...] = Field(min_length=1)
    capture_modes: tuple[CaptureMode, ...] = Field(min_length=1)
    supported_harness_ids: tuple[str, ...] = Field(min_length=1)
    harness_requirements: tuple[str, ...] = ()
    runtime_requirements: tuple[str, ...] = ()
    input_bindings: tuple[MethodInputBinding, ...] = ()
    output_artifact_types: tuple[str, ...] = Field(min_length=1)
    config_schema: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    )
    default_config: dict[str, Any] = Field(default_factory=dict)
    maturity: Maturity = Maturity.EXPERIMENTAL

    _text_fields = field_validator("display_name", "description")(_text)
    _target = field_validator("target_id")(_stable_id)
    _modes = field_validator("execution_modes", "capture_modes")(_enum_tuple)
    _ids = field_validator(
        "supported_harness_ids",
        "harness_requirements",
        "runtime_requirements",
        "output_artifact_types",
    )(_unique_ids)

    @model_validator(mode="after")
    def _unique_input_bindings(self) -> EvolutionMethodDescriptor:
        binding_ids = tuple(binding.binding_id for binding in self.input_bindings)
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("method input binding IDs must be unique")
        return self

    @field_validator("config_schema", "default_config")
    @classmethod
    def _json_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        copied = _json_value(value)
        if not isinstance(copied, dict):  # Field typing makes this unreachable.
            raise ValueError("config must be a JSON object")
        return _FrozenJsonDict(copied)


class TargetHandlerDescriptor(_Descriptor):
    kind: Literal[DescriptorKind.TARGET_HANDLER] = DescriptorKind.TARGET_HANDLER
    target_id: str
    artifact_types: tuple[str, ...] = Field(min_length=1)
    renderer_kind: RendererKind
    renderer_contract_version: Literal["1"] = "1"
    contribution_contract_version: Literal["1"] = "1"
    allowed_uri_schemes: tuple[str, ...] = Field(min_length=1)
    allowed_media_types: tuple[str, ...] = Field(min_length=1)
    allowed_destination_scopes: tuple[DestinationScope, ...] = Field(min_length=1)
    environment_allowlist: tuple[str, ...] = ()
    allowed_contribution_kinds: tuple[ContributionKind, ...] = Field(min_length=1)

    _target = field_validator("target_id")(_stable_id)
    _artifacts = field_validator("artifact_types")(_unique_ids)
    _schemes = field_validator("allowed_uri_schemes")(
        lambda values: tuple(_uri_scheme(value) for value in _unique_strings(values))
    )
    _media = field_validator("allowed_media_types")(
        lambda values: tuple(_mime_type(value) for value in _unique_strings(values))
    )
    _scopes = field_validator("allowed_destination_scopes")(_enum_tuple)
    _environment = field_validator("environment_allowlist")(
        lambda values: tuple(
            _environment_name(value) for value in _unique_strings(values)
        )
    )
    _contributions = field_validator("allowed_contribution_kinds")(_enum_tuple)


__all__ = [
    "EvolutionMethodDescriptor",
    "EvolutionSelectionResolverDescriptor",
    "EvolutionTargetDescriptor",
    "TargetHandlerDescriptor",
]
