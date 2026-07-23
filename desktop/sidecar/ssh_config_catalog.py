"""Bounded lexical discovery of literal aliases in the user's OpenSSH config.

The catalog is a convenience hint only.  It deliberately does not ask OpenSSH
to resolve a host and never evaluates configuration commands.  The eventual
connection passes the selected literal alias to the system ``ssh`` executable,
which remains the configuration authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Literal

from desktop.sidecar.contracts.v2.models import (
    SshCatalogWarningV2,
    SshHostHintV2,
)


_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GLOB_CHARACTERS = frozenset("*?[")
_DYNAMIC_INCLUDE_CHARACTERS = frozenset("$%~")
_WARNING_ORDER = (
    "dynamic_hosts_not_enumerated",
    "conditional_hosts_not_enumerated",
    "include_cycle_skipped",
    "include_unreadable",
    "catalog_budget_exhausted",
    "invalid_config_text_skipped",
)
_WARNING_ACTIONS = {
    "dynamic_hosts_not_enumerated": "manual_alias_available",
    "conditional_hosts_not_enumerated": "manual_alias_available",
    "include_cycle_skipped": "rescan",
    "include_unreadable": "rescan",
    "catalog_budget_exhausted": "rescan",
    "invalid_config_text_skipped": "administrator_action",
}

WarningCode = Literal[
    "dynamic_hosts_not_enumerated",
    "conditional_hosts_not_enumerated",
    "include_cycle_skipped",
    "include_unreadable",
    "catalog_budget_exhausted",
    "invalid_config_text_skipped",
]


class SshManualAliasError(ValueError):
    """A manually entered value is not a bounded literal SSH alias."""


def validate_manual_ssh_alias(alias: str) -> str:
    """Return an exact safe alias or reject option-like/dynamic input.

    There is intentionally no normalization.  The exact accepted value is the
    value later passed as one argv element to ``/usr/bin/ssh``.
    """

    if type(alias) is not str or _ALIAS_PATTERN.fullmatch(alias) is None:
        raise SshManualAliasError("SSH alias must be a bounded literal name")
    return alias


@dataclass(frozen=True)
class OpenSshCatalogBudgets:
    """Immutable upper bounds for one complete catalog scan."""

    max_files: int = 64
    max_total_bytes: int = 1 << 20
    max_file_bytes: int = 256 << 10
    max_include_depth: int = 16
    max_glob_matches: int = 256
    max_line_bytes: int = 4_096
    max_aliases: int = 512
    max_include_patterns: int = 256

    def __post_init__(self) -> None:
        values = (
            self.max_files,
            self.max_total_bytes,
            self.max_file_bytes,
            self.max_include_depth,
            self.max_glob_matches,
            self.max_line_bytes,
            self.max_aliases,
            self.max_include_patterns,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("OpenSSH catalog budgets must be positive integers")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("per-file catalog budget cannot exceed aggregate bytes")


@dataclass(frozen=True)
class OpenSshCatalogScan:
    """Path-free, renderer-safe semantic result of one lexical scan."""

    hosts: tuple[SshHostHintV2, ...]
    warnings: tuple[SshCatalogWarningV2, ...]

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "hosts": [host.model_dump(mode="json") for host in self.hosts],
            "warnings": [warning.model_dump(mode="json") for warning in self.warnings],
        }

    @property
    def semantic_sha256(self) -> str:
        encoded = json.dumps(
            self.to_safe_dict(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


@dataclass
class _ScanState:
    budgets: OpenSshCatalogBudgets
    aliases: dict[str, Literal["literal_host", "static_include"]] = field(
        default_factory=dict
    )
    warning_counts: dict[WarningCode, int] = field(default_factory=dict)
    files_consumed: int = 0
    bytes_consumed: int = 0
    include_patterns_consumed: int = 0
    glob_matches_consumed: int = 0
    active_files: set[tuple[int, int]] = field(default_factory=set)
    visited_files: set[tuple[int, int]] = field(default_factory=set)

    def warn(self, code: WarningCode, count: int = 1) -> None:
        current = self.warning_counts.get(code, 0)
        self.warning_counts[code] = min(10_000, current + max(1, count))

    def add_alias(
        self,
        alias: str,
        source_kind: Literal["literal_host", "static_include"],
    ) -> None:
        existing = self.aliases.get(alias)
        if existing is not None:
            if existing == "static_include" and source_kind == "literal_host":
                self.aliases[alias] = source_kind
            return
        if len(self.aliases) >= self.budgets.max_aliases:
            self.warn("catalog_budget_exhausted")
            return
        self.aliases[alias] = source_kind


class OpenSshHostCatalogLoader:
    """Read only static OpenSSH config syntax under aggregate budgets."""

    def __init__(
        self,
        *,
        config_path: Path | str,
        user_ssh_dir: Path | str,
        budgets: OpenSshCatalogBudgets | None = None,
    ) -> None:
        root = Path(config_path)
        ssh_dir = Path(user_ssh_dir)
        if not root.is_absolute() or not ssh_dir.is_absolute():
            raise ValueError("OpenSSH catalog paths must be absolute")
        self._config_path = root
        self._user_ssh_dir = ssh_dir
        self._budgets = budgets or OpenSshCatalogBudgets()

    def scan(self) -> OpenSshCatalogScan:
        state = _ScanState(self._budgets)
        self._parse_file(
            self._config_path,
            state=state,
            depth=0,
            source_kind="literal_host",
            missing_root_is_empty=True,
        )
        hosts = tuple(
            SshHostHintV2(
                ssh_host_alias=alias,
                availability="selectable",
                source_kind=source,
            )
            for alias, source in sorted(state.aliases.items())
        )
        warnings = tuple(
            SshCatalogWarningV2(
                code=code,
                action=_WARNING_ACTIONS[code],  # type: ignore[arg-type]
                affected_entry_count=state.warning_counts[code],
            )
            for code in _WARNING_ORDER
            if code in state.warning_counts
        )
        return OpenSshCatalogScan(hosts=hosts, warnings=warnings)

    def _parse_file(
        self,
        path: Path,
        *,
        state: _ScanState,
        depth: int,
        source_kind: Literal["literal_host", "static_include"],
        missing_root_is_empty: bool = False,
    ) -> None:
        if depth > state.budgets.max_include_depth:
            state.warn("catalog_budget_exhausted")
            return
        if state.files_consumed >= state.budgets.max_files:
            state.warn("catalog_budget_exhausted")
            return
        state.files_consumed += 1

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if not (missing_root_is_empty and exc.errno == errno.ENOENT):
                state.warn("include_unreadable")
            return
        try:
            before = os.fstat(fd)
            identity = (before.st_dev, before.st_ino)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                state.warn("include_unreadable")
                return
            if identity in state.active_files:
                state.warn("include_cycle_skipped")
                return
            if identity in state.visited_files:
                return
            if before.st_size > state.budgets.max_file_bytes:
                state.bytes_consumed = min(
                    state.budgets.max_total_bytes,
                    state.bytes_consumed + state.budgets.max_file_bytes,
                )
                state.warn("catalog_budget_exhausted")
                return
            if before.st_size > state.budgets.max_total_bytes - state.bytes_consumed:
                state.bytes_consumed = state.budgets.max_total_bytes
                state.warn("catalog_budget_exhausted")
                return
            state.bytes_consumed += before.st_size
            state.active_files.add(identity)
            try:
                payload = _read_exact_bounded(fd, before.st_size)
                after = os.fstat(fd)
                if (
                    (after.st_dev, after.st_ino) != identity
                    or after.st_size != before.st_size
                    or after.st_mode != before.st_mode
                ):
                    state.warn("include_unreadable")
                    return
                state.visited_files.add(identity)
                self._parse_payload(
                    payload,
                    state=state,
                    depth=depth,
                    source_kind=source_kind,
                )
            finally:
                state.active_files.discard(identity)
        except OSError:
            state.warn("include_unreadable")
        finally:
            os.close(fd)

    def _parse_payload(
        self,
        payload: bytes,
        *,
        state: _ScanState,
        depth: int,
        source_kind: Literal["literal_host", "static_include"],
    ) -> None:
        scope: Literal["global", "host", "match"] = "global"
        for raw_line in payload.splitlines():
            if len(raw_line) > state.budgets.max_line_bytes:
                state.warn("catalog_budget_exhausted")
                continue
            try:
                line = raw_line.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                state.warn("invalid_config_text_skipped")
                continue
            try:
                tokens = _tokenize_config_line(line)
            except ValueError:
                state.warn("invalid_config_text_skipped")
                continue
            if not tokens:
                continue
            keyword, arguments = _split_directive(tokens)
            if keyword == "host":
                scope = "host"
                if not arguments:
                    state.warn("invalid_config_text_skipped")
                    continue
                for alias in arguments:
                    if alias.startswith("!") or any(
                        character in alias for character in _GLOB_CHARACTERS
                    ):
                        state.warn("dynamic_hosts_not_enumerated")
                    elif _ALIAS_PATTERN.fullmatch(alias) is None:
                        state.warn("invalid_config_text_skipped")
                    else:
                        state.add_alias(alias, source_kind)
                continue
            if keyword == "match":
                scope = "match"
                state.warn("conditional_hosts_not_enumerated")
                continue
            if keyword != "include":
                continue
            if not arguments:
                state.warn("invalid_config_text_skipped")
                continue
            if scope != "global":
                state.warn("conditional_hosts_not_enumerated", len(arguments))
                continue
            for include_token in arguments:
                self._parse_include_token(
                    include_token,
                    state=state,
                    depth=depth + 1,
                )

    def _parse_include_token(
        self,
        token: str,
        *,
        state: _ScanState,
        depth: int,
    ) -> None:
        if state.include_patterns_consumed >= state.budgets.max_include_patterns:
            state.warn("catalog_budget_exhausted")
            return
        state.include_patterns_consumed += 1
        if (
            not token
            or "\x00" in token
            or any(character in token for character in _DYNAMIC_INCLUDE_CHARACTERS)
        ):
            state.warn("dynamic_hosts_not_enumerated")
            return
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = self._user_ssh_dir / candidate
        normalized = Path(os.path.abspath(candidate))
        has_glob = glob.has_magic(os.fspath(normalized))
        if not has_glob:
            self._parse_file(
                normalized,
                state=state,
                depth=depth,
                source_kind="static_include",
            )
            return

        matches: list[Path] = []
        iterator = glob.iglob(os.fspath(normalized), recursive=False)
        for raw_match in iterator:
            if state.glob_matches_consumed >= state.budgets.max_glob_matches:
                state.warn("catalog_budget_exhausted")
                break
            state.glob_matches_consumed += 1
            matches.append(Path(raw_match))
        for match in sorted(set(matches), key=os.fspath):
            self._parse_file(
                match,
                state=state,
                depth=depth,
                source_kind="static_include",
            )


def _read_exact_bounded(fd: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size + 1
    while remaining:
        chunk = os.read(fd, min(remaining, 64 << 10))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) != expected_size:
        raise OSError(errno.EIO, "OpenSSH config changed while reading")
    return payload


def _tokenize_config_line(line: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            else:
                current.append(character)
            continue
        if character in {'"', "'"}:
            quote = character
            continue
        if character == "#" and not current:
            break
        if character.isspace():
            if current:
                tokens.append("".join(current))
                current.clear()
            continue
        current.append(character)
    if escaped or quote is not None:
        raise ValueError("unterminated OpenSSH configuration token")
    if current:
        tokens.append("".join(current))
    return tokens


def _split_directive(tokens: list[str]) -> tuple[str, list[str]]:
    first = tokens[0]
    if "=" in first:
        keyword, initial = first.split("=", 1)
        arguments = ([initial] if initial else []) + tokens[1:]
        return keyword.casefold(), arguments
    if len(tokens) >= 2 and tokens[1] == "=":
        return first.casefold(), tokens[2:]
    return first.casefold(), tokens[1:]


__all__ = (
    "OpenSshCatalogBudgets",
    "OpenSshCatalogScan",
    "OpenSshHostCatalogLoader",
    "SshManualAliasError",
    "validate_manual_ssh_alias",
)
