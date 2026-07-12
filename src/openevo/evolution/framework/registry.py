"""Frozen target/method registry layered on the existing evolution backend."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Generic, TypeAlias, TypeVar, cast

from pydantic import BaseModel

from .contracts import (
    DescriptorKind,
    EvolutionExecutionProfile,
    Exposure,
    ImplementationIdentity,
    ImplementationRef,
    canonical_digest,
    canonical_json,
)
from .contributions import TargetHandlerOutput
from .descriptors import (
    EvolutionMethodDescriptor,
    EvolutionTargetDescriptor,
    TargetHandlerDescriptor,
)
from .execution import validate_user_config_schema_ownership
from .handler_validation import (
    validate_handler_output as validate_target_handler_output,
)
from .handler_validation import validate_handler_outputs as validate_target_handler_outputs
from .handlers import TargetHandlerInput
from .plan import EvolutionPlan, EvolutionTargetSelection, ResolvedEvolutionSelection
from .schema import (
    normalize_config_override,
    normalize_partial_config,
    validate_config_schema,
)
from .support import MethodSupportOverall, evaluate_method_support


Descriptor: TypeAlias = (
    EvolutionTargetDescriptor
    | EvolutionMethodDescriptor
    | TargetHandlerDescriptor
)
DescriptorKey: TypeAlias = tuple[DescriptorKind, str]

_ENTRY_POINT_RE = re.compile(
    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z",
    re.ASCII,
)
_EXPOSURE_RANK = {
    Exposure.DESKTOP: 0,
    Exposure.MAINTAINER: 1,
    Exposure.INTERNAL: 2,
}

DescriptorT = TypeVar("DescriptorT", bound=BaseModel)


class CanonicalModelView(Mapping[str, DescriptorT], Generic[DescriptorT]):
    """Read-only canonical backing that returns a fresh descriptor per access."""

    __slots__ = ("_model", "_serialized")

    def __init__(
        self,
        values: Mapping[str, str],
        model: type[DescriptorT],
    ) -> None:
        object.__setattr__(
            self,
            "_serialized",
            MappingProxyType(dict(sorted(values.items()))),
        )
        object.__setattr__(self, "_model", model)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("canonical model view is immutable")

    def __getitem__(self, key: str) -> DescriptorT:
        return self._model.model_validate_json(self._serialized[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._serialized)

    def __len__(self) -> int:
        return len(self._serialized)


def _identity_key(kind: DescriptorKind, descriptor_id: str) -> str:
    return f"{kind.value}:{descriptor_id}"


def _copy_descriptor(descriptor: Descriptor) -> Descriptor:
    return type(descriptor).model_validate(descriptor.model_dump(mode="python"))


def _descriptor_digest(descriptor: Descriptor) -> str:
    return canonical_digest(
        descriptor.model_dump(mode="json", exclude={"implementation_ref"})
    )


def _implementation_identity(descriptor: Descriptor) -> ImplementationIdentity:
    if descriptor.implementation_ref is None:
        raise ValueError(
            f"{descriptor.kind.value} descriptor {descriptor.id!r} requires implementation_ref"
        )
    return ImplementationIdentity(
        descriptor_kind=descriptor.kind,
        descriptor_id=descriptor.id,
        descriptor_digest=_descriptor_digest(descriptor),
        implementation=descriptor.implementation_ref,
    )


def _validate_entry_point(ref: ImplementationRef, owner: str) -> None:
    if _ENTRY_POINT_RE.fullmatch(ref.entry_point) is None:
        raise ValueError(f"{owner} has malformed implementation entry_point")


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    targets: CanonicalModelView[EvolutionTargetDescriptor]
    methods: CanonicalModelView[EvolutionMethodDescriptor]
    target_handlers: CanonicalModelView[TargetHandlerDescriptor]
    identities: CanonicalModelView[ImplementationIdentity]
    identity_digests: Mapping[str, str]
    registry_digest: str

    def identity_for(
        self,
        kind: DescriptorKind | str,
        descriptor_id: str,
    ) -> ImplementationIdentity:
        try:
            normalized_kind = DescriptorKind(kind)
            return self.identities[_identity_key(normalized_kind, descriptor_id)]
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unknown implementation identity {kind}:{descriptor_id}") from exc

    def identity_digest_for(
        self,
        kind: DescriptorKind | str,
        descriptor_id: str,
    ) -> str:
        try:
            normalized_kind = DescriptorKind(kind)
            return self.identity_digests[_identity_key(normalized_kind, descriptor_id)]
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unknown implementation identity {kind}:{descriptor_id}") from exc

    def normalize_method_config(
        self,
        method_id: str,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            method = self.methods[method_id]
        except KeyError as exc:
            raise ValueError(f"unknown method {method_id!r}") from exc
        try:
            return normalize_config_override(
                method.config_schema,
                method.default_config,
                dict(config),
            )
        except ValueError as exc:
            raise ValueError(f"invalid config for method {method.id!r}: {exc}") from exc

    def resolve_selection(
        self,
        selection: EvolutionTargetSelection,
        profile: EvolutionExecutionProfile,
    ) -> ResolvedEvolutionSelection:
        if not selection.enabled or selection.method_id is None:
            raise ValueError("only enabled target selections can be resolved")
        target = self._target(selection.target_id)
        method = self._method_for_target(selection.method_id, target.id)
        self._validate_method_profile(method, profile)
        config = self.normalize_method_config(method.id, selection.config)
        config_json = canonical_json(config)
        handler = self.target_handlers[target.handler_id]
        return ResolvedEvolutionSelection(
            target_id=target.id,
            handler_id=handler.id,
            method_id=method.id,
            config_json=config_json,
            config_digest=canonical_digest(config),
            target_identity_digest=self.identity_digest_for(
                DescriptorKind.TARGET, target.id
            ),
            handler_identity_digest=self.identity_digest_for(
                DescriptorKind.TARGET_HANDLER, handler.id
            ),
            method_identity_digest=self.identity_digest_for(
                DescriptorKind.METHOD, method.id
            ),
        )

    def compile_plan(
        self,
        *,
        plan_id: str,
        selections: Iterable[EvolutionTargetSelection],
        profile: EvolutionExecutionProfile,
    ) -> EvolutionPlan:
        selection_list = tuple(selections)
        seen: set[str] = set()
        resolved: list[ResolvedEvolutionSelection] = []
        reachable: set[str] = set()

        for selection in selection_list:
            if selection.target_id in seen:
                raise ValueError(
                    f"duplicate target selection for {selection.target_id!r}"
                )
            seen.add(selection.target_id)
            target = self._target(selection.target_id)
            if not selection.enabled:
                continue
            item = self.resolve_selection(selection, profile)
            resolved.append(item)
            reachable.update(
                {
                    _identity_key(DescriptorKind.TARGET, target.id),
                    _identity_key(DescriptorKind.TARGET_HANDLER, target.handler_id),
                    _identity_key(DescriptorKind.METHOD, item.method_id),
                    _identity_key(DescriptorKind.METHOD, target.default_method_id),
                }
            )

        registry_snapshot_digest = canonical_digest(
            [
                {"identity": key, "digest": self.identity_digests[key]}
                for key in sorted(reachable)
            ]
        )
        return EvolutionPlan(
            plan_id=plan_id,
            registry_snapshot_digest=registry_snapshot_digest,
            execution_profile=profile,
            selections=tuple(resolved),
        )

    def plan_snapshot_digest(
        self,
        selections: Iterable[EvolutionTargetSelection],
        profile: EvolutionExecutionProfile,
    ) -> str:
        return self.compile_plan(
            plan_id="digest-probe",
            selections=selections,
            profile=profile,
        ).registry_snapshot_digest

    def validate_handler_output(
        self,
        output: TargetHandlerOutput,
        *,
        handler_input: TargetHandlerInput,
    ) -> TargetHandlerOutput:
        return validate_target_handler_output(
            self,
            output,
            handler_input=handler_input,
        )

    def validate_handler_outputs(
        self,
        pairs: Iterable[tuple[TargetHandlerInput, TargetHandlerOutput]],
    ) -> tuple[TargetHandlerOutput, ...]:
        return validate_target_handler_outputs(self, pairs)

    def _target(self, target_id: str) -> EvolutionTargetDescriptor:
        try:
            return self.targets[target_id]
        except KeyError as exc:
            raise ValueError(f"unknown target {target_id!r}") from exc

    def _method_for_target(
        self,
        method_id: str,
        target_id: str,
    ) -> EvolutionMethodDescriptor:
        try:
            method = self.methods[method_id]
        except KeyError as exc:
            raise ValueError(f"unknown method {method_id!r}") from exc
        if method.target_id != target_id:
            raise ValueError(
                f"method {method.id!r} does not belong to target {target_id!r}"
            )
        return method

    @staticmethod
    def _validate_method_profile(
        method: EvolutionMethodDescriptor,
        profile: EvolutionExecutionProfile,
    ) -> None:
        support = evaluate_method_support(method, profile)
        if support.overall is not MethodSupportOverall.SUPPORTED:
            raise ValueError(support.failure_messages()[0])


class EvolutionFrameworkRegistry:
    """Mutable startup builder that freezes into one validated snapshot."""

    def __init__(self, descriptors: Iterable[Descriptor] = ()) -> None:
        self._descriptors: dict[DescriptorKey, Descriptor] = {}
        self._snapshot: RegistrySnapshot | None = None
        for descriptor in descriptors:
            self.register(descriptor)

    @property
    def frozen(self) -> bool:
        return self._snapshot is not None

    @property
    def snapshot(self) -> RegistrySnapshot:
        if self._snapshot is None:
            raise RuntimeError("evolution framework registry is not frozen")
        return self._snapshot

    def register(self, descriptor: Descriptor) -> None:
        if self.frozen:
            raise RuntimeError("evolution framework registry is frozen")
        if type(descriptor) not in {
            EvolutionTargetDescriptor,
            EvolutionMethodDescriptor,
            TargetHandlerDescriptor,
        }:
            raise TypeError(f"unsupported evolution descriptor: {type(descriptor).__name__}")
        key = (descriptor.kind, descriptor.id)
        if key in self._descriptors:
            raise ValueError(
                f"duplicate {descriptor.kind.value} descriptor ID {descriptor.id!r}"
            )
        self._descriptors[key] = _copy_descriptor(descriptor)

    def register_target(self, descriptor: EvolutionTargetDescriptor) -> None:
        self._register_typed(descriptor, EvolutionTargetDescriptor)

    def register_method(self, descriptor: EvolutionMethodDescriptor) -> None:
        self._register_typed(descriptor, EvolutionMethodDescriptor)

    def register_target_handler(self, descriptor: TargetHandlerDescriptor) -> None:
        self._register_typed(descriptor, TargetHandlerDescriptor)

    def freeze(self) -> RegistrySnapshot:
        if self._snapshot is not None:
            return self._snapshot
        targets = self._group(DescriptorKind.TARGET, EvolutionTargetDescriptor)
        methods = self._group(DescriptorKind.METHOD, EvolutionMethodDescriptor)
        handlers = self._group(
            DescriptorKind.TARGET_HANDLER,
            TargetHandlerDescriptor,
        )
        self._validate_graph(targets, methods, handlers)

        identities: dict[str, ImplementationIdentity] = {}
        identity_digests: dict[str, str] = {}
        for descriptor in self._descriptors.values():
            identity = _implementation_identity(descriptor)
            key = _identity_key(descriptor.kind, descriptor.id)
            identities[key] = identity
            identity_digests[key] = canonical_digest(identity)

        serialized_targets = {
            key: canonical_json(value) for key, value in targets.items()
        }
        serialized_methods = {
            key: canonical_json(value) for key, value in methods.items()
        }
        serialized_handlers = {
            key: canonical_json(value) for key, value in handlers.items()
        }
        serialized_identities = {
            key: canonical_json(value) for key, value in identities.items()
        }
        frozen_digests = MappingProxyType(dict(sorted(identity_digests.items())))
        registry_digest = canonical_digest(
            [
                {"identity": key, "digest": digest}
                for key, digest in frozen_digests.items()
            ]
        )
        self._snapshot = RegistrySnapshot(
            targets=CanonicalModelView(serialized_targets, EvolutionTargetDescriptor),
            methods=CanonicalModelView(serialized_methods, EvolutionMethodDescriptor),
            target_handlers=CanonicalModelView(
                serialized_handlers,
                TargetHandlerDescriptor,
            ),
            identities=CanonicalModelView(
                serialized_identities,
                ImplementationIdentity,
            ),
            identity_digests=frozen_digests,
            registry_digest=registry_digest,
        )
        return self._snapshot

    def _group(
        self,
        kind: DescriptorKind,
        model: type[DescriptorT],
    ) -> dict[str, DescriptorT]:
        return {
            descriptor_id: cast(DescriptorT, descriptor)
            for (descriptor_kind, descriptor_id), descriptor in self._descriptors.items()
            if descriptor_kind is kind and isinstance(descriptor, model)
        }

    def _validate_graph(
        self,
        targets: Mapping[str, EvolutionTargetDescriptor],
        methods: Mapping[str, EvolutionMethodDescriptor],
        handlers: Mapping[str, TargetHandlerDescriptor],
    ) -> None:
        for descriptor in self._descriptors.values():
            if descriptor.implementation_ref is None:
                raise ValueError(
                    f"{descriptor.kind.value} descriptor {descriptor.id!r} "
                    "requires implementation_ref"
                )
            _validate_entry_point(
                descriptor.implementation_ref,
                f"{descriptor.kind.value} descriptor {descriptor.id!r}",
            )

        for handler in handlers.values():
            if handler.target_id not in targets:
                raise ValueError(
                    f"target handler {handler.id!r} references unknown target "
                    f"{handler.target_id!r}"
                )

        for method in methods.values():
            try:
                target = targets[method.target_id]
            except KeyError as exc:
                raise ValueError(
                    f"method {method.id!r} references unknown target {method.target_id!r}"
                ) from exc
            if target.artifact_type not in method.output_artifact_types:
                raise ValueError(
                    f"method {method.id!r} does not output target artifact type "
                    f"{target.artifact_type!r}"
                )
            injected_fields = {
                injection.field_name
                for injection in method.project_config_injections
            }
            root_annotations = [
                method.config_schema.get("default"),
                method.config_schema.get("const"),
            ]
            enum_annotation = method.config_schema.get("enum")
            if isinstance(enum_annotation, list):
                root_annotations.extend(enum_annotation)
            if any(
                isinstance(annotation, Mapping)
                and bool(injected_fields.intersection(annotation))
                for annotation in root_annotations
            ):
                raise ValueError(
                    f"method {method.id!r} root schema annotation embeds injected "
                    "project config fields"
                )
            try:
                validate_config_schema(method.config_schema)
                validate_user_config_schema_ownership(method.config_schema)
            except ValueError as exc:
                raise ValueError(
                    f"method {method.id!r} has invalid config schema: {exc}"
                ) from exc
            config_properties = method.config_schema.get("properties", {})
            missing_injected_fields = injected_fields.difference(config_properties)
            if missing_injected_fields:
                raise ValueError(
                    f"method {method.id!r} injects undeclared project config fields: "
                    + ", ".join(sorted(missing_injected_fields))
                )
            try:
                normalize_partial_config(method.config_schema, method.default_config)
            except ValueError as exc:
                raise ValueError(
                    f"method {method.id!r} has invalid default config: {exc}"
                ) from exc

        for target in targets.values():
            try:
                handler = handlers[target.handler_id]
            except KeyError as exc:
                raise ValueError(
                    f"target {target.id!r} references unknown target handler "
                    f"{target.handler_id!r}"
                ) from exc
            if (
                handler.target_id != target.id
                or set(handler.artifact_types) != {target.artifact_type}
                or handler.renderer_kind != target.renderer_kind
                or handler.renderer_contract_version
                != target.renderer_contract_version
            ):
                raise ValueError(
                    f"target handler {handler.id!r} mismatch for target {target.id!r}"
                )
            if _EXPOSURE_RANK[handler.exposure] > _EXPOSURE_RANK[target.exposure]:
                raise ValueError(
                    f"target {target.id!r} handler is hidden from its audience"
                )
            try:
                default_method = methods[target.default_method_id]
            except KeyError as exc:
                raise ValueError(
                    f"target {target.id!r} references unknown default method "
                    f"{target.default_method_id!r}"
                ) from exc
            if default_method.target_id != target.id:
                raise ValueError(
                    f"target {target.id!r} default method belongs to another target"
                )
            if _EXPOSURE_RANK[default_method.exposure] > _EXPOSURE_RANK[target.exposure]:
                raise ValueError(
                    f"target {target.id!r} default method is hidden from its audience"
                )
            target_method_ids = {
                method.id for method in methods.values() if method.target_id == target.id
            }
            for resolver in target.selection_resolvers:
                if resolver.selection_value in target_method_ids:
                    raise ValueError(
                        f"target {target.id!r} selection resolver shadows a method ID"
                    )
                for method_id in resolver.resolved_method_ids:
                    try:
                        resolved_method = methods[method_id]
                    except KeyError as exc:
                        raise ValueError(
                            f"target {target.id!r} selection resolver references "
                            f"unknown method {method_id!r}"
                        ) from exc
                    if resolved_method.target_id != target.id:
                        raise ValueError(
                            f"target {target.id!r} selection resolver method belongs "
                            "to another target"
                        )

    def _register_typed(
        self,
        descriptor: object,
        expected: type[Descriptor],
    ) -> None:
        if type(descriptor) is not expected:
            raise TypeError(f"expected {expected.__name__}, got {type(descriptor).__name__}")
        self.register(cast(Descriptor, descriptor))


__all__ = [
    "CanonicalModelView",
    "EvolutionFrameworkRegistry",
    "RegistrySnapshot",
]
