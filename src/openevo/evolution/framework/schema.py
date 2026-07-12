"""Validation for OpenEvo's bounded JSON Schema configuration subset."""

from __future__ import annotations

import math
import re
from typing import Any

MAX_SCHEMA_DEPTH = 8
MAX_SCHEMA_NODES = 256
MAX_OBJECT_PROPERTIES = 64
MAX_ENUM_VALUES = 128
MAX_STRING_LENGTH = 4096
MAX_ARRAY_ITEMS = 256

__all__ = [
    "MAX_ARRAY_ITEMS",
    "MAX_ENUM_VALUES",
    "MAX_OBJECT_PROPERTIES",
    "MAX_SCHEMA_DEPTH",
    "MAX_SCHEMA_NODES",
    "MAX_STRING_LENGTH",
    "normalize_config",
    "normalize_partial_config",
    "validate_config_schema",
    "validate_schema",
]

_SCALAR_TYPES = {"string", "number", "integer", "boolean"}
_SUPPORTED_TYPES = _SCALAR_TYPES | {"object", "array"}
_COMMON_KEYWORDS = {"type", "title", "description", "enum", "const", "default"}
_TYPE_KEYWORDS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength", "x-openevo-secret-ref"},
    "number": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
    "integer": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
    "boolean": set(),
    "null": set(),
}
_NULLABLE_KEYWORDS = {"anyOf", "title", "description", "enum", "const", "default"}
_ALL_KEYWORDS = _NULLABLE_KEYWORDS | _COMMON_KEYWORDS | set().union(*_TYPE_KEYWORDS.values())
_SECRET_REFERENCE_RE = re.compile(r"openevo-secret:[A-Za-z0-9_.-]{1,128}\Z", re.ASCII)
_MISSING = object()


def _error(path: str, message: str) -> ValueError:
    return ValueError(f"{path}: {message}")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _is_sensitive_name(name: str) -> bool:
    snake_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    parts = tuple(part for part in re.split(r"[^a-z0-9]+", snake_case) if part)
    values = set(parts)
    if values.intersection({"secret", "password", "credential", "authorization"}):
        return True
    if "apikey" in values or ({"api", "key"}.issubset(values)):
        return True
    if "token" in values and (
        len(parts) == 1
        or "ref" in values
        or values.intersection({"access", "auth", "bearer", "refresh", "session"})
    ):
        return True
    return "key" in values and bool(
        values.intersection(
            {"access", "private", "secret", "client", "signing", "encryption", "ssh"}
        )
    )


def _is_secret_reference_name(name: str) -> bool:
    return _is_sensitive_name(name) and name.lower().endswith("_ref")


def _contains_sensitive_value(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            _is_sensitive_name(key) or _contains_sensitive_value(item)
            for key, item in value.items()
            if isinstance(key, str)
        )
    if isinstance(value, list):
        return any(_contains_sensitive_value(item) for item in value)
    return False


def _json_equal(left: object, right: object) -> bool:
    if _is_number(left) and _is_number(right):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _check_json_value(value: object, path: str, active: set[int]) -> None:
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
            raise _error(path, f"string exceeds {MAX_STRING_LENGTH} characters")
        return
    if _is_number(value):
        if not _is_finite_number(value):
            raise _error(path, "number must be finite")
        return
    if isinstance(value, list):
        if len(value) > MAX_ARRAY_ITEMS:
            raise _error(path, f"array exceeds {MAX_ARRAY_ITEMS} items")
        identity = id(value)
        if identity in active:
            raise _error(path, "recursive values are forbidden")
        active.add(identity)
        try:
            for index, item in enumerate(value):
                _check_json_value(item, f"{path}[{index}]", active)
        finally:
            active.remove(identity)
        return
    if isinstance(value, dict):
        if len(value) > MAX_OBJECT_PROPERTIES:
            raise _error(path, f"object exceeds {MAX_OBJECT_PROPERTIES} properties")
        identity = id(value)
        if identity in active:
            raise _error(path, "recursive values are forbidden")
        active.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise _error(path, "object property names must be strings")
                if len(key) > MAX_STRING_LENGTH:
                    raise _error(path, f"property name exceeds {MAX_STRING_LENGTH} characters")
                _check_json_value(item, f"{path}.{key}", active)
        finally:
            active.remove(identity)
        return
    raise _error(path, "value must use JSON-compatible types")


class _SchemaValidator:
    def __init__(self) -> None:
        self.nodes = 0
        self.active: set[int] = set()

    def validate(self, schema: object, path: str = "schema", depth: int = 1) -> None:
        if not isinstance(schema, dict):
            raise _error(path, "schema node must be an object")
        if depth > MAX_SCHEMA_DEPTH:
            raise _error(path, f"schema exceeds maximum depth {MAX_SCHEMA_DEPTH}")
        self.nodes += 1
        if self.nodes > MAX_SCHEMA_NODES:
            raise _error(path, f"schema exceeds maximum node count {MAX_SCHEMA_NODES}")

        identity = id(schema)
        if identity in self.active:
            raise _error(path, "recursive schemas are forbidden")
        self.active.add(identity)
        try:
            self._validate_node(schema, path, depth)
        finally:
            self.active.remove(identity)

    def _validate_node(self, schema: dict[str, Any], path: str, depth: int) -> None:
        for key in schema:
            if not isinstance(key, str):
                raise _error(path, "schema keyword names must be strings")
            if key not in _ALL_KEYWORDS:
                raise _error(f"{path}.{key}", "unsupported schema keyword")

        if "anyOf" in schema:
            self._validate_nullable(schema, path, depth)
        else:
            schema_type = schema.get("type")
            if not isinstance(schema_type, str) or schema_type not in _SUPPORTED_TYPES:
                raise _error(f"{path}.type", "must be one supported scalar, object, or array type")
            allowed = _COMMON_KEYWORDS | _TYPE_KEYWORDS[schema_type]
            self._reject_unknown_keywords(schema, allowed, path)
            self._validate_typed_keywords(schema, schema_type, path, depth)

        self._validate_annotations(schema, path)
        self._validate_enum_const_default(schema, path)

    @staticmethod
    def _reject_unknown_keywords(schema: dict[str, Any], allowed: set[str], path: str) -> None:
        for keyword in schema:
            if keyword not in allowed:
                raise _error(f"{path}.{keyword}", "unsupported schema keyword")

    def _validate_nullable(self, schema: dict[str, Any], path: str, depth: int) -> None:
        self._reject_unknown_keywords(schema, _NULLABLE_KEYWORDS, path)
        branches = schema["anyOf"]
        if not isinstance(branches, list) or len(branches) != 2:
            raise _error(f"{path}.anyOf", "nullable anyOf must contain exactly two schemas")
        null_indexes = [
            index
            for index, branch in enumerate(branches)
            if isinstance(branch, dict) and branch == {"type": "null"}
        ]
        if len(null_indexes) != 1:
            raise _error(f"{path}.anyOf", "nullable anyOf must contain exactly one type=null schema")
        for index, branch in enumerate(branches):
            branch_path = f"{path}.anyOf[{index}]"
            if index in null_indexes:
                self._validate_null_branch(branch, branch_path, depth + 1)
            else:
                if isinstance(branch, dict) and "anyOf" in branch:
                    raise _error(f"{branch_path}.anyOf", "nested nullable anyOf is forbidden")
                self.validate(branch, branch_path, depth + 1)

    def _validate_null_branch(self, schema: object, path: str, depth: int) -> None:
        if depth > MAX_SCHEMA_DEPTH:
            raise _error(path, f"schema exceeds maximum depth {MAX_SCHEMA_DEPTH}")
        self.nodes += 1
        if self.nodes > MAX_SCHEMA_NODES:
            raise _error(path, f"schema exceeds maximum node count {MAX_SCHEMA_NODES}")
        if schema != {"type": "null"}:
            raise _error(path, "nullable null branch must be exactly type=null")

    def _validate_typed_keywords(
        self, schema: dict[str, Any], schema_type: str, path: str, depth: int
    ) -> None:
        if schema_type == "object":
            self._validate_object(schema, path, depth)
        elif schema_type == "array":
            self._validate_array(schema, path, depth)
        elif schema_type == "string":
            self._validate_size_bounds(schema, path, "Length", MAX_STRING_LENGTH)
            if (
                "x-openevo-secret-ref" in schema
                and schema["x-openevo-secret-ref"] is not True
            ):
                raise _error(
                    f"{path}.x-openevo-secret-ref",
                    "must be true when present",
                )
        elif schema_type in {"number", "integer"}:
            self._validate_numeric_bounds(schema, path)

    def _validate_object(self, schema: dict[str, Any], path: str, depth: int) -> None:
        if schema.get("additionalProperties", _MISSING) is not False:
            raise _error(f"{path}.additionalProperties", "must be present and false")
        if "properties" not in schema:
            raise _error(f"{path}.properties", "must be present for closed objects")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise _error(f"{path}.properties", "must be an object")
        if len(properties) > MAX_OBJECT_PROPERTIES:
            raise _error(
                f"{path}.properties",
                f"exceeds maximum property count {MAX_OBJECT_PROPERTIES}",
            )
        for name, child in properties.items():
            if not isinstance(name, str):
                raise _error(f"{path}.properties", "property names must be strings")
            if len(name) > MAX_STRING_LENGTH:
                raise _error(f"{path}.properties", "property name is too long")
            self.validate(child, f"{path}.properties.{name}", depth + 1)
            if isinstance(child, dict) and (
                _is_sensitive_name(name) or "x-openevo-secret-ref" in child
            ):
                for keyword in ("default", "enum", "const"):
                    if keyword in child:
                        raise _error(
                            f"{path}.properties.{name}.{keyword}",
                            "sensitive properties must not embed values",
                        )
                if not _is_secret_reference_name(name):
                    raise _error(
                        f"{path}.properties.{name}",
                        "sensitive properties must be opaque *_ref fields",
                    )
                if child.get("type") != "string":
                    raise _error(
                        f"{path}.properties.{name}.type",
                        "secret references must be strings",
                    )
                if child.get("x-openevo-secret-ref") is not True:
                    raise _error(
                        f"{path}.properties.{name}.x-openevo-secret-ref",
                        "secret reference fields must opt into Core validation",
                    )

        required = schema.get("required", [])
        if not isinstance(required, list):
            raise _error(f"{path}.required", "must be an array")
        if len(required) > MAX_OBJECT_PROPERTIES:
            raise _error(f"{path}.required", "contains too many entries")
        seen: set[str] = set()
        for index, name in enumerate(required):
            required_path = f"{path}.required[{index}]"
            if not isinstance(name, str):
                raise _error(required_path, "must be a property name")
            if name not in properties:
                raise _error(required_path, "must name a declared property")
            if name in seen:
                raise _error(required_path, "must not duplicate a property name")
            seen.add(name)

    def _validate_array(self, schema: dict[str, Any], path: str, depth: int) -> None:
        if "items" not in schema:
            raise _error(f"{path}.items", "must be present for bounded arrays")
        self.validate(schema["items"], f"{path}.items", depth + 1)
        self._validate_size_bounds(schema, path, "Items", MAX_ARRAY_ITEMS)

    @staticmethod
    def _validate_size_bounds(
        schema: dict[str, Any], path: str, suffix: str, maximum: int
    ) -> None:
        minimum = schema.get(f"min{suffix}")
        declared_maximum = schema.get(f"max{suffix}")
        for keyword, value in ((f"min{suffix}", minimum), (f"max{suffix}", declared_maximum)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise _error(f"{path}.{keyword}", "must be a non-negative integer")
        if declared_maximum is not None and declared_maximum > maximum:
            raise _error(f"{path}.max{suffix}", f"must not exceed {maximum}")
        if minimum is not None and minimum > maximum:
            raise _error(f"{path}.min{suffix}", f"must not exceed {maximum}")
        if minimum is not None and declared_maximum is not None and minimum > declared_maximum:
            raise _error(f"{path}.min{suffix}", f"must not exceed max{suffix}")

    @staticmethod
    def _validate_numeric_bounds(schema: dict[str, Any], path: str) -> None:
        keywords = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
        for keyword in keywords:
            if keyword in schema and not _is_finite_number(schema[keyword]):
                raise _error(f"{path}.{keyword}", "must be a finite number, not a boolean")

        lower_bounds = [
            (schema[keyword], keyword.startswith("exclusive"))
            for keyword in ("minimum", "exclusiveMinimum")
            if keyword in schema
        ]
        upper_bounds = [
            (schema[keyword], keyword.startswith("exclusive"))
            for keyword in ("maximum", "exclusiveMaximum")
            if keyword in schema
        ]
        if not lower_bounds or not upper_bounds:
            return
        lower_value = max(value for value, _ in lower_bounds)
        upper_value = min(value for value, _ in upper_bounds)
        lower_exclusive = any(
            exclusive for value, exclusive in lower_bounds if value == lower_value
        )
        upper_exclusive = any(
            exclusive for value, exclusive in upper_bounds if value == upper_value
        )
        if lower_value > upper_value or (
            lower_value == upper_value and (lower_exclusive or upper_exclusive)
        ):
            raise _error(path, "numeric bounds define an empty range")

    @staticmethod
    def _validate_annotations(schema: dict[str, Any], path: str) -> None:
        for keyword in ("title", "description"):
            if keyword not in schema:
                continue
            value = schema[keyword]
            if not isinstance(value, str):
                raise _error(f"{path}.{keyword}", "must be a string")
            if len(value) > MAX_STRING_LENGTH:
                raise _error(f"{path}.{keyword}", f"exceeds {MAX_STRING_LENGTH} characters")

    @staticmethod
    def _validate_enum_const_default(schema: dict[str, Any], path: str) -> None:
        if "enum" in schema:
            enum = schema["enum"]
            if not isinstance(enum, list) or not enum:
                raise _error(f"{path}.enum", "must be a non-empty array")
            if len(enum) > MAX_ENUM_VALUES:
                raise _error(
                    f"{path}.enum", f"exceeds maximum enum value count {MAX_ENUM_VALUES}"
                )
            _check_json_value(enum, f"{path}.enum", set())
            for index, value in enumerate(enum):
                try:
                    _normalize_value(schema, value, f"{path}.enum[{index}]", apply_defaults=False)
                except ValueError as error:
                    raise _error(f"{path}.enum[{index}]", "does not satisfy its schema") from error

        if "const" in schema:
            _check_json_value(schema["const"], f"{path}.const", set())
            try:
                _normalize_value(schema, schema["const"], f"{path}.const", apply_defaults=False)
            except ValueError as error:
                raise _error(f"{path}.const", "does not satisfy its schema") from error

        if "default" in schema:
            default_path = f"{path}.default"
            if _contains_sensitive_value(schema["default"]):
                raise _error(default_path, "must not contain sensitive field defaults")
            _check_json_value(schema["default"], default_path, set())
            try:
                _normalize_value(schema, schema["default"], default_path, apply_defaults=True)
            except ValueError as error:
                raise _error(default_path, "does not satisfy its schema") from error


def validate_schema(schema: object) -> None:
    """Validate a schema against OpenEvo's bounded, fail-closed subset."""

    validator = _SchemaValidator()
    validator.validate(schema)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise _error("schema.type", "root schema must have type=object")
    if schema.get("additionalProperties", _MISSING) is not False:
        raise _error("schema.additionalProperties", "root schema must be closed")


def validate_config_schema(schema: object) -> None:
    """Validate a method config schema against the bounded subset."""

    validate_schema(schema)


def _nullable_branch(schema: dict[str, Any]) -> dict[str, Any] | None:
    branches = schema.get("anyOf")
    if not isinstance(branches, list):
        return None
    return next(branch for branch in branches if branch != {"type": "null"})


def _normalize_value(
    schema: dict[str, Any],
    value: object,
    path: str,
    *,
    apply_defaults: bool,
    require_required: bool = True,
) -> object:
    if "anyOf" in schema:
        if value is None:
            normalized: object = None
        else:
            branch = _nullable_branch(schema)
            if branch is None:
                raise _error(path, "does not match nullable schema")
            normalized = _normalize_value(
                branch,
                value,
                path,
                apply_defaults=apply_defaults,
                require_required=require_required,
            )
    else:
        schema_type = schema["type"]
        normalized = _normalize_typed_value(
            schema,
            schema_type,
            value,
            path,
            apply_defaults=apply_defaults,
            require_required=require_required,
        )

    if "enum" in schema and not any(_json_equal(normalized, item) for item in schema["enum"]):
        raise _error(path, "must match an allowed enum value")
    if "const" in schema and not _json_equal(normalized, schema["const"]):
        raise _error(path, "must match the constant value")
    return normalized


def _normalize_typed_value(
    schema: dict[str, Any],
    schema_type: str,
    value: object,
    path: str,
    *,
    apply_defaults: bool,
    require_required: bool,
) -> object:
    if schema_type == "object":
        return _normalize_object(
            schema,
            value,
            path,
            apply_defaults=apply_defaults,
            require_required=require_required,
        )
    if schema_type == "array":
        return _normalize_array(
            schema,
            value,
            path,
            apply_defaults=apply_defaults,
            require_required=require_required,
        )
    if schema_type == "string":
        if not isinstance(value, str):
            raise _error(path, "must be a string")
        if len(value) > MAX_STRING_LENGTH:
            raise _error(path, f"string exceeds {MAX_STRING_LENGTH} characters")
        _check_length_bounds(schema, value, path)
        return value
    if schema_type == "boolean":
        if not isinstance(value, bool):
            raise _error(path, "must be a boolean")
        return value
    if schema_type == "null":
        if value is not None:
            raise _error(path, "must be null")
        return None
    if schema_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise _error(path, "must be an integer, not a boolean")
        _check_numeric_bounds(schema, value, path)
        return value
    if not _is_finite_number(value):
        raise _error(path, "must be a finite number, not a boolean")
    _check_numeric_bounds(schema, value, path)
    return value


def _normalize_object(
    schema: dict[str, Any],
    value: object,
    path: str,
    *,
    apply_defaults: bool,
    require_required: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, "must be an object")
    if len(value) > MAX_OBJECT_PROPERTIES:
        raise _error(path, f"object exceeds {MAX_OBJECT_PROPERTIES} properties")
    properties = schema.get("properties", {})
    for name in value:
        if not isinstance(name, str):
            raise _error(path, "property names must be strings")
        if name not in properties:
            raise _error(f"{path}.{name}", "unknown property")

    normalized: dict[str, Any] = {}
    for name, child_schema in properties.items():
        if name in value:
            normalized[name] = _normalize_value(
                child_schema,
                value[name],
                f"{path}.{name}",
                apply_defaults=apply_defaults,
                require_required=require_required,
            )
            if child_schema.get("x-openevo-secret-ref") is True:
                secret_reference = normalized[name]
                if (
                    not isinstance(secret_reference, str)
                    or _SECRET_REFERENCE_RE.fullmatch(secret_reference) is None
                ):
                    raise _error(
                        f"{path}.{name}",
                        "must be an opaque Core secret reference",
                    )
        elif apply_defaults and "default" in child_schema:
            normalized[name] = _normalize_value(
                child_schema,
                child_schema["default"],
                f"{path}.{name}",
                apply_defaults=True,
                require_required=require_required,
            )

    if require_required:
        for name in schema.get("required", []):
            if name not in normalized:
                raise _error(f"{path}.{name}", "required property is missing")
    return normalized


def _normalize_array(
    schema: dict[str, Any],
    value: object,
    path: str,
    *,
    apply_defaults: bool,
    require_required: bool,
) -> list[Any]:
    if not isinstance(value, list):
        raise _error(path, "must be an array")
    if len(value) > MAX_ARRAY_ITEMS:
        raise _error(path, f"array exceeds {MAX_ARRAY_ITEMS} items")
    minimum = schema.get("minItems", 0)
    maximum = schema.get("maxItems", MAX_ARRAY_ITEMS)
    if len(value) < minimum:
        raise _error(path, "array has fewer than minItems")
    if len(value) > maximum:
        raise _error(path, "array has more than maxItems")
    return [
        _normalize_value(
            schema["items"],
            item,
            f"{path}[{index}]",
            apply_defaults=apply_defaults,
            require_required=require_required,
        )
        for index, item in enumerate(value)
    ]


def _check_length_bounds(schema: dict[str, Any], value: str, path: str) -> None:
    if len(value) < schema.get("minLength", 0):
        raise _error(path, "string is shorter than minLength")
    if len(value) > schema.get("maxLength", MAX_STRING_LENGTH):
        raise _error(path, "string is longer than maxLength")


def _check_numeric_bounds(schema: dict[str, Any], value: int | float, path: str) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        raise _error(path, "number is below minimum")
    if "maximum" in schema and value > schema["maximum"]:
        raise _error(path, "number is above maximum")
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        raise _error(path, "number is not above exclusiveMinimum")
    if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
        raise _error(path, "number is not below exclusiveMaximum")


def normalize_config(schema: object, value: object) -> dict[str, Any]:
    """Validate and copy a config, recursively applying declared defaults."""

    validate_schema(schema)
    normalized = _normalize_value(schema, value, "config", apply_defaults=True)
    if not isinstance(normalized, dict):  # Root validation above makes this unreachable.
        raise _error("config", "root config must be an object")
    return normalized


def normalize_partial_config(schema: object, value: object) -> dict[str, Any]:
    """Validate a partial descriptor default without requiring user-owned fields."""

    validate_schema(schema)
    normalized = _normalize_value(
        schema,
        value,
        "config",
        apply_defaults=False,
        require_required=False,
    )
    if not isinstance(normalized, dict):  # Root validation makes this unreachable.
        raise _error("config", "root config must be an object")
    return normalized
