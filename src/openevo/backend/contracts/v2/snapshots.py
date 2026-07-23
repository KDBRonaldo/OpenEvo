"""Deterministic v2 contract serialization and schema builders."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, TypeVar

from .app import create_core_control_v2_contract_app
from .models import ContractModel, SseFrameV2


CONTRACT_DIRECTORY = Path(__file__).resolve().parent
OPENAPI_SNAPSHOT_PATH = CONTRACT_DIRECTORY / "openapi.json"
EVENTS_SCHEMA_SNAPSHOT_PATH = CONTRACT_DIRECTORY / "events.schema.json"
MAX_CONTRACT_JSON_BYTES = 1024 * 1024
MAX_CONTRACT_JSON_DEPTH = 16
MAX_CONTRACT_JSON_NODES = 8192
MAX_CONTRACT_JSON_COLLECTION_ITEMS = 4096
_ContractT = TypeVar("_ContractT", bound=ContractModel)


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


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def parse_contract_json_bytes(
    model: type[_ContractT],
    payload: bytes,
) -> _ContractT:
    """Budget raw JSON before validating one strict closed contract model."""

    if type(payload) is not bytes:
        raise TypeError("contract JSON payload must be exact bytes")
    if len(payload) > MAX_CONTRACT_JSON_BYTES:
        raise ValueError("contract JSON exceeds the byte limit")
    try:
        decoded = json.loads(
            payload.decode("utf-8", errors="strict"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("contract JSON is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("contract JSON must contain one object")

    stack: list[tuple[object, int]] = [(decoded, 1)]
    node_count = 0
    collection_items = 0
    while stack:
        value, depth = stack.pop()
        node_count += 1
        if node_count > MAX_CONTRACT_JSON_NODES:
            raise ValueError("contract JSON exceeds the node limit")
        if depth > MAX_CONTRACT_JSON_DEPTH:
            raise ValueError("contract JSON exceeds the depth limit")
        if isinstance(value, dict):
            collection_items += len(value)
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            collection_items += len(value)
            stack.extend((child, depth + 1) for child in value)
        if collection_items > MAX_CONTRACT_JSON_COLLECTION_ITEMS:
            raise ValueError("contract JSON exceeds the collection item limit")
    return model.model_validate(decoded)


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


def write_contract_snapshots() -> None:
    """Mechanically regenerate both checked-in v2 schema snapshots."""

    OPENAPI_SNAPSHOT_PATH.write_bytes(canonical_json_bytes(build_openapi_document()))
    EVENTS_SCHEMA_SNAPSHOT_PATH.write_bytes(
        canonical_json_bytes(build_events_schema_document())
    )


__all__ = [
    "EVENTS_SCHEMA_SNAPSHOT_PATH",
    "MAX_CONTRACT_JSON_BYTES",
    "OPENAPI_SNAPSHOT_PATH",
    "build_events_schema_document",
    "build_openapi_document",
    "canonical_contract_bytes",
    "canonical_contract_sha256",
    "canonical_json_bytes",
    "deterministic_sha256",
    "events_schema_sha256",
    "openapi_sha256",
    "parse_contract_json_bytes",
    "write_contract_snapshots",
]
