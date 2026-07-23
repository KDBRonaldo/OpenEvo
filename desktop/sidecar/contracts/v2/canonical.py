"""Canonical schema snapshots and digests for Desktop Local API v2."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, cast

from .app import desktop_local_v2_contract_app
from .models import DesktopSseFrameV2


_SNAPSHOT_ROOT = Path(__file__).resolve().parent
OPENAPI_SNAPSHOT_PATH = _SNAPSHOT_ROOT / "openapi.json"
EVENTS_SCHEMA_SNAPSHOT_PATH = _SNAPSHOT_ROOT / "events.schema.json"
JsonDocument = dict[str, Any]


def canonical_json_bytes(document: JsonDocument) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(document: JsonDocument) -> str:
    return sha256(canonical_json_bytes(document)).hexdigest()


def desktop_openapi_document() -> JsonDocument:
    return cast(JsonDocument, desktop_local_v2_contract_app.openapi())


def desktop_events_schema_document() -> JsonDocument:
    generated = cast(
        JsonDocument,
        DesktopSseFrameV2.model_json_schema(
            mode="validation",
            ref_template="#/$defs/{model}",
        ),
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://openevo.ai/contracts/desktop/v2/events.schema.json",
        "x-openevo-contract-only": True,
        **generated,
    }


def desktop_openapi_sha256() -> str:
    return canonical_sha256(desktop_openapi_document())


def desktop_events_schema_sha256() -> str:
    return canonical_sha256(desktop_events_schema_document())


def write_contract_snapshots() -> None:
    OPENAPI_SNAPSHOT_PATH.write_bytes(canonical_json_bytes(desktop_openapi_document()))
    EVENTS_SCHEMA_SNAPSHOT_PATH.write_bytes(
        canonical_json_bytes(desktop_events_schema_document())
    )


DESKTOP_OPENAPI_SHA256 = desktop_openapi_sha256()
DESKTOP_EVENTS_SCHEMA_SHA256 = desktop_events_schema_sha256()


__all__ = [
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
    "write_contract_snapshots",
]
