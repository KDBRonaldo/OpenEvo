"""Deterministic v2 contract serialization and schema builders."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .app import create_core_control_v2_contract_app
from .models import ContractModel, SseFrameV2


CONTRACT_DIRECTORY = Path(__file__).resolve().parent
OPENAPI_SNAPSHOT_PATH = CONTRACT_DIRECTORY / "openapi.json"
EVENTS_SCHEMA_SNAPSHOT_PATH = CONTRACT_DIRECTORY / "events.schema.json"


def canonical_json_bytes(document: object) -> bytes:
    """Serialize one JSON document into the canonical checked-in byte form."""

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


def canonical_contract_bytes(contract: ContractModel) -> bytes:
    """Serialize only an already-validated closed v2 contract model."""

    if not isinstance(contract, ContractModel):
        raise TypeError("canonical_contract_bytes requires a ContractModel")
    return canonical_json_bytes(contract.model_dump(mode="json"))


def canonical_contract_sha256(contract: ContractModel) -> str:
    """Digest one already-validated closed v2 contract model."""

    return hashlib.sha256(canonical_contract_bytes(contract)).hexdigest()


def build_openapi_document() -> dict[str, Any]:
    """Build the Core Control API v2 OpenAPI document from model source."""

    return create_core_control_v2_contract_app().openapi()


def build_events_schema_document() -> dict[str, Any]:
    """Build the standalone closed schema for the v2 SSE wire frame."""

    schema = SseFrameV2.model_json_schema(
        mode="validation",
        ref_template="#/$defs/{model}",
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://openevo.ai/contracts/core-control/v2/events.schema.json",
        "x-openevo-contract-only": True,
        **schema,
    }


def openapi_sha256() -> str:
    return deterministic_sha256(build_openapi_document())


def events_schema_sha256() -> str:
    return deterministic_sha256(build_events_schema_document())


__all__ = [
    "EVENTS_SCHEMA_SNAPSHOT_PATH",
    "OPENAPI_SNAPSHOT_PATH",
    "build_events_schema_document",
    "build_openapi_document",
    "canonical_contract_bytes",
    "canonical_contract_sha256",
    "canonical_json_bytes",
    "deterministic_sha256",
    "events_schema_sha256",
    "openapi_sha256",
]
