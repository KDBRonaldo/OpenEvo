from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import secrets
import threading
import time
from typing import Final

from desktop.sidecar.contracts.v1.models import (
    EventDataV1,
    EventEnvelopeV1,
    HeartbeatEventV1,
    ResourceEventV1,
    StateEventV1,
)


MAX_EVENT_SEQUENCE: Final = 9_007_199_254_740_991
MAX_EVENT_FRAME_BYTES: Final = 1_048_576
DEFAULT_MAX_EVENTS: Final = 4_096
DEFAULT_MAX_SUBSCRIBER_EVENTS: Final = 512
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final = 15.0
DEFAULT_POLL_INTERVAL_SECONDS: Final = 0.05

_EVENT_NAMES = {
    "state_changed": "desktop.v1.state.changed",
    "resource_changed": "desktop.v1.resource.changed",
    "heartbeat": "desktop.v1.heartbeat",
}


class DesktopEventBrokerError(RuntimeError):
    """Base class for closed, renderer-safe broker failures."""


class DesktopEventBrokerClosedError(DesktopEventBrokerError):
    """The process-owned event authority has been sealed."""


class DesktopEventCursorExpiredError(DesktopEventBrokerError):
    """The requested event cursor is outside the bounded replay window."""


class DesktopEventGapError(DesktopEventBrokerError):
    """A subscriber cannot receive a contiguous sequence."""


@dataclass(slots=True)
class _Subscriber:
    token: str
    pending: deque[EventEnvelopeV1] = field(default_factory=deque)
    overflowed: bool = False
    closed: bool = False


class DesktopEventSubscriptionV1:
    """One bounded async SSE body owned by a DesktopEventBrokerV1."""

    def __init__(self, broker: DesktopEventBrokerV1, subscriber: _Subscriber) -> None:
        self._broker = broker
        self._subscriber = subscriber
        self._last_delivery = time.monotonic()
        self._closed = False

    def __aiter__(self) -> DesktopEventSubscriptionV1:
        return self

    async def __anext__(self) -> bytes:
        while True:
            outcome = self._broker._next_for(self._subscriber)
            if outcome is _CLOSED:
                self._closed = True
                raise StopAsyncIteration
            if outcome is _GAP:
                await self.aclose()
                raise DesktopEventGapError("Desktop event subscriber exceeded its replay bound")
            if isinstance(outcome, EventEnvelopeV1):
                self._last_delivery = time.monotonic()
                return _sse_frame(outcome)
            now = time.monotonic()
            if now - self._last_delivery >= self._broker.heartbeat_interval:
                self._last_delivery = now
                return b": heartbeat\n\n"
            await asyncio.sleep(
                min(
                    self._broker.poll_interval,
                    max(0.0, self._broker.heartbeat_interval - (now - self._last_delivery)),
                )
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._broker._unsubscribe(self._subscriber)


_CLOSED = object()
_GAP = object()
_EMPTY = object()


class DesktopEventBrokerV1:
    """Thread-safe bounded replay authority for the Desktop Local SSE route."""

    def __init__(
        self,
        *,
        max_events: int = DEFAULT_MAX_EVENTS,
        max_subscriber_events: int | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        event_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 2 <= max_events <= 100_000:
            raise ValueError("event replay capacity is outside the supported bound")
        if max_subscriber_events is None:
            max_subscriber_events = min(DEFAULT_MAX_SUBSCRIBER_EVENTS, max_events)
        if not 1 <= max_subscriber_events <= max_events:
            raise ValueError("subscriber capacity is outside the replay bound")
        if not 0 < heartbeat_interval <= 60:
            raise ValueError("heartbeat interval must be positive and at most 60 seconds")
        if not 0 < poll_interval <= 1:
            raise ValueError("poll interval must be positive and at most one second")
        self._max_events = max_events
        self._max_subscriber_events = max_subscriber_events
        self._heartbeat_interval = float(heartbeat_interval)
        self._poll_interval = float(poll_interval)
        self._event_id_factory = event_id_factory or (lambda: secrets.token_urlsafe(24))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._events: deque[EventEnvelopeV1] = deque(maxlen=max_events)
        self._subscribers: dict[str, _Subscriber] = {}
        self._next_sequence = 0
        self._closed = False

    @property
    def heartbeat_interval(self) -> float:
        return self._heartbeat_interval

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    @property
    def next_sequence(self) -> int:
        with self._lock:
            return self._next_sequence

    def publish(self, data: EventDataV1) -> EventEnvelopeV1:
        if not isinstance(data, (StateEventV1, ResourceEventV1, HeartbeatEventV1)):
            raise TypeError("Desktop event data must be a closed v1 event model")
        with self._lock:
            self._require_open()
            if self._next_sequence > MAX_EVENT_SEQUENCE:
                raise DesktopEventGapError("Desktop event sequence is exhausted")
            occurred_at = _timestamp(self._clock())
            sequence = self._next_sequence
            raw_id = self._event_id_factory()
            if type(raw_id) is not str:
                raise ValueError("event identity factory must return a string")
            event = EventEnvelopeV1(
                event_id=f"{sequence:x}.{raw_id}",
                event_name=_EVENT_NAMES[data.kind],
                occurred_at=occurred_at,
                sequence=sequence,
                data=data,
            )
            _sse_frame(event)
            self._next_sequence += 1
            self._events.append(event)
            for subscriber in self._subscribers.values():
                if subscriber.closed or subscriber.overflowed:
                    continue
                if len(subscriber.pending) >= self._max_subscriber_events:
                    subscriber.pending.clear()
                    subscriber.overflowed = True
                    continue
                subscriber.pending.append(event)
            return event

    def subscribe(self, last_event_id: str | None = None) -> DesktopEventSubscriptionV1:
        with self._lock:
            self._require_open()
            replay: tuple[EventEnvelopeV1, ...] = ()
            if last_event_id is not None:
                event_ids = tuple(event.event_id for event in self._events)
                try:
                    position = event_ids.index(last_event_id)
                except ValueError as exc:
                    raise DesktopEventCursorExpiredError(
                        "Desktop event cursor is outside the replay window"
                    ) from exc
                replay = tuple(self._events)[position + 1 :]
                if len(replay) > self._max_subscriber_events:
                    raise DesktopEventCursorExpiredError(
                        "Desktop event cursor exceeds the subscriber replay bound"
                    )
            token = secrets.token_urlsafe(24)
            while token in self._subscribers:
                token = secrets.token_urlsafe(24)
            subscriber = _Subscriber(token=token, pending=deque(replay))
            self._subscribers[token] = subscriber
            return DesktopEventSubscriptionV1(self, subscriber)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for subscriber in self._subscribers.values():
                subscriber.closed = True
                subscriber.pending.clear()
            self._subscribers.clear()
            self._events.clear()

    def _next_for(self, subscriber: _Subscriber) -> object:
        with self._lock:
            current = self._subscribers.get(subscriber.token)
            if self._closed or current is not subscriber or subscriber.closed:
                return _CLOSED
            if subscriber.overflowed:
                return _GAP
            if subscriber.pending:
                return subscriber.pending.popleft()
            return _EMPTY

    def _unsubscribe(self, subscriber: _Subscriber) -> None:
        with self._lock:
            current = self._subscribers.get(subscriber.token)
            if current is subscriber:
                del self._subscribers[subscriber.token]
            subscriber.closed = True
            subscriber.pending.clear()

    def _require_open(self) -> None:
        if self._closed:
            raise DesktopEventBrokerClosedError("Desktop event broker is closed")


def _timestamp(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("event clock must return a timezone-aware datetime")
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sse_frame(event: EventEnvelopeV1) -> bytes:
    payload = event.model_dump_json().encode("utf-8")
    frame = (
        b"id: "
        + event.event_id.encode("utf-8")
        + b"\nevent: "
        + event.event_name.encode("ascii")
        + b"\ndata: "
        + payload
        + b"\n\n"
    )
    if len(frame) > MAX_EVENT_FRAME_BYTES:
        raise DesktopEventGapError("Desktop event frame exceeds the v1 byte bound")
    return frame


__all__ = (
    "DesktopEventBrokerClosedError",
    "DesktopEventBrokerV1",
    "DesktopEventCursorExpiredError",
    "DesktopEventGapError",
    "DesktopEventSubscriptionV1",
)
