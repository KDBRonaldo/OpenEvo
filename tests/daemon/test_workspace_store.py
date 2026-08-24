from __future__ import annotations

from pathlib import Path

import pytest

from openevo.daemon.errors import RequestError, StateConflictError
from openevo.daemon.workspace_store import ProjectWorkspaceStore


def test_workspace_authority_persists_and_rejects_unsafe_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspaces"
    store = ProjectWorkspaceStore(root)

    entry = store.upload_file("project-1", "notes/result.txt", b"stable result\n", overwrite=False)
    authority = store.authoritative_snapshot_v2("project-1")

    assert entry["path"] == "notes/result.txt"
    assert authority["entries"][1]["content"] == "stable result\n"
    assert len(authority["manifest_sha256"]) == 64
    assert (
        ProjectWorkspaceStore(root).read_file("project-1", "notes/result.txt")[0]
        == b"stable result\n"
    )
    with pytest.raises(StateConflictError):
        store.upload_file("project-1", "notes/result.txt", b"replacement", overwrite=False)
    with pytest.raises(RequestError, match="unsafe"):
        store.read_file("project-1", "../outside.txt")
