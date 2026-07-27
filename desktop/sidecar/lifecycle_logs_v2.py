"""Bounded process-output sanitization for Desktop lifecycle operations."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import codecs
import re
import threading
from typing import Literal, TypeAlias

from desktop.sidecar.contracts.v2.models import MAX_LIFECYCLE_LOG_ENTRY_BYTES


LifecycleLogSourceV2: TypeAlias = Literal[
    "desktop",
    "ssh_stdout",
    "ssh_stderr",
    "daemon_stdout",
    "daemon_stderr",
]
LifecycleLogSinkV2: TypeAlias = Callable[[LifecycleLogSourceV2, str, bool], None]
LifecycleRawOutputObserverV2: TypeAlias = Callable[[LifecycleLogSourceV2, bytes], None]

_PROCESS_SOURCES = frozenset(
    {
        "ssh_stdout",
        "ssh_stderr",
        "daemon_stdout",
        "daemon_stderr",
    }
)
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ESCAPE_RE = re.compile(r"\x1b[@-_]")
_AUTHORIZATION_RE = re.compile(r"(?i)(authorization[ \t]*:[ \t]*)(?:bearer|basic)[ \t]+[^\s]+")
_BEARER_RE = re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{8,}")
_PROXY_USERINFO_RE = re.compile(r"(?i)\b(https?://)[^/@\s:]+:[^/@\s]+@")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"[a-z0-9_-]*(?:api[_-]?key|token|secret|password|passwd|private[_-]?key|"
    r"credential|capability)[a-z0-9_-]*"
    r")([ \t]*[:=][ \t]*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s&;,\r\n]+)"
)
_LOOPBACK_ENDPOINT_RE = re.compile(
    r"(?i)\bhttps?://(?:127(?:\.[0-9]{1,3}){3}|localhost|\[::1\])"
    r"(?::[0-9]{1,5})?(?:/[^\s]*)?"
)
_ABSOLUTE_HOST_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9._~:/-])"
    r"/(?!/)(?:[^\s\t\r\n'\"<>/]+)(?:/[^\s\t\r\n'\"<>/]+)*"
)
_HOME_RELATIVE_HOST_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9._~:/-])"
    r"(?:~[A-Za-z0-9._-]*|\$HOME|\$\{HOME\})/"
    r"(?:[^\s\t\r\n'\"<>/]+)(?:/[^\s\t\r\n'\"<>/]+)*"
)
_MAX_UNTERMINATED_LINE_BYTES = MAX_LIFECYCLE_LOG_ENTRY_BYTES
_UNTERMINATED_LINE_OMITTED = "[TRUNCATED: unterminated process output omitted]\n"


class LifecycleOutputSanitizerV2:
    """Incrementally decode and sanitize child output before persistence.

    The callable accepts only a closed source and raw child bytes. It never
    accepts argv or an environment mapping, which keeps those authorities out
    of the logging path by construction.
    """

    def __init__(
        self,
        sink: LifecycleLogSinkV2,
        *,
        secret_canaries: Iterable[str] = (),
        forbidden_endpoints: Iterable[str] = (),
        forbidden_paths: Iterable[str] = (),
    ) -> None:
        if not callable(sink):
            raise TypeError("lifecycle log sink must be callable")
        self._sink = sink
        self._secret_canaries = self._validated_secret_canaries(secret_canaries)
        self._forbidden_endpoints = self._validated_literals(
            forbidden_endpoints,
            label="Core endpoint",
        )
        self._forbidden_paths = self._validated_literals(
            forbidden_paths,
            label="host path",
        )
        self._decoders = {
            source: codecs.getincrementaldecoder("utf-8")(errors="replace")
            for source in _PROCESS_SOURCES
        }
        self._pending = {source: "" for source in _PROCESS_SOURCES}
        self._discarding_unterminated = {source: False for source in _PROCESS_SOURCES}
        self._lock = threading.RLock()
        self._closed = False

    def __call__(self, source: LifecycleLogSourceV2, chunk: bytes) -> None:
        self.feed(source, chunk)

    def feed(self, source: LifecycleLogSourceV2, chunk: bytes) -> None:
        if source not in _PROCESS_SOURCES:
            raise ValueError("process output source is outside the closed v2 set")
        if type(chunk) is not bytes:
            raise TypeError("process output chunk must be bytes")
        if not chunk:
            return
        with self._lock:
            if self._closed:
                return
            decoded = self._decoders[source].decode(chunk, final=False)
            normalized = self._normalize_newlines(decoded)
            if self._discarding_unterminated[source]:
                boundary = normalized.find("\n")
                if boundary < 0:
                    return
                self._discarding_unterminated[source] = False
                normalized = normalized[boundary + 1 :]
            self._pending[source] += normalized
            self._drain_complete_lines(source)

    def flush(self, source: LifecycleLogSourceV2 | None = None) -> None:
        if source is not None and source not in _PROCESS_SOURCES:
            raise ValueError("process output source is outside the closed v2 set")
        with self._lock:
            if self._closed:
                return
            sources = tuple(_PROCESS_SOURCES) if source is None else (source,)
            for current in sources:
                final = self._decoders[current].decode(b"", final=True)
                if not self._discarding_unterminated[current]:
                    self._pending[current] += final
                pending, self._pending[current] = self._pending[current], ""
                if not self._discarding_unterminated[current]:
                    self._emit_lines(current, self._normalize_newlines(pending))
                self._discarding_unterminated[current] = False
                self._decoders[current] = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
        self.flush()
        with self._lock:
            self._closed = True

    def _drain_complete_lines(self, source: LifecycleLogSourceV2) -> None:
        normalized = self._normalize_newlines(self._pending[source])
        boundary = normalized.rfind("\n")
        if boundary < 0:
            if len(normalized.encode("utf-8")) > _MAX_UNTERMINATED_LINE_BYTES:
                self._pending[source] = ""
                self._discarding_unterminated[source] = True
                self._emit_bounded(
                    source,
                    _UNTERMINATED_LINE_OMITTED,
                    force_truncated=True,
                )
            else:
                self._pending[source] = normalized
            return
        complete = normalized[: boundary + 1]
        trailing = normalized[boundary + 1 :]
        if len(trailing.encode("utf-8")) > _MAX_UNTERMINATED_LINE_BYTES:
            self._pending[source] = ""
            self._discarding_unterminated[source] = True
        else:
            self._pending[source] = trailing
        self._emit_lines(source, complete)
        if self._discarding_unterminated[source]:
            self._emit_bounded(
                source,
                _UNTERMINATED_LINE_OMITTED,
                force_truncated=True,
            )

    def _emit_lines(self, source: LifecycleLogSourceV2, value: str) -> None:
        for line in value.splitlines(keepends=True):
            safe = self._sanitize(line)
            if safe:
                self._emit_bounded(source, safe)

    def _sanitize(self, value: str) -> str:
        safe = self._strip_terminal_controls(value)
        for canary in self._secret_canaries:
            safe = safe.replace(canary, "[REDACTED_SECRET]")
        safe = _AUTHORIZATION_RE.sub(r"\1[REDACTED_CREDENTIAL]", safe)
        safe = _BEARER_RE.sub("[REDACTED_CREDENTIAL]", safe)
        safe = _PROXY_USERINFO_RE.sub(r"\1[REDACTED_CREDENTIAL]@", safe)
        safe = _SENSITIVE_ASSIGNMENT_RE.sub(
            r"\1\2[REDACTED_CREDENTIAL]",
            safe,
        )
        for endpoint in self._forbidden_endpoints:
            safe = re.sub(
                re.escape(endpoint) + r"(?:/[^\s]*)?",
                "[REDACTED_CORE_ENDPOINT]",
                safe,
            )
        safe = _LOOPBACK_ENDPOINT_RE.sub("[REDACTED_CORE_ENDPOINT]", safe)
        for path in self._forbidden_paths:
            safe = re.sub(
                re.escape(path) + r"(?:/[^\s\t\r\n'\"<>]+)*",
                "[REDACTED_HOST_PATH]",
                safe,
            )
        safe = _HOME_RELATIVE_HOST_PATH_RE.sub("[REDACTED_HOST_PATH]", safe)
        return _ABSOLUTE_HOST_PATH_RE.sub("[REDACTED_HOST_PATH]", safe)

    def _emit_bounded(
        self,
        source: LifecycleLogSourceV2,
        value: str,
        *,
        force_truncated: bool = False,
    ) -> None:
        original_bytes = len(value.encode("utf-8"))
        split = force_truncated or original_bytes > MAX_LIFECYCLE_LOG_ENTRY_BYTES
        remaining = value
        while remaining:
            raw = remaining.encode("utf-8")
            if len(raw) <= MAX_LIFECYCLE_LOG_ENTRY_BYTES:
                part = remaining
            else:
                prefix = raw[:MAX_LIFECYCLE_LOG_ENTRY_BYTES]
                try:
                    part = prefix.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    part = prefix[: exc.start].decode("utf-8", errors="strict")
            if not part:
                return
            try:
                self._sink(source, part, split)
            except Exception:
                pass
            remaining = remaining[len(part) :]

    @staticmethod
    def _normalize_newlines(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _strip_terminal_controls(value: str) -> str:
        safe = _OSC_RE.sub("", value)
        safe = _CSI_RE.sub("", safe)
        safe = _ESCAPE_RE.sub("", safe)
        return "".join(
            character
            for character in safe
            if character in {"\n", "\t"}
            or (ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F)
        )

    @classmethod
    def _validated_secret_canaries(cls, values: Iterable[str]) -> tuple[str, ...]:
        validated = cls._validated_literals(values, label="secret canary")
        expanded: set[str] = set()
        for value in validated:
            visible = cls._strip_terminal_controls(cls._normalize_newlines(value))
            if not visible:
                continue
            expanded.add(visible)
            expanded.update(part for part in visible.split("\n") if part)
        return tuple(sorted(expanded, key=lambda item: (-len(item), item)))

    @staticmethod
    def _validated_literals(values: Iterable[str], *, label: str) -> tuple[str, ...]:
        try:
            items = tuple(values)
        except TypeError as exc:
            raise TypeError(f"{label} collection is invalid") from exc
        if any(
            type(value) is not str
            or not value
            or len(value.encode("utf-8")) > 65_536
            or "\x00" in value
            for value in items
        ):
            raise ValueError(f"{label} is invalid")
        return tuple(sorted(set(items), key=lambda item: (-len(item), item)))


__all__ = [
    "LifecycleLogSinkV2",
    "LifecycleLogSourceV2",
    "LifecycleOutputSanitizerV2",
    "LifecycleRawOutputObserverV2",
]
