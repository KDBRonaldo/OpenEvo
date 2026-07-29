from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json

import pytest

from desktop.sidecar.contracts.v2.models import (
    CoreAuthorityEventPayloadV2,
    HostCatalogEventPayloadV2,
    LifecycleOperationEventPayloadV2,
)
from desktop.sidecar.event_broker_v2 import (
    DesktopEventBrokerClosedError,
    DesktopEventBrokerV2,
    DesktopEventCursorExpiredError,
    DesktopEventGapError,
)


def _clock() -> datetime:
    return datetime(2026, 7, 23, 6, tzinfo=timezone.utc)


def _payload(count: int = 1) -> HostCatalogEventPayloadV2:
    return HostCatalogEventPayloadV2(
        payload_kind="ssh_host_catalog_changed",
        catalog_generation=count,
        host_count=count,
        warning_count=0,
    )


def test_publish_binds_sequence_payload_digest_and_canonical_sse() -> None:
    broker = DesktopEventBrokerV2(
        clock=_clock,
        event_id_factory=lambda: "event-token",
    )
    event = broker.publish(_payload())
    payload_bytes = json.dumps(
        event.payload.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert event.sequence == 1
    assert event.event_id == "1.event-token"
    assert event.event_type == "ssh_host_catalog_changed"
    assert event.payload_sha256 == hashlib.sha256(payload_bytes).hexdigest()

    async def consume() -> bytes:
        subscription = broker.subscribe()
        broker.publish(_payload(2))
        try:
            return await subscription.__anext__()
        finally:
            await subscription.aclose()

    frame = asyncio.run(consume())
    assert frame.startswith(b"id: 2.event-token\nevent: ssh_host_catalog_changed\ndata: {")
    assert frame.endswith(b"\n\n")
    broker.close()


def test_replay_is_contiguous_and_evicted_cursor_fails_closed() -> None:
    broker = DesktopEventBrokerV2(
        max_events=2,
        max_subscriber_events=2,
        clock=_clock,
        event_id_factory=lambda: "replay-token",
    )
    first = broker.publish(_payload(1))
    second = broker.publish(_payload(2))
    broker.publish(_payload(3))

    async def replay() -> bytes:
        subscription = broker.subscribe(last_event_id=second.event_id)
        try:
            return await subscription.__anext__()
        finally:
            await subscription.aclose()

    assert b"3.replay-token" in asyncio.run(replay())
    with pytest.raises(DesktopEventCursorExpiredError):
        broker.subscribe(last_event_id=first.event_id)
    broker.close()


def test_slow_subscriber_is_sealed_on_gap_without_unbounded_queue() -> None:
    broker = DesktopEventBrokerV2(
        max_events=4,
        max_subscriber_events=1,
        clock=_clock,
    )
    subscription = broker.subscribe()
    broker.publish(_payload(1))
    broker.publish(_payload(2))

    async def consume() -> None:
        with pytest.raises(DesktopEventGapError):
            await subscription.__anext__()

    asyncio.run(consume())
    assert broker.subscriber_count == 0
    broker.close()


def test_lifecycle_operation_payload_is_published_to_live_subscriber() -> None:
    broker = DesktopEventBrokerV2(
        clock=_clock,
        event_id_factory=lambda: "lifecycle-token",
    )
    subscription = broker.subscribe()
    payload = LifecycleOperationEventPayloadV2(
        payload_kind="lifecycle_operation_changed",
        operation_id="operation-1",
        kind="project_create",
        status="running",
        phase="creating_remote_project",
        etag='"' + ("a" * 64) + '"',
        log_sequence_high_watermark=3,
    )

    event = broker.publish(payload)

    async def consume() -> bytes:
        try:
            return await subscription.__anext__()
        finally:
            await subscription.aclose()

    frame = asyncio.run(consume())
    assert event.event_type == "lifecycle_operation_changed"
    assert event.payload == payload
    assert b"event: lifecycle_operation_changed" in frame
    broker.close()


def test_exact_v2_payload_type_and_close_are_enforced() -> None:
    broker = DesktopEventBrokerV2(clock=_clock)
    with pytest.raises(TypeError):
        broker.publish({"payload_kind": "ssh_host_catalog_changed"})  # type: ignore[arg-type]

    event = broker.publish(
        CoreAuthorityEventPayloadV2(
            payload_kind="core_authority_changed",
            profile_id="profile-1",
            project_id="project-1",
            core_event_id="core-event-1",
            core_event_sequence=1,
            core_event_type="task_admitted",
            core_payload_sha256="a" * 64,
        )
    )
    assert event.event_type == "core_authority_changed"
    broker.close()
    broker.close()
    with pytest.raises(DesktopEventBrokerClosedError):
        broker.publish(_payload())
