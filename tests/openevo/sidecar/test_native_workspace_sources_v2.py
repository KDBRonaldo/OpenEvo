from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from desktop.sidecar.native_workspace_sources_v2 import (
    NativeWorkspaceSourceRecordV2,
    NativeWorkspaceSourceStoreV2,
    NativeWorkspaceSourceStoreV2Error,
)


def _record(selected: Path) -> NativeWorkspaceSourceRecordV2:
    metadata = selected.stat()
    return NativeWorkspaceSourceRecordV2(
        schema_version="2",
        action_id="native-source-store-action-0001",
        import_id="workspace-import-" + ("a" * 48),
        selected_path=str(selected),
        selected_device=metadata.st_dev,
        selected_inode=metadata.st_ino,
        project_id="desktop-project-native-source-1",
        display_name=selected.name,
        journal_sha256="b" * 64,
    )


def test_private_native_source_roundtrips_exactly_across_restart(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    root = tmp_path / "native-sources"
    record = _record(selected)

    store = NativeWorkspaceSourceStoreV2(root)
    assert store.put(record)
    assert not store.put(record)
    assert store.list_records() == (record,)
    store.close()

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    record_path = root / f"{record.import_id}.json"
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    reopened = NativeWorkspaceSourceStoreV2(root)
    assert reopened.list_records() == (record,)
    reopened.remove(record)
    assert reopened.list_records() == ()
    reopened.close()


def test_native_source_authentication_rejects_same_length_path_tampering(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected-a"
    selected.mkdir()
    replacement = tmp_path / "selected-b"
    replacement.mkdir()
    root = tmp_path / "native-sources"
    record = _record(selected)
    store = NativeWorkspaceSourceStoreV2(root)
    store.put(record)
    store.close()
    record_path = root / f"{record.import_id}.json"
    document = json.loads(record_path.read_text(encoding="utf-8"))
    document["selected_path"] = str(replacement)
    payload = (
        json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    )
    assert len(payload.encode("utf-8")) == record_path.stat().st_size
    record_path.write_text(payload, encoding="utf-8")
    os.chmod(record_path, 0o600)

    with pytest.raises(
        NativeWorkspaceSourceStoreV2Error,
        match="authentication failed",
    ):
        NativeWorkspaceSourceStoreV2(root)
