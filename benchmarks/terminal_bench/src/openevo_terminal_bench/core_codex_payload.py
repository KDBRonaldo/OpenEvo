"""Pinned host Codex payload used by the isolated Harbor benchmark adapter."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import stat


_MAX_CODEX_BINARY_BYTES = 512 * 1024 * 1024
_MAX_RG_BINARY_BYTES = 32 * 1024 * 1024
_REMOTE_CODEX_PATH = "/installed-agent/openevo-codex"
_REMOTE_RG_PATH = "/installed-agent/openevo-rg"


@dataclass(frozen=True)
class CodexPayload:
    codex_path: Path
    codex_sha256: str
    codex_bytes: int
    rg_path: Path | None
    rg_sha256: str | None
    rg_bytes: int | None

    def manifest(self) -> dict[str, object]:
        return {
            "codex_bytes": self.codex_bytes,
            "codex_path": str(self.codex_path),
            "codex_sha256": self.codex_sha256,
            "rg_bytes": self.rg_bytes,
            "rg_path": str(self.rg_path) if self.rg_path is not None else None,
            "rg_sha256": self.rg_sha256,
        }


def resolve_codex_payload(
    *,
    codex_binary_path: str | None = None,
    rg_binary_path: str | None = None,
) -> CodexPayload:
    codex_path = _resolve_codex_binary(codex_binary_path)
    codex_sha256, codex_bytes = _hash_executable(
        codex_path,
        label="Codex",
        maximum_bytes=_MAX_CODEX_BINARY_BYTES,
    )
    rg_path = _resolve_rg_binary(codex_path, rg_binary_path)
    if rg_path is None:
        rg_sha256 = None
        rg_bytes = None
    else:
        rg_sha256, rg_bytes = _hash_executable(
            rg_path,
            label="Codex rg",
            maximum_bytes=_MAX_RG_BINARY_BYTES,
        )
    return CodexPayload(
        codex_path=codex_path,
        codex_sha256=codex_sha256,
        codex_bytes=codex_bytes,
        rg_path=rg_path,
        rg_sha256=rg_sha256,
        rg_bytes=rg_bytes,
    )


def build_codex_install_command(payload: CodexPayload, *, version: str) -> str:
    expected_version = f"codex-cli {version}"
    commands = [
        "set -eu",
        f"install -m 0755 {_REMOTE_CODEX_PATH} /usr/local/bin/codex",
        (
            f"printf '%s  %s\\n' {shlex.quote(payload.codex_sha256)} "
            "/usr/local/bin/codex | sha256sum -c -"
        ),
    ]
    if payload.rg_path is not None:
        assert payload.rg_sha256 is not None
        commands.extend(
            [
                f"install -m 0755 {_REMOTE_RG_PATH} /usr/local/bin/rg",
                (
                    f"printf '%s  %s\\n' {shlex.quote(payload.rg_sha256)} "
                    "/usr/local/bin/rg | sha256sum -c -"
                ),
            ]
        )
    commands.append(
        f"test \"$(/usr/local/bin/codex --version)\" = {shlex.quote(expected_version)}"
    )
    return "; ".join(commands)


def remote_codex_payload_paths() -> tuple[str, str]:
    return _REMOTE_CODEX_PATH, _REMOTE_RG_PATH


def _resolve_codex_binary(explicit_path: str | None) -> Path:
    if explicit_path:
        return Path(explicit_path).expanduser().resolve(strict=True)
    launcher = shutil.which("codex")
    if launcher is None:
        raise FileNotFoundError("Codex is not installed on the benchmark host")
    resolved_launcher = Path(launcher).resolve(strict=True)
    if resolved_launcher.name != "codex.js":
        return resolved_launcher
    package_root = resolved_launcher.parent.parent
    candidates = sorted(
        package_root.glob(
            "node_modules/@openai/codex-linux-*/vendor/*/bin/codex"
        )
    )
    if len(candidates) != 1:
        raise FileNotFoundError(
            "expected exactly one native Linux Codex payload next to the launcher"
        )
    return candidates[0].resolve(strict=True)


def _resolve_rg_binary(codex_path: Path, explicit_path: str | None) -> Path | None:
    if explicit_path:
        return Path(explicit_path).expanduser().resolve(strict=True)
    bundled = codex_path.parent.parent / "codex-path" / "rg"
    return bundled.resolve(strict=True) if bundled.is_file() else None


def _hash_executable(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> tuple[str, int]:
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} payload must be a regular file")
    if before.st_nlink < 1 or not 0 < before.st_size <= maximum_bytes:
        raise ValueError(f"{label} payload has an invalid size or link count")
    if before.st_mode & 0o111 == 0:
        raise ValueError(f"{label} payload must be executable")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
    if identity(before) != identity(after):
        raise RuntimeError(f"{label} payload changed while it was being hashed")
    return digest.hexdigest(), before.st_size


__all__ = [
    "CodexPayload",
    "build_codex_install_command",
    "remote_codex_payload_paths",
    "resolve_codex_payload",
]
