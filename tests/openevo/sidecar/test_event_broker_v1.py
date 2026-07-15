from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gc
import json
import threading
import weakref

import pytest
from pydantic import ConfigDict

from desktop.sidecar.contracts.v1.models import (
    ContractNegotiationV1,
    CoreConnectionStateV1,
    DesktopStateV1,
    ResourceEventV1,
    ResourceRefV1,
    StateEventV1,
)
from desktop.sidecar.event_broker_v1 import (
    DesktopEventCapacityError,
    DesktopEventBrokerClosedError,
    DesktopEventBrokerV1,
    DesktopEventCursorExpiredError,
    DesktopEventGapError,
    DesktopEventReentrantPublishError,
    DesktopEventSubscriberLimitError,
)
from desktop.sidecar.release_capabilities import RELEASE_EXECUTION_MODE_CAPABILITIES_V1


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


def _large_state(operation_count: int = 3_870) -> StateEventV1:
    pending_operation_ids = tuple(
        f"operation-{index:04d}-" + "x" * 241 for index in range(operation_count)
    )
    return StateEventV1(
        state=DesktopStateV1(
            observed_at="2026-07-14T12:00:00.000000Z",
            contract=ContractNegotiationV1(
                selected_major=1,
                desktop_openapi_sha256="a" * 64,
                compatible=False,
            ),
            execution_mode_capabilities=RELEASE_EXECUTION_MODE_CAPABILITIES_V1,
            core=CoreConnectionStateV1(
                state="disconnected",
                active_tunnel=False,
            ),
            pending_operation_ids=pending_operation_ids,
        )
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


@pytest.mark.asyncio
async def test_large_state_event_preserves_strict_tuples_and_is_detached() -> None:
    broker = _broker()
    source = _large_state()
    subscription = broker.subscribe()

    published = broker.publish(source)
    frame = await anext(subscription)
    payload = _decode_frame(frame)[2]

    assert len(frame) > 1_000_000
    assert published.sequence == 0
    assert isinstance(published.data.state.pending_operation_ids, tuple)
    assert payload == published.model_dump(mode="json")

    object.__setattr__(source.state, "pending_operation_ids", ("mutated",))
    object.__setattr__(published.data.state, "pending_operation_ids", ("returned",))
    replay = broker.subscribe(published.event_id)
    next_event = broker.publish(_resource("change-after-large-state"))
    assert _decode_frame(await anext(replay))[2] == next_event.model_dump(mode="json")

    await subscription.aclose()
    await replay.aclose()


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


def test_publish_revalidates_corrupted_strict_tuple_without_json_coercion() -> None:
    broker = _broker()
    source = _large_state(operation_count=1)
    object.__setattr__(source.state, "pending_operation_ids", ["corrupted-list"])

    with pytest.raises(ValueError, match="tuple"):
        broker.publish(source)

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


def test_cross_thread_synchronous_callback_publish_does_not_deadlock() -> None:
    broker: DesktopEventBrokerV1
    callback_calls = 0
    callback_lock = threading.Lock()
    nested_done = threading.Event()
    nested_events = []

    def event_id() -> str:
        nonlocal callback_calls
        with callback_lock:
            callback_calls += 1
            call = callback_calls
        if call == 1:
            def publish_nested() -> None:
                nested_events.append(broker.publish(_resource("nested-change")))
                nested_done.set()

            threading.Thread(target=publish_nested, daemon=True).start()
            assert nested_done.wait(2), "cross-thread callback publication deadlocked"
        return f"event-{call}"

    broker = DesktopEventBrokerV1(event_id_factory=event_id, clock=lambda: NOW)
    outer = broker.publish(_resource("outer-change"))

    assert len(nested_events) == 1
    assert nested_events[0].sequence == 0
    assert outer.sequence == 1
    assert broker.next_sequence == 2


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


def test_callback_close_prevents_outer_publish_without_consuming_sequence() -> None:
    broker: DesktopEventBrokerV1

    def event_id() -> str:
        broker.close()
        return "event-after-close"

    broker = DesktopEventBrokerV1(event_id_factory=event_id, clock=lambda: NOW)

    with pytest.raises(DesktopEventBrokerClosedError):
        broker.publish(_resource("change-1"))

    assert broker.next_sequence == 0


def test_callback_failure_does_not_consume_sequence() -> None:
    callback_calls = 0

    def event_id() -> str:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 1:
            raise RuntimeError("identity unavailable")
        return "event-2"

    broker = DesktopEventBrokerV1(event_id_factory=event_id, clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="identity unavailable"):
        broker.publish(_resource("change-1"))
    published = broker.publish(_resource("change-2"))

    assert published.sequence == 0
    assert broker.next_sequence == 1


def test_close_linearizes_before_publish_commit_while_callback_is_running() -> None:
    callback_started = threading.Event()
    release_callback = threading.Event()
    close_done = threading.Event()
    outcomes: list[object] = []

    def event_id() -> str:
        callback_started.set()
        assert release_callback.wait(2)
        return "event-1"

    broker = DesktopEventBrokerV1(event_id_factory=event_id, clock=lambda: NOW)

    def publish() -> None:
        try:
            outcomes.append(broker.publish(_resource("change-1")))
        except BaseException as exc:
            outcomes.append(exc)

    publisher = threading.Thread(target=publish)
    publisher.start()
    assert callback_started.wait(2)

    closer = threading.Thread(target=lambda: (broker.close(), close_done.set()))
    closer.start()
    assert close_done.wait(2), "close blocked on an untrusted publication callback"
    release_callback.set()
    publisher.join(2)
    closer.join(2)

    assert not publisher.is_alive()
    assert isinstance(outcomes[0], DesktopEventBrokerClosedError)
    assert broker.next_sequence == 0


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


@pytest.mark.asyncio
async def test_ledger_byte_budget_evicts_oldest_complete_frames() -> None:
    sizing_broker = _broker()
    sizing_subscription = sizing_broker.subscribe()
    sizing_broker.publish(_resource("sizing-change"))
    frame_bytes = len(await anext(sizing_subscription))
    await sizing_subscription.aclose()

    broker = _broker(max_events=10, max_ledger_bytes=frame_bytes * 2)
    first = broker.publish(_resource("change-1"))
    second = broker.publish(_resource("change-2"))
    third = broker.publish(_resource("change-3"))

    with pytest.raises(DesktopEventCursorExpiredError):
        broker.subscribe(first.event_id)
    replay = broker.subscribe(second.event_id)
    assert _decode_frame(await anext(replay))[2] == third.model_dump(mode="json")
    await replay.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget_name", "budget_value"),
    (("max_queued_events", 1), ("max_queued_bytes", 1)),
)
async def test_global_subscriber_queue_budgets_fail_excess_subscriber_closed(
    budget_name: str,
    budget_value: int,
) -> None:
    broker = _broker(
        max_subscribers=2,
        max_subscriber_events=10,
        **{budget_name: budget_value},
    )
    first = broker.subscribe()
    second = broker.subscribe()
    published = broker.publish(_resource("change-1"))

    if budget_name == "max_queued_events":
        assert _decode_frame(await anext(first))[2] == published.model_dump(mode="json")
        with pytest.raises(DesktopEventGapError):
            await anext(second)
    else:
        with pytest.raises(DesktopEventGapError):
            await anext(first)
        with pytest.raises(DesktopEventGapError):
            await anext(second)

    await first.aclose()
    await second.aclose()


@pytest.mark.asyncio
async def test_global_queue_budget_is_released_by_delivery_and_asgi_cleanup() -> None:
    broker = _broker(max_queued_events=1, max_queued_bytes=1_048_576)
    first = broker.subscribe()
    broker.publish(_resource("change-1"))
    await anext(first)
    broker.publish(_resource("change-2"))
    assert _decode_frame(await anext(first))[2]["data"]["change_id"] == "change-2"
    await first.aclose()

    iterator = broker.subscribe()
    try:
        broker.publish(_resource("change-3"))
        await anext(iterator)
    finally:
        await iterator.aclose()
    assert broker.subscriber_count == 0

    replacement = broker.subscribe()
    broker.publish(_resource("change-4"))
    assert _decode_frame(await anext(replacement))[2]["data"]["change_id"] == "change-4"
    await replacement.aclose()


def test_memory_budget_configuration_has_hard_upper_bounds() -> None:
    with pytest.raises(ValueError, match="ledger byte"):
        _broker(max_ledger_bytes=1_073_741_825)
    with pytest.raises(ValueError, match="queued event"):
        _broker(max_queued_events=1_000_001)
    with pytest.raises(ValueError, match="queued byte"):
        _broker(max_queued_bytes=1_073_741_825)


def test_frame_larger_than_ledger_budget_fails_without_consuming_sequence() -> None:
    broker = _broker(max_ledger_bytes=1)

    with pytest.raises(DesktopEventCapacityError, match="ledger byte"):
        broker.publish(_resource("change-1"))

    assert broker.next_sequence == 0


def test_naive_clock_is_rejected_without_publishing_partial_state() -> None:
    ids = _ids()
    broker = DesktopEventBrokerV1(
        event_id_factory=lambda: next(ids),
        clock=lambda: datetime(2026, 7, 14, 12, 0),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        broker.publish(_resource("change-1"))

    assert broker.next_sequence == 0
