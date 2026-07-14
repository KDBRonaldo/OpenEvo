from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gc
import json
import weakref

import pytest
from pydantic import ConfigDict

from desktop.sidecar.contracts.v1.models import (
    ResourceEventV1,
    ResourceRefV1,
)
from desktop.sidecar.event_broker_v1 import (
    DesktopEventBrokerClosedError,
    DesktopEventBrokerV1,
    DesktopEventCursorExpiredError,
    DesktopEventGapError,
    DesktopEventReentrantPublishError,
    DesktopEventSubscriberLimitError,
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


class _MutableResourceEventV1(ResourceEventV1):
    model_config = ConfigDict(frozen=False)


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
async def test_published_frame_is_detached_from_returned_and_input_models() -> None:
    broker = _broker()
    first = broker.publish(_resource("change-1"))
    original_cursor = first.event_id
    source = _resource("change-2")
    live_subscription = broker.subscribe()
    published = broker.publish(source)
    expected_payload = published.model_dump(mode="json")

    object.__setattr__(first, "event_id", "mutated-cursor")
    object.__setattr__(source, "change_id", "mutated-change")
    object.__setattr__(source.resource, "resource_id", "mutated-resource")
    object.__setattr__(published, "event_id", "mutated-published-event")
    object.__setattr__(published.data, "change_id", "mutated-published-change")

    live_frame = await anext(live_subscription)
    replay_subscription = broker.subscribe(original_cursor)
    replay_frame = await anext(replay_subscription)

    assert live_frame == replay_frame
    assert _decode_frame(live_frame)[2] == expected_payload
    await live_subscription.aclose()
    await replay_subscription.aclose()


def test_publish_rejects_event_model_subclasses_without_consuming_sequence() -> None:
    broker = _broker()
    mutable = _MutableResourceEventV1(
        authority="desktop",
        resource=ResourceRefV1(resource_type="operation", resource_id="operation-1"),
        change="updated",
        change_id="change-1",
        resource_etag=ETAG,
    )
    mutable.change_id = "change-2"

    with pytest.raises(TypeError, match="exact frozen v1"):
        broker.publish(mutable)

    assert broker.next_sequence == 0


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


@pytest.mark.asyncio
async def test_cancel_close_and_gc_unregister_subscribers() -> None:
    broker = _broker(poll_interval=0.001)
    cancelled_before_start = broker.subscribe()
    pending = asyncio.create_task(anext(cancelled_before_start))
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert broker.subscriber_count == 0

    cancelled_while_polling = broker.subscribe()
    pending = asyncio.create_task(anext(cancelled_while_polling))
    await asyncio.sleep(0)
    assert broker.subscriber_count == 1

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert broker.subscriber_count == 0

    disconnected = broker.subscribe()
    assert broker.subscriber_count == 1
    await disconnected.aclose()
    assert broker.subscriber_count == 0

    abandoned = broker.subscribe()
    abandoned_ref = weakref.ref(abandoned)
    assert broker.subscriber_count == 1
    del abandoned
    gc.collect()
    assert abandoned_ref() is None
    assert broker.subscriber_count == 0


@pytest.mark.asyncio
async def test_subscriber_hard_limit_fails_closed_and_released_slot_is_reusable() -> None:
    broker = _broker(max_subscribers=2)
    first = broker.subscribe()
    second = broker.subscribe()

    with pytest.raises(DesktopEventSubscriberLimitError):
        broker.subscribe()
    assert broker.subscriber_count == 2

    await first.aclose()
    replacement = broker.subscribe()
    assert broker.subscriber_count == 2

    await second.aclose()
    await replacement.aclose()


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


def test_reentrant_event_id_factory_cannot_reuse_reserved_sequence() -> None:
    broker: DesktopEventBrokerV1
    calls = 0

    def event_id() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            with pytest.raises(DesktopEventReentrantPublishError):
                broker.publish(_resource("nested-change"))
        return f"event-{calls}"

    broker = DesktopEventBrokerV1(event_id_factory=event_id, clock=lambda: NOW)
    first = broker.publish(_resource("change-1"))
    second = broker.publish(_resource("change-2"))

    assert (first.sequence, second.sequence) == (0, 1)
    assert (first.event_id, second.event_id) == ("0.event-1", "1.event-2")
    assert broker.next_sequence == 2


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
    assert broker.subscriber_count == 0


def test_naive_clock_is_rejected_without_publishing_partial_state() -> None:
    ids = _ids()
    broker = DesktopEventBrokerV1(
        event_id_factory=lambda: next(ids),
        clock=lambda: datetime(2026, 7, 14, 12, 0),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        broker.publish(_resource("change-1"))

    assert broker.next_sequence == 0
