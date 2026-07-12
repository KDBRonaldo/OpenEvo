"""Editable target selections and deeply immutable resolved plans."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any, Literal, Self

from pydantic import (
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    SerializationInfo,
    TypeAdapter,
    field_validator,
    model_validator,
)
from pydantic_core import core_schema
from pydantic.json_schema import JsonSchemaValue

from .contracts import (
    EvolutionExecutionProfile,
    _Contract,
    _digest,
    _json_value,
    _optional_stable_id,
    _stable_id,
    canonical_digest,
    canonical_json,
)

_MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
_PROJECT_CONFIG_SERIALIZER = TypeAdapter(dict[str, Any])


def _project_config_json_value(value: Any, path: str = "$") -> Any:
    """Normalize JSON into the lossless Python/JavaScript project-config domain."""

    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_JSON_INTEGER:
            raise ValueError(f"integer exceeds safe JSON range at {path}")
        return value
    if isinstance(value, float):
        if value.is_integer():
            normalized = int(value)
            if abs(normalized) > _MAX_SAFE_JSON_INTEGER:
                raise ValueError(f"integer exceeds safe JSON range at {path}")
            return normalized
        return value
    if isinstance(value, dict):
        return {
            key: _project_config_json_value(item, f"{path}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _project_config_json_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"unsupported project config value at {path}")


def _serialize_project_config(
    value: ProjectEvolutionConfig,
    info: SerializationInfo,
) -> dict[str, Any]:
    return _PROJECT_CONFIG_SERIALIZER.dump_python(
        value.to_dict(),
        mode=info.mode,
        include=info.include,
        exclude=info.exclude,
        by_alias=info.by_alias,
        exclude_unset=info.exclude_unset,
        exclude_defaults=info.exclude_defaults,
        exclude_none=info.exclude_none,
        exclude_computed_fields=info.exclude_computed_fields,
        round_trip=info.round_trip,
        serialize_as_any=info.serialize_as_any,
        context=info.context,
    )


class EvolutionTargetSelection(_Contract):
    """Project editing selection; disabled targets may retain draft settings."""

    target_id: str
    enabled: bool
    method_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    _target = field_validator("target_id")(_stable_id)
    _method = field_validator("method_id")(_optional_stable_id)

    @field_validator("config")
    @classmethod
    def _copy_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        copied = _json_value(value)
        if not isinstance(copied, dict):  # Field typing makes this unreachable.
            raise ValueError("config must be a JSON object")
        return copied

    @model_validator(mode="after")
    def _selection(self) -> EvolutionTargetSelection:
        if self.enabled and self.method_id is None:
            raise ValueError("enabled target requires method_id")
        return self


class ProjectEvolutionConfig(Mapping[str, Any]):
    """Immutable canonical JSON object used by editable project selections."""

    __slots__ = ("_encoded",)

    def __init__(self, values: Mapping[str, object] | None = None) -> None:
        source: Mapping[str, object] = {} if values is None else values
        copied = _project_config_json_value(_json_value(source))
        if not isinstance(copied, dict):  # Mapping input makes this unreachable.
            raise ValueError("config must be a JSON object")
        object.__setattr__(self, "_encoded", canonical_json(copied))

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise TypeError("ProjectEvolutionConfig is immutable")

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())

    def __copy__(self) -> ProjectEvolutionConfig:
        return type(self)(self.to_dict())

    def __deepcopy__(self, memo: dict[int, object]) -> ProjectEvolutionConfig:
        del memo
        return type(self)(self.to_dict())

    def __reduce__(self) -> tuple[object, tuple[dict[str, Any]]]:
        return type(self), (self.to_dict(),)

    def __repr__(self) -> str:
        return f"ProjectEvolutionConfig({self.to_dict()!r})"

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._encoded)

    @classmethod
    def _validated(cls, value: object) -> ProjectEvolutionConfig:
        if isinstance(value, cls):
            return cls(value.to_dict())
        try:
            if not isinstance(value, Mapping):
                raise ValueError("config must be a JSON object")
            return cls(value)
        except TypeError as exc:
            raise ValueError(str(exc)) from exc

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: object,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        del source_type, handler
        dict_schema = core_schema.dict_schema(
            keys_schema=core_schema.str_schema(strict=True),
            values_schema=core_schema.any_schema(),
        )
        return core_schema.no_info_after_validator_function(
            cls._validated,
            core_schema.union_schema(
                [core_schema.is_instance_schema(cls), dict_schema]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                _serialize_project_config,
                info_arg=True,
                return_schema=dict_schema,
            ),
        )


class ProjectEvolutionTargetSelection(_Contract):
    """One editable value in a project's generic evolution target map."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        strict=True,
        validate_default=True,
    )

    enabled: bool
    method: str | None = None
    config: ProjectEvolutionConfig = Field(default_factory=ProjectEvolutionConfig)

    _method = field_validator("method")(_optional_stable_id)

    @model_validator(mode="after")
    def _enabled_method(self) -> ProjectEvolutionTargetSelection:
        if self.enabled and self.method is None:
            raise ValueError("enabled target requires method")
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Return a validated copy using the public field names."""

        del deep  # Canonical backing makes shallow and deep copies equivalent.
        payload = self.model_dump(mode="python")
        if update:
            unknown = set(update).difference({"enabled", "method", "config"})
            if unknown:
                raise ValueError(
                    "unknown project target selection update fields: "
                    + ", ".join(sorted(unknown))
                )
            payload.update(update)
        return type(self).model_validate(payload)


_PROJECT_TARGET_MAP_SERIALIZER = TypeAdapter(
    dict[str, ProjectEvolutionTargetSelection]
)


def _validated_project_target_selection(
    value: object,
) -> ProjectEvolutionTargetSelection:
    if isinstance(value, ProjectEvolutionTargetSelection):
        value = value.model_dump(mode="python")
    return ProjectEvolutionTargetSelection.model_validate(value)


def _serialize_project_target_map(
    value: ProjectEvolutionTargetMap,
    info: SerializationInfo,
) -> dict[str, Any]:
    return _PROJECT_TARGET_MAP_SERIALIZER.dump_python(
        value.to_dict(),
        mode=info.mode,
        include=info.include,
        exclude=info.exclude,
        by_alias=info.by_alias,
        exclude_unset=info.exclude_unset,
        exclude_defaults=info.exclude_defaults,
        exclude_none=info.exclude_none,
        exclude_computed_fields=info.exclude_computed_fields,
        round_trip=info.round_trip,
        serialize_as_any=info.serialize_as_any,
        context=info.context,
    )


def validate_project_target_map_keys(value: object) -> object:
    """Reject non-string/coerced target IDs before Pydantic parses a map."""

    if not isinstance(value, Mapping):
        raise ValueError("evolution.targets must be a JSON object")
    for target_id in value:
        if type(target_id) is not str:
            raise ValueError("evolution target IDs must be strings")
        _stable_id(target_id)
    return value


class ProjectEvolutionTargetMap(Mapping[str, ProjectEvolutionTargetSelection]):
    """Canonical immutable map with fresh selection views and typed schemas."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, object]) -> None:
        validate_project_target_map_keys(values)
        object.__setattr__(
            self,
            "_items",
            tuple(
                (
                    target_id,
                    _validated_project_target_selection(selection).model_dump_json(),
                )
                for target_id, selection in sorted(values.items())
            ),
        )

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise TypeError("ProjectEvolutionTargetMap is immutable")

    def __getitem__(self, key: str) -> ProjectEvolutionTargetSelection:
        for target_id, encoded in self._items:
            if target_id == key:
                return ProjectEvolutionTargetSelection.model_validate_json(encoded)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (target_id for target_id, _encoded in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __copy__(self) -> ProjectEvolutionTargetMap:
        return type(self)(self.to_dict())

    def __deepcopy__(self, memo: dict[int, object]) -> ProjectEvolutionTargetMap:
        del memo
        return type(self)(self.to_dict())

    def __reduce__(self) -> tuple[object, tuple[dict[str, object]]]:
        return type(self), (self.to_dict(),)

    def to_dict(self) -> dict[str, ProjectEvolutionTargetSelection]:
        return {target_id: self[target_id] for target_id in self}

    @classmethod
    def _validated(cls, value: object) -> ProjectEvolutionTargetMap:
        if not isinstance(value, Mapping):
            raise ValueError("evolution.targets must be a JSON object")
        return cls(value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: object,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        del source_type
        selection_schema = handler.generate_schema(ProjectEvolutionTargetSelection)
        dict_schema = core_schema.dict_schema(
            keys_schema=core_schema.str_schema(strict=True),
            values_schema=selection_schema,
        )
        validated_dict_schema = core_schema.no_info_before_validator_function(
            validate_project_target_map_keys,
            dict_schema,
        )
        input_schema = core_schema.union_schema(
            [
                core_schema.is_instance_schema(cls),
                validated_dict_schema,
            ]
        )
        return core_schema.no_info_after_validator_function(
            cls._validated,
            input_schema,
            serialization=core_schema.plain_serializer_function_ser_schema(
                _serialize_project_target_map,
                info_arg=True,
                return_schema=core_schema.dict_schema(
                    keys_schema=core_schema.str_schema(strict=True),
                    values_schema=core_schema.any_schema(),
                ),
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        schema: core_schema.CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        del schema
        return handler(_PROJECT_TARGET_MAP_SERIALIZER.core_schema)


class ResolvedEvolutionSelection(_Contract):
    target_id: str
    handler_id: str
    method_id: str
    config_json: str
    config_digest: str
    target_identity_digest: str
    handler_identity_digest: str
    method_identity_digest: str

    _ids = field_validator("target_id", "handler_id", "method_id")(_stable_id)
    _digests = field_validator(
        "config_digest",
        "target_identity_digest",
        "handler_identity_digest",
        "method_identity_digest",
    )(_digest)

    @model_validator(mode="after")
    def _canonical_config(self) -> ResolvedEvolutionSelection:
        try:
            value = json.loads(self.config_json)
        except json.JSONDecodeError as exc:
            raise ValueError("config_json must contain canonical JSON") from exc
        if not isinstance(value, dict) or canonical_json(value) != self.config_json:
            raise ValueError("config_json must be a canonical JSON object")
        if canonical_digest(value) != self.config_digest:
            raise ValueError("config_digest does not match config_json")
        return self

    def config(self) -> dict[str, Any]:
        return json.loads(self.config_json)


class EvolutionPlan(_Contract):
    schema_version: Literal["1"] = "1"
    plan_id: str
    registry_snapshot_digest: str
    execution_profile: EvolutionExecutionProfile
    selections: tuple[ResolvedEvolutionSelection, ...]

    _plan = field_validator("plan_id")(_stable_id)
    _digest = field_validator("registry_snapshot_digest")(_digest)

    @field_validator("selections")
    @classmethod
    def _canonical_order(
        cls,
        values: tuple[ResolvedEvolutionSelection, ...],
    ) -> tuple[ResolvedEvolutionSelection, ...]:
        return tuple(sorted(values, key=lambda value: value.target_id))

    @model_validator(mode="after")
    def _unique_targets(self) -> EvolutionPlan:
        targets = tuple(selection.target_id for selection in self.selections)
        if len(targets) != len(set(targets)):
            raise ValueError("plan contains duplicate target selections")
        return self


__all__ = [
    "EvolutionPlan",
    "EvolutionTargetSelection",
    "ProjectEvolutionConfig",
    "ProjectEvolutionTargetSelection",
    "ProjectEvolutionTargetMap",
    "ResolvedEvolutionSelection",
    "validate_project_target_map_keys",
]
