"""Bounded replay authority for Desktop Local API v2 events."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import secrets
import threading
import time
from typing import Any, Final
import weakref

from pydantic import TypeAdapter, ValidationError

from desktop.sidecar.contracts.v2 import models as m


MAX_EVENT_SEQUENCE: Final = m.MAX_JAVASCRIPT_SAFE_INTEGER
MAX_EVENT_FRAME_BYTES: Final = 1_048_576
DEFAULT_MAX_EVENTS: Final = 4_096
DEFAULT_MAX_LEDGER_BYTES: Final = 16 * 1_048_576
MAX_LEDGER_BYTES: Final = 256 * 1_048_576
DEFAULT_MAX_SUBSCRIBER_EVENTS: Final = 512
DEFAULT_MAX_SUBSCRIBERS: Final = 256
MAX_SUBSCRIBERS: Final = 4_096
DEFAULT_MAX_QUEUED_EVENTS: Final = 16_384
MAX_QUEUED_EVENTS: Final = 262_144
DEFAULT_MAX_QUEUED_BYTES: Final = 64 * 1_048_576
MAX_QUEUED_BYTES: Final = 256 * 1_048_576
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final = 15.0
DEFAULT_POLL_INTERVAL_SECONDS: Final = 0.05

_PAYLOAD_TYPES = (
    m.HostCatalogEventPayloadV2,
    m.ProfileEventPayloadV2,
    m.CoreAuthorityEventPayloadV2,
    m.DiagnosticEventPayloadV2,
    m.LifecycleOperationEventPayloadV2,
)
_PAYLOAD_ADAPTER = TypeAdapter(m.DesktopEventPayloadV2)


class DesktopEventBrokerError(RuntimeError):
    """Base class for closed, renderer-safe v2 broker failures."""


class DesktopEventBrokerClosedError(DesktopEventBrokerError):
    """The process-owned event authority has been sealed."""


class DesktopEventCursorExpiredError(DesktopEventBrokerError):
    """The requested event cursor is outside the bounded replay window."""


class DesktopEventGapError(DesktopEventBrokerError):
    """A subscriber cannot receive a contiguous v2 sequence."""


class DesktopEventSubscriberLimitError(DesktopEventBrokerError):
    """The process-wide subscriber capacity has been reached."""


class DesktopEventCapacityError(DesktopEventBrokerError):
    """A process-wide event memory budget cannot admit more data."""


class DesktopEventReentrantPublishError(DesktopEventBrokerError):
    """A broker callback attempted to publish recursively."""


@dataclass(frozen=True, slots=True)
class _PublishedFrame:
    event_id: str
    frame: bytes


@dataclass(slots=True)
class _Subscriber:
    token: str = field(repr=False)
    pending: deque[_PublishedFrame] = field(default_factory=deque)
    pending_bytes: int = 0
    overflowed: bool = False
    closed: bool = False


class DesktopEventSubscriptionV2:
    """One bounded async SSE body owned by a DesktopEventBrokerV2."""

    def __init__(self, broker: DesktopEventBrokerV2, subscriber: _Subscriber) -> None:
        self._broker = broker
        self._subscriber = subscriber
        self._last_delivery = time.monotonic()
        self._closed = False
        self._finalizer = weakref.finalize(self, broker._unsubscribe, subscriber)
        self._finalizer.atexit = False

    def __aiter__(self) -> DesktopEventSubscriptionV2:
        return self

    def __anext__(self) -> Coroutine[Any, Any, bytes]:
        return _SubscriptionAdvance(self)

    async def _next_frame(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            while True:
                outcome = self._broker._next_for(self._subscriber)
                if outcome is _CLOSED:
                    raise StopAsyncIteration
                if outcome is _GAP:
                    raise DesktopEventGapError(
                        "Desktop v2 event subscriber exceeded its replay bound"
                    )
                if isinstance(outcome, _PublishedFrame):
                    self._last_delivery = time.monotonic()
                    return outcome.frame
                now = time.monotonic()
                if now - self._last_delivery >= self._broker.heartbeat_interval:
                    self._last_delivery = now
                    return b": heartbeat\n\n"
                await asyncio.sleep(
                    min(
                        self._broker.poll_interval,
                        max(
                            0.0,
                            self._broker.heartbeat_interval - (now - self._last_delivery),
                        ),
                    )
                )
        except BaseException:
            self._close()
            raise

    async def aclose(self) -> None:
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._finalizer()


class _SubscriptionAdvance(Coroutine[Any, Any, bytes]):
    """Cancellation-aware awaitable, including cancellation before first send."""

    def __init__(self, subscription: DesktopEventSubscriptionV2) -> None:
        self._subscription = subscription
        self._coroutine = subscription._next_frame()

    def __await__(self) -> Iterator[Any]:
        return self

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        return self.send(None)

    def send(self, value: Any) -> Any:
        return self._coroutine.send(value)

    def throw(
        self,
        typ: type[BaseException] | BaseException,
        val: object | None = None,
        tb: object | None = None,
    ) -> Any:
        try:
            if val is None and tb is None:
                return self._coroutine.throw(typ)
            if tb is None:
                return self._coroutine.throw(typ, val)
            return self._coroutine.throw(typ, val, tb)
        except BaseException:
            self._subscription._close()
            raise

    def close(self) -> None:
        try:
            self._coroutine.close()
        finally:
            self._subscription._close()


_CLOSED = object()
_GAP = object()
_EMPTY = object()


class DesktopEventBrokerV2:
    """Thread-safe bounded replay authority for the Desktop v2 SSE route."""

    def __init__(
        self,
        *,
        max_events: int = DEFAULT_MAX_EVENTS,
        max_ledger_bytes: int = DEFAULT_MAX_LEDGER_BYTES,
        max_subscriber_events: int | None = None,
        max_subscribers: int = DEFAULT_MAX_SUBSCRIBERS,
        max_queued_events: int = DEFAULT_MAX_QUEUED_EVENTS,
        max_queued_bytes: int = DEFAULT_MAX_QUEUED_BYTES,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        event_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(max_events) is not int or not 2 <= max_events <= 100_000:
            raise ValueError("event replay capacity is outside the supported bound")
        if type(max_ledger_bytes) is not int or not 1 <= max_ledger_bytes <= MAX_LEDGER_BYTES:
            raise ValueError("ledger byte capacity is outside the supported bound")
        if max_subscriber_events is None:
            max_subscriber_events = min(DEFAULT_MAX_SUBSCRIBER_EVENTS, max_events)
        if type(max_subscriber_events) is not int or not 1 <= max_subscriber_events <= max_events:
            raise ValueError("subscriber capacity is outside the replay bound")
        if type(max_subscribers) is not int or not 1 <= max_subscribers <= MAX_SUBSCRIBERS:
            raise ValueError("subscriber count is outside the process-wide bound")
        if type(max_queued_events) is not int or not 1 <= max_queued_events <= MAX_QUEUED_EVENTS:
            raise ValueError("queued event capacity is outside the process-wide bound")
        if type(max_queued_bytes) is not int or not 1 <= max_queued_bytes <= MAX_QUEUED_BYTES:
            raise ValueError("queued byte capacity is outside the process-wide bound")
        if (
            isinstance(heartbeat_interval, bool)
            or not isinstance(heartbeat_interval, (int, float))
            or not 0 < heartbeat_interval <= 60
        ):
            raise ValueError("heartbeat interval must be positive and at most 60 seconds")
        if (
            isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
            or not 0 < poll_interval <= 1
        ):
            raise ValueError("poll interval must be positive and at most one second")
        self._max_events = max_events
        self._max_ledger_bytes = max_ledger_bytes
        self._max_subscriber_events = max_subscriber_events
        self._max_subscribers = max_subscribers
        self._max_queued_events = max_queued_events
        self._max_queued_bytes = max_queued_bytes
        self._heartbeat_interval = float(heartbeat_interval)
        self._poll_interval = float(poll_interval)
        self._event_id_factory = event_id_factory or (lambda: secrets.token_urlsafe(24))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._publish_state = threading.local()
        self._events: deque[_PublishedFrame] = deque()
        self._subscribers: dict[str, _Subscriber] = {}
        self._ledger_bytes = 0
        self._queued_events = 0
        self._queued_bytes = 0
        self._next_sequence = 1
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

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, payload: m.DesktopEventPayloadV2) -> m.DesktopEventEnvelopeV2:
        if getattr(self._publish_state, "active", False):
            raise DesktopEventReentrantPublishError(
                "Desktop v2 event publication is not reentrant"
            )
        if type(payload) not in _PAYLOAD_TYPES:
            raise TypeError("Desktop event payload must be an exact frozen v2 model")
        self._publish_state.active = True
        try:
            try:
                typed_payload = _PAYLOAD_ADAPTER.validate_python(
                    payload.model_dump(mode="python", round_trip=True),
                    strict=True,
                )
            except ValidationError as exc:
                raise TypeError("Desktop event payload must be an exact frozen v2 model") from exc
            with self._lock:
                self._require_open()
                if self._next_sequence > MAX_EVENT_SEQUENCE:
                    raise DesktopEventGapError("Desktop v2 event sequence is exhausted")
            occurred_at = _timestamp(self._clock())
            raw_id = self._event_id_factory()
            if type(raw_id) is not str:
                raise ValueError("event identity factory must return a string")
            payload_bytes = _canonical_json_bytes(typed_payload.model_dump(mode="json"))
            with self._lock:
                self._require_open()
                if self._next_sequence > MAX_EVENT_SEQUENCE:
                    raise DesktopEventGapError("Desktop v2 event sequence is exhausted")
                sequence = self._next_sequence
                try:
                    event = m.DesktopEventEnvelopeV2(
                        event_id=f"{sequence:x}.{raw_id}",
                        sequence=sequence,
                        occurred_at=occurred_at,
                        event_type=typed_payload.payload_kind,
                        payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
                        payload=typed_payload,
                    )
                except ValidationError as exc:
                    raise ValueError("event identity factory returned an invalid token") from exc
                published = _PublishedFrame(
                    event_id=event.event_id,
                    frame=_sse_frame(event),
                )
                if len(published.frame) > self._max_ledger_bytes:
                    raise DesktopEventCapacityError(
                        "Desktop v2 event frame exceeds the ledger byte capacity"
                    )
                while self._events and (
                    len(self._events) >= self._max_events
                    or self._ledger_bytes + len(published.frame) > self._max_ledger_bytes
                ):
                    evicted = self._events.popleft()
                    self._ledger_bytes -= len(evicted.frame)
                self._next_sequence += 1
                self._events.append(published)
                self._ledger_bytes += len(published.frame)
                for subscriber in self._subscribers.values():
                    if subscriber.closed or subscriber.overflowed:
                        continue
                    if len(subscriber.pending) >= self._max_subscriber_events:
                        self._overflow_subscriber(subscriber)
                        continue
                    if (
                        self._queued_events >= self._max_queued_events
                        or self._queued_bytes + len(published.frame) > self._max_queued_bytes
                    ):
                        self._overflow_subscriber(subscriber)
                        continue
                    subscriber.pending.append(published)
                    subscriber.pending_bytes += len(published.frame)
                    self._queued_events += 1
                    self._queued_bytes += len(published.frame)
                return event
        finally:
            self._publish_state.active = False

    def subscribe(self, last_event_id: str | None = None) -> DesktopEventSubscriptionV2:
        with self._lock:
            self._require_open()
            if len(self._subscribers) >= self._max_subscribers:
                raise DesktopEventSubscriberLimitError(
                    "Desktop v2 event subscriber capacity is exhausted"
                )
            replay: tuple[_PublishedFrame, ...] = ()
            if last_event_id is not None:
                if type(last_event_id) is not str:
                    raise DesktopEventCursorExpiredError("Desktop v2 event cursor is invalid")
                event_ids = tuple(event.event_id for event in self._events)
                try:
                    position = event_ids.index(last_event_id)
                except ValueError as exc:
                    raise DesktopEventCursorExpiredError(
                        "Desktop v2 event cursor is outside the replay window"
                    ) from exc
                replay = tuple(self._events)[position + 1 :]
                if len(replay) > self._max_subscriber_events:
                    raise DesktopEventCursorExpiredError(
                        "Desktop v2 event cursor exceeds the subscriber replay bound"
                    )
            replay_bytes = sum(len(event.frame) for event in replay)
            if (
                self._queued_events + len(replay) > self._max_queued_events
                or self._queued_bytes + replay_bytes > self._max_queued_bytes
            ):
                raise DesktopEventCapacityError(
                    "Desktop v2 event replay exceeds the process-wide queue capacity"
                )
            token = secrets.token_urlsafe(24)
            while token in self._subscribers:
                token = secrets.token_urlsafe(24)
            subscriber = _Subscriber(
                token=token,
                pending=deque(replay),
                pending_bytes=replay_bytes,
            )
            self._subscribers[token] = subscriber
            self._queued_events += len(replay)
            self._queued_bytes += replay_bytes
            return DesktopEventSubscriptionV2(self, subscriber)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for subscriber in self._subscribers.values():
                subscriber.closed = True
                self._clear_pending(subscriber)
            self._subscribers.clear()
            self._events.clear()
            self._ledger_bytes = 0

    def _next_for(self, subscriber: _Subscriber) -> object:
        with self._lock:
            current = self._subscribers.get(subscriber.token)
            if self._closed or current is not subscriber or subscriber.closed:
                return _CLOSED
            if subscriber.overflowed:
                return _GAP
            if subscriber.pending:
                published = subscriber.pending.popleft()
                subscriber.pending_bytes -= len(published.frame)
                self._queued_events -= 1
                self._queued_bytes -= len(published.frame)
                return published
            return _EMPTY

    def _unsubscribe(self, subscriber: _Subscriber) -> None:
        with self._lock:
            current = self._subscribers.get(subscriber.token)
            if current is subscriber:
                del self._subscribers[subscriber.token]
            subscriber.closed = True
            self._clear_pending(subscriber)

    def _overflow_subscriber(self, subscriber: _Subscriber) -> None:
        self._clear_pending(subscriber)
        subscriber.overflowed = True

    def _clear_pending(self, subscriber: _Subscriber) -> None:
        self._queued_events -= len(subscriber.pending)
        self._queued_bytes -= subscriber.pending_bytes
        subscriber.pending.clear()
        subscriber.pending_bytes = 0

    def _require_open(self) -> None:
        if self._closed:
            raise DesktopEventBrokerClosedError("Desktop v2 event broker is closed")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _timestamp(now: datetime) -> str:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("event clock must return a timezone-aware datetime")
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sse_frame(event: m.DesktopEventEnvelopeV2) -> bytes:
    payload = _canonical_json_bytes(event.model_dump(mode="json"))
    frame = (
        b"id: "
        + event.event_id.encode("ascii")
        + b"\nevent: "
        + event.event_type.encode("ascii")
        + b"\ndata: "
        + payload
        + b"\n\n"
    )
    if len(frame) > MAX_EVENT_FRAME_BYTES:
        raise DesktopEventGapError("Desktop event frame exceeds the v2 byte bound")
    return frame


__all__ = (
    "DesktopEventBrokerClosedError",
    "DesktopEventBrokerError",
    "DesktopEventBrokerV2",
    "DesktopEventCapacityError",
    "DesktopEventCursorExpiredError",
    "DesktopEventGapError",
    "DesktopEventReentrantPublishError",
    "DesktopEventSubscriberLimitError",
    "DesktopEventSubscriptionV2",
)
