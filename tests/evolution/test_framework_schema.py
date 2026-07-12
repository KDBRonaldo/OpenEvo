from __future__ import annotations

import math
import re

import pytest

from openevo.evolution.framework.schema import (
    MAX_ARRAY_ITEMS,
    MAX_ENUM_VALUES,
    MAX_OBJECT_PROPERTIES,
    MAX_SCHEMA_DEPTH,
    MAX_SCHEMA_NODES,
    MAX_STRING_LENGTH,
    normalize_config,
    normalize_config_override,
    normalize_partial_config,
    validate_config_schema,
    validate_schema,
)


def _object_schema(properties: dict | None = None, **keywords: object) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
        **keywords,
    }


def test_exports_exact_schema_limits() -> None:
    assert MAX_SCHEMA_DEPTH == 8
    assert MAX_SCHEMA_NODES == 256
    assert MAX_OBJECT_PROPERTIES == 64
    assert MAX_ENUM_VALUES == 128
    assert MAX_STRING_LENGTH == 4096
    assert MAX_ARRAY_ITEMS == 256
    assert validate_config_schema(_object_schema()) is None


def test_normalize_config_applies_nested_defaults_without_mutating_inputs() -> None:
    schema = _object_schema(
        {
            "name": {"type": "string", "default": "worker", "minLength": 1},
            "settings": _object_schema(
                {
                    "retries": {
                        "type": "integer",
                        "default": 2,
                        "minimum": 0,
                        "maximum": 5,
                    },
                    "ratio": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "exclusiveMaximum": 1,
                    },
                }
            ),
            "tags": {
                "type": "array",
                "items": {"type": "string", "maxLength": 8},
                "default": ["stable"],
                "minItems": 1,
                "maxItems": 3,
            },
            "mode": {"type": "string", "enum": ["fast", "safe"], "default": "safe"},
            "enabled": {"type": "boolean", "const": True},
            "note": {"anyOf": [{"type": "string", "maxLength": 20}, {"type": "null"}]},
        },
        required=["settings", "enabled"],
    )
    value = {"settings": {"ratio": 0.5}, "enabled": True, "note": None}

    normalized = normalize_config(schema, value)

    assert normalized == {
        "name": "worker",
        "settings": {"retries": 2, "ratio": 0.5},
        "tags": ["stable"],
        "mode": "safe",
        "enabled": True,
        "note": None,
    }
    assert normalized is not value
    assert normalized["settings"] is not value["settings"]
    assert value == {"settings": {"ratio": 0.5}, "enabled": True, "note": None}
    assert schema["properties"]["tags"]["default"] == ["stable"]


def test_normalize_config_override_deep_merges_defaults_and_requires_final_fields() -> None:
    schema = _object_schema(
        {
            "settings": _object_schema(
                {
                    "model": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1},
                    "retries": {"type": "integer", "default": 2},
                },
                required=["model", "timeout"],
            )
        },
        required=["settings"],
    )
    defaults = {"settings": {"model": "remote-model", "timeout": 30}}
    override = {"settings": {"timeout": 60}}

    assert normalize_config_override(schema, defaults, override) == {
        "settings": {
            "model": "remote-model",
            "timeout": 60,
            "retries": 2,
        }
    }
    assert defaults == {"settings": {"model": "remote-model", "timeout": 30}}
    assert override == {"settings": {"timeout": 60}}

    with pytest.raises(ValueError, match="required property is missing"):
        normalize_config_override(schema, {}, {"settings": {"timeout": 60}})


@pytest.mark.parametrize(
    ("schema", "path"),
    [
        ({"type": "string"}, "schema.type"),
        (
            {"type": "object", "additionalProperties": False},
            "schema.properties",
        ),
        ({"type": "object", "properties": {}}, "schema.additionalProperties"),
        (_object_schema(additionalProperties=True), "schema.additionalProperties"),
        (_object_schema({"x": {"type": "string", "format": "uri"}}), "schema.properties.x.format"),
        (_object_schema({"x": {"type": "string", "pattern": "x"}}), "schema.properties.x.pattern"),
        (_object_schema({"x": {"$ref": "#"}}), "schema.properties.x.$ref"),
        (_object_schema({"x": {"type": "string", "unknown": 1}}), "schema.properties.x.unknown"),
        (
            _object_schema({"x": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}),
            "schema.properties.x.anyOf",
        ),
        (
            _object_schema(
                {"x": {"anyOf": [{"type": "string"}, {"type": "null"}, {"type": "null"}]}}
            ),
            "schema.properties.x.anyOf",
        ),
    ],
)
def test_validate_schema_rejects_open_unsupported_or_unknown_constructs(
    schema: dict, path: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(path)):
        validate_schema(schema)


@pytest.mark.parametrize("bad_type", [["string", "null"], "any", 1, True])
def test_validate_schema_rejects_unsupported_type_forms(bad_type: object) -> None:
    with pytest.raises(ValueError, match=r"schema\.properties\.x\.type"):
        validate_schema(_object_schema({"x": {"type": bad_type}}))


def test_validate_schema_enforces_depth_node_property_enum_and_declared_bounds() -> None:
    child: dict = {"type": "string"}
    for index in range(MAX_SCHEMA_DEPTH):
        child = _object_schema({f"level_{index}": child})
    with pytest.raises(ValueError, match="maximum depth"):
        validate_schema(child)

    too_many_nodes = _object_schema(
        {
            f"group_{group}": _object_schema(
                {f"item_{item}": {"type": "string"} for item in range(64)}
            )
            for group in range(4)
        }
    )
    with pytest.raises(ValueError, match="maximum node count"):
        validate_schema(too_many_nodes)

    with pytest.raises(ValueError, match="maximum property count"):
        validate_schema(
            _object_schema(
                {f"field_{index}": {"type": "string"} for index in range(MAX_OBJECT_PROPERTIES + 1)}
            )
        )

    with pytest.raises(ValueError, match="maximum enum value count"):
        validate_schema(
            _object_schema(
                {"choice": {"type": "integer", "enum": list(range(MAX_ENUM_VALUES + 1))}}
            )
        )

    with pytest.raises(ValueError, match=r"maxLength"):
        validate_schema(
            _object_schema({"text": {"type": "string", "maxLength": MAX_STRING_LENGTH + 1}})
        )
    with pytest.raises(ValueError, match=r"maxItems"):
        validate_schema(
            _object_schema(
                {
                    "values": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "maxItems": MAX_ARRAY_ITEMS + 1,
                    }
                }
            )
        )


@pytest.mark.parametrize("number", [True, False, math.nan, math.inf, -math.inf])
def test_schema_rejects_non_finite_or_boolean_numeric_keywords(number: object) -> None:
    schema = _object_schema({"count": {"type": "number", "minimum": number}})
    with pytest.raises(ValueError, match=r"schema\.properties\.count\.minimum"):
        validate_schema(schema)


@pytest.mark.parametrize(
    "bounds",
    [
        {"minimum": 2, "maximum": 1},
        {"minimum": 1, "exclusiveMaximum": 1},
        {"exclusiveMinimum": 1, "maximum": 1},
        {"exclusiveMinimum": 1, "exclusiveMaximum": 1},
    ],
)
def test_schema_rejects_empty_numeric_ranges(bounds: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="empty range"):
        validate_schema(_object_schema({"count": {"type": "number", **bounds}}))


@pytest.mark.parametrize("field_name", ["api_key", "accessToken", "PASSWORD", "client_secret"])
@pytest.mark.parametrize(
    ("keyword", "embedded"),
    [
        ("default", "do-not-report"),
        ("enum", ["do-not-report"]),
        ("const", "do-not-report"),
    ],
)
def test_sensitive_fields_cannot_embed_values(
    field_name: str,
    keyword: str,
    embedded: object,
) -> None:
    schema = _object_schema({field_name: {"type": "string", keyword: embedded}})

    with pytest.raises(ValueError) as exc_info:
        validate_schema(schema)

    message = str(exc_info.value)
    assert f"schema.properties.{field_name}.{keyword}" in message
    assert "do-not-report" not in message


@pytest.mark.parametrize(
    ("keyword", "embedded"),
    [
        ("default", {"nested": {"password": "do-not-report"}}),
        ("enum", [{"nested": {"api_key_ref": "openevo-secret:private"}}]),
        ("const", {"nested": {"credential": "do-not-report"}}),
    ],
)
def test_ancestor_annotations_cannot_embed_sensitive_values(
    keyword: str,
    embedded: object,
) -> None:
    schema = _object_schema(
        {
            "wrapper": {
                **_object_schema(),
                keyword: embedded,
            }
        }
    )

    with pytest.raises(ValueError, match="sensitive") as exc_info:
        validate_schema(schema)

    assert "do-not-report" not in str(exc_info.value)
    assert "openevo-secret:private" not in str(exc_info.value)


def test_sensitive_config_requires_an_opaque_core_reference() -> None:
    schema = _object_schema(
        {
            "api_key_ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160,
                "x-openevo-secret-ref": True,
            }
        }
    )
    assert normalize_config(schema, {"api_key_ref": "openevo-secret:credential-1"}) == {
        "api_key_ref": "openevo-secret:credential-1"
    }
    with pytest.raises(ValueError, match="opaque Core secret reference"):
        normalize_config(schema, {"api_key_ref": "sk-raw-secret"})

    with pytest.raises(ValueError, match=r"opaque \*_ref"):
        validate_schema(_object_schema({"api_key": {"type": "string"}}))

    with pytest.raises(ValueError, match="opt into Core validation"):
        validate_schema(_object_schema({"ssh_key_ref": {"type": "string"}}))


@pytest.mark.parametrize("field_name", ["signing_key", "encryption_key", "ssh_key"])
def test_common_private_key_fields_cannot_bypass_secret_references(
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match=r"opaque \*_ref"):
        validate_schema(_object_schema({field_name: {"type": "string"}}))


def test_token_count_and_tokenizer_fields_are_not_misclassified_as_secrets() -> None:
    schema = _object_schema(
        {
            "max_tokens": {"type": "integer", "minimum": 1, "default": 256},
            "token_count": {"type": "integer", "minimum": 0},
            "tokenizer_name": {"type": "string", "default": "qwen"},
        }
    )
    assert normalize_config(schema, {"token_count": 12}) == {
        "max_tokens": 256,
        "token_count": 12,
        "tokenizer_name": "qwen",
    }


def test_default_must_validate_against_its_own_schema() -> None:
    schema = _object_schema(
        {"count": {"type": "integer", "minimum": 1, "maximum": 3, "default": 9}}
    )

    with pytest.raises(ValueError) as exc_info:
        validate_schema(schema)

    assert "schema.properties.count.default" in str(exc_info.value)
    assert "9" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ({"count": True}, "config.count"),
        ({"count": math.nan}, "config.count"),
        ({"count": math.inf}, "config.count"),
        ({"count": 4}, "config.count"),
        ({"name": "x" * (MAX_STRING_LENGTH + 1)}, "config.name"),
        ({"items": list(range(MAX_ARRAY_ITEMS + 1))}, "config.items"),
        ({"extra": "hidden-value"}, "config.extra"),
    ],
)
def test_normalize_config_rejects_invalid_values_without_echoing_them(
    value: dict, path: str
) -> None:
    schema = _object_schema(
        {
            "count": {"type": "number", "minimum": 0, "maximum": 3},
            "name": {"type": "string"},
            "items": {"type": "array", "items": {"type": "integer"}},
        }
    )

    with pytest.raises(ValueError) as exc_info:
        normalize_config(schema, value)

    message = str(exc_info.value)
    assert path in message
    assert "hidden-value" not in message


def test_normalize_config_requires_fields_after_applying_defaults() -> None:
    schema = _object_schema(
        {
            "with_default": {"type": "integer", "default": 1},
            "missing": {"type": "string"},
        },
        required=["with_default", "missing"],
    )

    with pytest.raises(ValueError, match=r"config\.missing"):
        normalize_config(schema, {})

    assert normalize_partial_config(schema, {}) == {}


def test_enum_and_const_comparison_do_not_treat_bool_as_number() -> None:
    schema = _object_schema(
        {
            "enum_value": {"type": "integer", "enum": [1]},
            "const_value": {"type": "integer", "const": 1},
        }
    )

    with pytest.raises(ValueError, match=r"config\.enum_value"):
        normalize_config(schema, {"enum_value": True, "const_value": 1})
    with pytest.raises(ValueError, match=r"config\.const_value"):
        normalize_config(schema, {"enum_value": 1, "const_value": True})
