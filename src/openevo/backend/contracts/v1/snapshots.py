"""Deterministic schema snapshot builders and SHA-256 helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .app import create_core_control_contract_app
from .models import EventEnvelopeV1


CONTRACT_DIRECTORY = Path(__file__).resolve().parent
OPENAPI_SNAPSHOT_PATH = CONTRACT_DIRECTORY / "openapi.json"
EVENTS_SCHEMA_SNAPSHOT_PATH = CONTRACT_DIRECTORY / "events.schema.json"


def canonical_json_bytes(document: object) -> bytes:
    """Serialize one JSON document into the checked-in canonical byte form."""

    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def deterministic_sha256(document: object) -> str:
    """Return the lowercase SHA-256 of a canonical JSON document."""

    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def build_openapi_document() -> dict[str, Any]:
    """Rebuild the Core Control API v1 OpenAPI document from contract source."""

    return create_core_control_contract_app().openapi()


def build_events_schema_document() -> dict[str, Any]:
    """Rebuild the standalone closed schema for SSE data envelopes."""

    schema = EventEnvelopeV1.model_json_schema(
        mode="validation",
        ref_template="#/$defs/{model}",
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://openevo.ai/contracts/core-control/v1/events.schema.json",
        "x-openevo-contract-only": True,
        **schema,
    }


def openapi_sha256() -> str:
    return deterministic_sha256(build_openapi_document())


def events_schema_sha256() -> str:
    return deterministic_sha256(build_events_schema_document())


def write_contract_snapshots() -> None:
    """Developer helper for mechanically regenerating both checked-in files."""

    OPENAPI_SNAPSHOT_PATH.write_bytes(canonical_json_bytes(build_openapi_document()))
    EVENTS_SCHEMA_SNAPSHOT_PATH.write_bytes(canonical_json_bytes(build_events_schema_document()))


__all__ = [
    "EVENTS_SCHEMA_SNAPSHOT_PATH",
    "OPENAPI_SNAPSHOT_PATH",
    "build_events_schema_document",
    "build_openapi_document",
    "canonical_json_bytes",
    "deterministic_sha256",
    "events_schema_sha256",
    "openapi_sha256",
    "write_contract_snapshots",
]
