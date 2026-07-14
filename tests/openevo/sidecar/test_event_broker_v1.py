from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json

import pytest

from desktop.sidecar.contracts.v1.models import (
    ResourceEventV1,
    ResourceRefV1,
)
from desktop.sidecar.event_broker_v1 import (
    DesktopEventBrokerClosedError,
    DesktopEventBrokerV1,
    DesktopEventCursorExpiredError,
    DesktopEventGapError,
)


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
ETAG = '"' + "a" * 64 + '"'


def _ids() -> Iterator[str]:
    index = 0
    while True:
        index += 1
        yield f"event-{index:04d}"


def _resource(change_id: str) -> ResourceEventV1:
    return ResourceEventV1(
        authority="desktop",
        resource=ResourceRefV1(resource_type="operation", resource_id="operation-1"),
        change="updated",
        change_id=change_id,
        resource_etag=ETAG,
    )


def _broker(**kwargs: object) -> DesktopEventBrokerV1:
    ids = _ids()
    return DesktopEventBrokerV1(
        event_id_factory=lambda: next(ids),
        clock=lambda: NOW,
        **kwargs,
    )


def _decode_frame(frame: bytes) -> tuple[str, str, dict[str, object]]:
    fields: dict[str, str] = {}
    for line in frame.decode("utf-8").strip().splitlines():
        name, value = line.split(": ", 1)
        fields[name] = value
    return fields["id"], fields["event"], json.loads(fields["data"])


@pytest.mark.asyncio
async def test_subscription_replays_after_exact_cursor_then_delivers_live_events() -> None:
    broker = _broker()
    first = broker.publish(_resource("change-1"))
    second = broker.publish(_resource("change-2"))
    subscription = broker.subscribe(first.event_id)

    replay = await anext(subscription)
    event_id, event_name, payload = _decode_frame(replay)
    assert event_id == second.event_id
    assert event_name == second.event_name
    assert payload == second.model_dump(mode="json")

    third = broker.publish(_resource("change-3"))
    live = await anext(subscription)
    assert _decode_frame(live)[2] == third.model_dump(mode="json")
    await subscription.aclose()


@pytest.mark.asyncio
async def test_new_subscription_starts_at_live_head_without_replaying_history() -> None:
    broker = _broker(heartbeat_interval=0.01, poll_interval=0.001)
    broker.publish(_resource("old-change"))
    subscription = broker.subscribe()

    assert await anext(subscription) == b": heartbeat\n\n"
    current = broker.publish(_resource("current-change"))
    assert _decode_frame(await anext(subscription))[2] == current.model_dump(mode="json")
    await subscription.aclose()


def test_unknown_and_evicted_event_cursors_fail_before_streaming() -> None:
    broker = _broker(max_events=2)
    evicted = broker.publish(_resource("change-1"))
    broker.publish(_resource("change-2"))
    broker.publish(_resource("change-3"))

    with pytest.raises(DesktopEventCursorExpiredError):
        broker.subscribe(evicted.event_id)
    with pytest.raises(DesktopEventCursorExpiredError):
        broker.subscribe("unknown-event")


@pytest.mark.asyncio
async def test_slow_subscriber_overflow_fails_closed() -> None:
    broker = _broker(max_subscriber_events=2)
    subscription = broker.subscribe()
    broker.publish(_resource("change-1"))
    broker.publish(_resource("change-2"))
    broker.publish(_resource("change-3"))

    with pytest.raises(DesktopEventGapError):
        await anext(subscription)
    await subscription.aclose()


def test_publish_is_thread_safe_and_sequences_are_contiguous() -> None:
    broker = _broker(max_events=128)
    with ThreadPoolExecutor(max_workers=8) as executor:
        events = tuple(
            executor.map(
                lambda index: broker.publish(_resource(f"change-{index}")),
                range(64),
            )
        )

    assert sorted(event.sequence for event in events) == list(range(64))
    assert len({event.event_id for event in events}) == 64


def test_sequence_component_keeps_final_event_id_unique_and_close_fails_closed() -> None:
    broker = DesktopEventBrokerV1(
        event_id_factory=lambda: "same-event",
        clock=lambda: NOW,
    )
    first = broker.publish(_resource("change-1"))
    second = broker.publish(_resource("change-2"))

    assert first.event_id == "0.same-event"
    assert second.event_id == "1.same-event"
    assert first.event_id != second.event_id

    broker.close()
    broker.close()
    with pytest.raises(DesktopEventBrokerClosedError):
        broker.publish(_resource("change-3"))
    with pytest.raises(DesktopEventBrokerClosedError):
        broker.subscribe()


@pytest.mark.asyncio
async def test_broker_close_terminates_existing_subscriptions() -> None:
    broker = _broker(poll_interval=0.001)
    subscription = broker.subscribe()
    broker.close()

    with pytest.raises(StopAsyncIteration):
        await anext(subscription)


def test_naive_clock_is_rejected_without_publishing_partial_state() -> None:
    ids = _ids()
    broker = DesktopEventBrokerV1(
        event_id_factory=lambda: next(ids),
        clock=lambda: datetime(2026, 7, 14, 12, 0),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        broker.publish(_resource("change-1"))

    assert broker.next_sequence == 0
