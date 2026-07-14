from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import cast

from .app import contract_app
from .models import EventEnvelopeV1, JsonValueV1


_SNAPSHOT_ROOT = Path(__file__).resolve().parent
OPENAPI_SNAPSHOT_PATH = _SNAPSHOT_ROOT / "openapi.json"
EVENTS_SCHEMA_SNAPSHOT_PATH = _SNAPSHOT_ROOT / "events.schema.json"

JsonDocument = dict[str, JsonValueV1]


def canonical_json_bytes(document: JsonDocument) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(document: JsonDocument) -> str:
    return sha256(canonical_json_bytes(document)).hexdigest()


def desktop_openapi_document() -> JsonDocument:
    return cast(JsonDocument, contract_app.openapi())


def desktop_events_schema_document() -> JsonDocument:
    generated = cast(JsonDocument, EventEnvelopeV1.model_json_schema(mode="validation"))
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://openevo.dev/contracts/desktop/v1/events.schema.json",
        **generated,
    }


def desktop_openapi_sha256() -> str:
    return canonical_sha256(desktop_openapi_document())


def desktop_events_schema_sha256() -> str:
    return canonical_sha256(desktop_events_schema_document())


def load_canonical_snapshot(path: Path) -> JsonDocument:
    raw = path.read_bytes()
    loaded = cast(JsonDocument, json.loads(raw))
    if canonical_json_bytes(loaded) != raw:
        raise ValueError(f"contract snapshot is not canonical JSON: {path.name}")
    return loaded


def verify_contract_snapshots() -> tuple[str, str]:
    openapi = load_canonical_snapshot(OPENAPI_SNAPSHOT_PATH)
    events = load_canonical_snapshot(EVENTS_SCHEMA_SNAPSHOT_PATH)
    expected_openapi = desktop_openapi_document()
    expected_events = desktop_events_schema_document()
    if openapi != expected_openapi:
        raise ValueError("openapi.json does not match the contract app")
    if events != expected_events:
        raise ValueError("events.schema.json does not match EventEnvelopeV1")
    return canonical_sha256(openapi), canonical_sha256(events)


def write_contract_snapshots() -> tuple[str, str]:
    openapi = desktop_openapi_document()
    events = desktop_events_schema_document()
    OPENAPI_SNAPSHOT_PATH.write_bytes(canonical_json_bytes(openapi))
    EVENTS_SCHEMA_SNAPSHOT_PATH.write_bytes(canonical_json_bytes(events))
    return canonical_sha256(openapi), canonical_sha256(events)


DESKTOP_OPENAPI_SHA256 = desktop_openapi_sha256()
DESKTOP_EVENTS_SCHEMA_SHA256 = desktop_events_schema_sha256()


__all__ = (
    "DESKTOP_EVENTS_SCHEMA_SHA256",
    "DESKTOP_OPENAPI_SHA256",
    "EVENTS_SCHEMA_SNAPSHOT_PATH",
    "OPENAPI_SNAPSHOT_PATH",
    "canonical_json_bytes",
    "canonical_sha256",
    "desktop_events_schema_document",
    "desktop_events_schema_sha256",
    "desktop_openapi_document",
    "desktop_openapi_sha256",
    "load_canonical_snapshot",
    "verify_contract_snapshots",
    "write_contract_snapshots",
)
