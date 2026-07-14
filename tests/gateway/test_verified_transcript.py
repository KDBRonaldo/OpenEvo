from __future__ import annotations

import os
from pathlib import Path

import pytest

from openevo.gateway import session_files
from openevo.gateway.session_files import (
    SessionFileSecurityError,
    capture_session_root_identity,
    read_verified_session_transcript,
)


_VALID_TRANSCRIPT = b'{"type":"agent_message","text":"verified bytes"}\n'


def _session_with_transcript(tmp_path: Path) -> tuple[Path, tuple[int, int, int], Path]:
    session_dir = tmp_path / "session"
    transcript = session_dir / "logs" / "agent" / "step.00.stdout.log"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(_VALID_TRANSCRIPT)
    return session_dir, capture_session_root_identity(session_dir), transcript


def test_verified_transcript_returns_exact_bytes_from_fixed_session_components(
    tmp_path: Path,
) -> None:
    session_dir, identity, transcript = _session_with_transcript(tmp_path)

    verified = read_verified_session_transcript(session_dir, identity, step_index=0)

    assert verified.content == _VALID_TRANSCRIPT
    assert verified.path == transcript


def test_verified_transcript_rejects_leaf_symlink_to_outside_content(tmp_path: Path) -> None:
    session_dir, identity, transcript = _session_with_transcript(tmp_path)
    outside = tmp_path / "outside.log"
    outside.write_bytes(b'{"type":"agent_message","text":"outside"}\n')
    transcript.unlink()
    transcript.symlink_to(outside)

    with pytest.raises(SessionFileSecurityError):
        read_verified_session_transcript(session_dir, identity, step_index=0)


def test_verified_transcript_rejects_ancestor_symlink_to_outside_content(
    tmp_path: Path,
) -> None:
    session_dir, identity, transcript = _session_with_transcript(tmp_path)
    outside_agent = tmp_path / "outside-agent"
    outside_agent.mkdir()
    (outside_agent / transcript.name).write_bytes(
        b'{"type":"agent_message","text":"outside ancestor"}\n'
    )
    transcript.unlink()
    transcript.parent.rmdir()
    transcript.parent.symlink_to(outside_agent, target_is_directory=True)

    with pytest.raises(SessionFileSecurityError):
        read_verified_session_transcript(session_dir, identity, step_index=0)


def test_verified_transcript_rejects_hard_linked_leaf(tmp_path: Path) -> None:
    session_dir, identity, transcript = _session_with_transcript(tmp_path)
    os.link(transcript, tmp_path / "second-link.log")

    with pytest.raises(SessionFileSecurityError, match="single-link"):
        read_verified_session_transcript(session_dir, identity, step_index=0)


def test_verified_transcript_rejects_leaf_over_byte_limit(tmp_path: Path) -> None:
    session_dir, identity, _ = _session_with_transcript(tmp_path)

    with pytest.raises(SessionFileSecurityError, match="bounded"):
        read_verified_session_transcript(
            session_dir,
            identity,
            step_index=0,
            max_bytes=len(_VALID_TRANSCRIPT) - 1,
        )


def test_verified_transcript_rejects_leaf_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir, identity, transcript = _session_with_transcript(tmp_path)
    original_read = os.read
    replaced = False

    def replacing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced and os.fstat(descriptor).st_ino == transcript.stat().st_ino:
            replaced = True
            old = transcript.with_suffix(".old")
            transcript.rename(old)
            transcript.write_bytes(b'{"type":"agent_message","text":"replacement"}\n')
        return original_read(descriptor, size)

    monkeypatch.setattr(session_files.os, "read", replacing_read)

    with pytest.raises(SessionFileSecurityError, match="changed"):
        read_verified_session_transcript(session_dir, identity, step_index=0)


def test_verified_transcript_rechecks_absolute_ancestor_chain_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    absolute_parent = tmp_path / "absolute-parent"
    session_dir, identity, transcript = _session_with_transcript(absolute_parent)
    original_read = os.read
    replaced = False

    def replacing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced and os.fstat(descriptor).st_ino == transcript.stat().st_ino:
            replaced = True
            moved = tmp_path / "moved-parent"
            absolute_parent.rename(moved)
            replacement = absolute_parent / "session" / "logs" / "agent"
            replacement.mkdir(parents=True)
            (replacement / transcript.name).write_bytes(
                b'{"type":"agent_message","text":"outside replacement"}\n'
            )
        return original_read(descriptor, size)

    monkeypatch.setattr(session_files.os, "read", replacing_read)

    with pytest.raises(SessionFileSecurityError, match="ancestor"):
        read_verified_session_transcript(session_dir, identity, step_index=0)


def test_verified_transcript_rejects_relative_ancestor_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir, identity, transcript = _session_with_transcript(tmp_path)
    transcript_inode = transcript.stat().st_ino
    original_read = os.read
    replaced = False

    def replacing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced and os.fstat(descriptor).st_ino == transcript_inode:
            replaced = True
            moved = session_dir / "logs" / "agent-old"
            transcript.parent.rename(moved)
            transcript.parent.mkdir()
            transcript.write_bytes(b'{"type":"agent_message","text":"outside replacement"}\n')
        return original_read(descriptor, size)

    monkeypatch.setattr(session_files.os, "read", replacing_read)

    with pytest.raises(SessionFileSecurityError, match="ancestor"):
        read_verified_session_transcript(session_dir, identity, step_index=0)
