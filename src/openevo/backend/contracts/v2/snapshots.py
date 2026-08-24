"""Deterministic serialization for shared WebUI wire models."""

from __future__ import annotations

import hashlib
import json
from typing import TypeVar

from .models import ContractModel

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
    *,
    max_depth: int = MAX_CONTRACT_JSON_DEPTH,
    max_nodes: int = MAX_CONTRACT_JSON_NODES,
    max_collection_items: int = MAX_CONTRACT_JSON_COLLECTION_ITEMS,
) -> _ContractT:
    """Budget raw JSON before validating one strict closed contract model."""

    if type(payload) is not bytes:
        raise TypeError("contract JSON payload must be exact bytes")
    for label, value in (
        ("depth", max_depth),
        ("node", max_nodes),
        ("collection item", max_collection_items),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"contract JSON {label} limit must be a positive integer")
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
        if node_count > max_nodes:
            raise ValueError("contract JSON exceeds the node limit")
        if depth > max_depth:
            raise ValueError("contract JSON exceeds the depth limit")
        if isinstance(value, dict):
            collection_items += len(value)
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            collection_items += len(value)
            stack.extend((child, depth + 1) for child in value)
        if collection_items > max_collection_items:
            raise ValueError("contract JSON exceeds the collection item limit")
    return model.model_validate(decoded)


__all__ = [
    "MAX_CONTRACT_JSON_BYTES",
    "canonical_contract_bytes",
    "canonical_contract_sha256",
    "canonical_json_bytes",
    "deterministic_sha256",
    "parse_contract_json_bytes",
]
