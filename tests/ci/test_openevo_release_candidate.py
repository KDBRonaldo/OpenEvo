from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/openevo_release_candidate.py"
    spec = importlib.util.spec_from_file_location("openevo_release_candidate", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_runtime_asset_module():
    path = (
        Path(__file__).resolve().parents[2] / "scripts/ci/verify_managed_runtime_release_asset.py"
    )
    spec = importlib.util.spec_from_file_location(
        "verify_managed_runtime_release_asset",
        path,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _release_notes_text() -> str:
    candidate = _load_module()
    return candidate.render_candidate_release_notes(
        source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        version="0.1.0",
        architecture="aarch64",
    )


def test_candidate_release_notes_are_one_canonical_document() -> None:
    notes = _release_notes_text()

    assert notes.endswith("\n")
    assert "Self-Deployed Reference mode: unavailable in this candidate." in notes
    assert "openevo-science-runtime-0.1.0-linux-amd64.tar.gz" in notes
    assert "Managed Science runtime source asset ID: 478167627." in notes
    assert "Credential-canary verification for release assets: pending." in notes
    assert "Local Desktop data under ~/.openevo/desktop is retained" in notes
    assert "org.openevo.desktop" in notes
    assert "run-retry recovery" in notes


def test_candidate_workflow_closes_managed_subscription_runtime_release() -> None:
    candidate = _load_module()
    workflow = Path(".github/workflows/openevo-desktop-candidate.yml").read_text(encoding="utf-8")

    for value in (
        str(candidate.MANAGED_RUNTIME_ASSET_ID),
        str(candidate.MANAGED_RUNTIME_ARCHIVE_SIZE),
        candidate.MANAGED_RUNTIME_ARCHIVE_NAME,
        candidate.MANAGED_RUNTIME_ARCHIVE_SHA256,
        "verify_managed_runtime_release_asset.py",
        "--managed-runtime-archive",
        "--release-build",
        "docker:29.3-dind",
        "smoke_managed_runtime_archive.py",
    ):
        assert value in workflow
    assert workflow.count("self-deployed") == 0


def test_write_notes_command_creates_but_never_replaces_canonical_document(
    tmp_path: Path,
) -> None:
    candidate = _load_module()
    output = tmp_path / "release-notes.md"
    arguments = [
        "write-notes",
        str(output),
        "--source-commit",
        "8e45af371eef49a86530a849041f7dcf047620ec",
        "--version",
        "0.1.0",
        "--architecture",
        "aarch64",
    ]

    assert candidate.main(arguments) == 0
    assert output.read_text(encoding="utf-8") == _release_notes_text()
    assert candidate.main(arguments) == 1


def test_write_draft_body_binds_owner_and_never_replaces_output(tmp_path: Path) -> None:
    candidate = _load_module()
    notes = tmp_path / "release-notes.md"
    notes.write_text(_release_notes_text(), encoding="utf-8")
    output = tmp_path / "draft-release-body.md"
    arguments = [
        "write-draft-body",
        str(output),
        "--release-notes",
        str(notes),
        "--ownership-token",
        "d" * 32,
    ]

    assert candidate.main(arguments) == 0
    assert output.read_text(encoding="utf-8") == _owned_draft_body(candidate, notes)
    assert candidate.main(arguments) == 1


@pytest.mark.parametrize("ownership_token", ["", "D" * 32, "a" * 31, "g" * 32])
def test_draft_body_rejects_invalid_ownership_token(ownership_token: str) -> None:
    candidate = _load_module()

    with pytest.raises(candidate.CandidateError, match="ownership token"):
        candidate.render_draft_release_body(
            release_notes=_release_notes_text(),
            ownership_token=ownership_token,
        )


def test_release_inventory_proves_draft_aware_tag_absence(tmp_path: Path) -> None:
    candidate = _load_module()
    inventory = tmp_path / "release-tags.jsonl"
    inventory.write_text('"unrelated-draft"\n', encoding="utf-8")
    arguments = [
        "assert-release-absent",
        str(inventory),
        "--expected-tag",
        "openevo-desktop-v0.1.0-exhibition.123.2",
    ]

    assert candidate.main(arguments) == 0
    inventory.write_text(
        '"unrelated-draft"\n"openevo-desktop-v0.1.0-exhibition.123.2"\n',
        encoding="utf-8",
    )
    assert candidate.main(arguments) == 1


@pytest.mark.parametrize("payload", ["not-json\n", "null\n", '""\n'])
def test_release_inventory_rejects_untrusted_output(
    tmp_path: Path,
    payload: str,
) -> None:
    candidate = _load_module()
    inventory = tmp_path / "release-tags.jsonl"
    inventory.write_text(payload, encoding="utf-8")

    with pytest.raises(candidate.CandidateError, match="release inventory line"):
        candidate.assert_release_tag_absent(inventory, expected_tag="candidate-tag")


def test_candidate_manifest_rejects_extra_or_contradictory_release_claims(
    tmp_path: Path,
) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    notes = tmp_path / "release-notes.md"
    notes.write_text(
        notes.read_text(encoding="utf-8")
        + "Self-Deployed Reference mode: available and fully validated.\n",
        encoding="utf-8",
    )

    with pytest.raises(candidate.CandidateError, match="canonical packaging draft"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="a" * 64,
        )


def _draft_release_metadata(*, body: str) -> dict[str, object]:
    return {
        "apiUrl": (
            "https://api.github.com/repos/CompLifeLab-ZJU/OpenEvo/releases/354404740"
        ),
        "body": body,
        "isDraft": True,
        "isPrerelease": True,
        "name": "OpenEvo Desktop 0.1.0 unsigned candidate",
        "tagName": "openevo-desktop-v0.1.0-exhibition.123.2",
        "targetCommitish": "8e45af371eef49a86530a849041f7dcf047620ec",
        "url": (
            "https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/"
            "untagged-7a9ca728f876fa16a90d"
        ),
    }


def _owned_draft_body(candidate, notes: Path) -> str:
    return candidate.render_draft_release_body(
        release_notes=notes.read_text(encoding="utf-8"),
        ownership_token="d" * 32,
    )


def test_draft_release_metadata_binds_review_facing_fields(tmp_path: Path) -> None:
    candidate = _load_module()
    notes = tmp_path / "release-notes.md"
    notes.write_text(_release_notes_text(), encoding="utf-8")
    metadata = tmp_path / "draft-release.json"
    metadata.write_text(
        json.dumps(_draft_release_metadata(body=_owned_draft_body(candidate, notes))),
        encoding="utf-8",
    )
    release_id = tmp_path / "release-id"
    validation_arguments = [
        "validate-draft",
        str(metadata),
        "--release-notes",
        str(notes),
        "--expected-tag",
        "openevo-desktop-v0.1.0-exhibition.123.2",
        "--expected-target",
        "8e45af371eef49a86530a849041f7dcf047620ec",
        "--expected-title",
        "OpenEvo Desktop 0.1.0 unsigned candidate",
        "--expected-repository",
        "CompLifeLab-ZJU/OpenEvo",
        "--expected-owner",
        "d" * 32,
        "--release-id-output",
        str(release_id),
    ]

    assert candidate.validate_draft_release_metadata(
        metadata,
        release_notes=notes,
        expected_tag="openevo-desktop-v0.1.0-exhibition.123.2",
        expected_target="8e45af371eef49a86530a849041f7dcf047620ec",
        expected_title="OpenEvo Desktop 0.1.0 unsigned candidate",
        expected_repository="CompLifeLab-ZJU/OpenEvo",
        expected_owner="d" * 32,
    ) == []
    assert candidate.main(validation_arguments) == 0
    assert release_id.read_text(encoding="ascii") == "354404740\n"
    assert release_id.stat().st_mode & 0o777 == 0o600
    assert candidate.main(validation_arguments) == 1
    assert release_id.read_text(encoding="ascii") == "354404740\n"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("body", "edited body"),
        ("isDraft", False),
        ("isPrerelease", False),
        ("name", "edited title"),
        ("tagName", "edited-tag"),
        ("targetCommitish", "f" * 40),
        (
            "apiUrl",
            "https://api.github.com/repos/attacker/unrelated/releases/354404740",
        ),
        (
            "apiUrl",
            "https://api.github.com/repos/CompLifeLab-ZJU/OpenEvo/releases/not-an-id",
        ),
        ("url", "https://github.com/attacker/unrelated/releases/tag/forged"),
        (
            "url",
            "https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/untagged/x",
        ),
        (
            "url",
            "https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/untagged?view=1",
        ),
    ],
)
def test_draft_release_metadata_rejects_review_surface_mutation(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    candidate = _load_module()
    notes = tmp_path / "release-notes.md"
    notes.write_text(_release_notes_text(), encoding="utf-8")
    payload = _draft_release_metadata(body=_owned_draft_body(candidate, notes))
    payload[field] = replacement
    metadata = tmp_path / "draft-release.json"
    metadata.write_text(json.dumps(payload), encoding="utf-8")

    errors = candidate.validate_draft_release_metadata(
        metadata,
        release_notes=notes,
        expected_tag="openevo-desktop-v0.1.0-exhibition.123.2",
        expected_target="8e45af371eef49a86530a849041f7dcf047620ec",
        expected_title="OpenEvo Desktop 0.1.0 unsigned candidate",
        expected_repository="CompLifeLab-ZJU/OpenEvo",
        expected_owner="d" * 32,
    )

    assert errors


def test_candidate_manifest_binds_exact_release_inventory(tmp_path: Path) -> None:
    candidate = _load_module()
    paths = _write_candidate_inputs(tmp_path)

    manifest = candidate.create_candidate_manifest(
        tmp_path,
        source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        version="0.1.0",
        architecture="aarch64",
        rust_target="aarch64-apple-darwin",
        registry_digest="a" * 64,
    )

    assert candidate.validate_candidate_manifest(
        manifest,
        expected_source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
    ) == []
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["release"] == {
        "channel": "unsigned-draft-prerelease",
        "notarized": False,
        "signed": False,
    }
    assert payload["macos"] == {
        "architecture": "aarch64",
        "minimum_system_version": "12.0",
        "native_architectures": {
            "bundled_external_bin": ["arm64"],
            "native_executable": ["arm64"],
        },
        "rust_target": "aarch64-apple-darwin",
    }
    by_role = {entry["role"]: entry for entry in payload["files"]}
    assert by_role["desktop_dmg"]["filename"] == paths["dmg"].name
    assert by_role["core_wheel"]["sha256"] == hashlib.sha256(
        paths["wheel"].read_bytes()
    ).hexdigest()
    assert by_role["framework_lock"]["filename"] == "framework-lock.json"
    assert by_role["core_descriptor"]["filename"] == "core-install-artifact.json"
    assert by_role["checksums"]["filename"] == "SHA256SUMS"
    assert by_role["app_bundle_smoke"]["filename"] == "app-bundle-smoke.json"
    assert by_role["dmg_copy_smoke"]["filename"] == "dmg-copy-smoke.json"
    assert by_role["managed_runtime_source"]["filename"] == "managed-runtime-source.json"
    assert payload["core"]["registry_digest"] == "a" * 64
    assert payload["managed_runtime"] == candidate._managed_runtime_manifest()
    assert payload["managed_runtime"]["capability"] == {
        "capture_mode": "transcript",
        "execution_mode": "codex_subscription_transcript",
        "harness_id": "codex",
        "token_level_metrics_available": False,
    }
    assert "self-deployed" not in json.dumps(payload["managed_runtime"])
    descriptor = json.loads(
        (tmp_path / "core-install-artifact.json").read_text(encoding="utf-8")
    )
    assert descriptor["artifact"] == by_role["core_wheel"]
    assert descriptor["framework_lock"] == by_role["framework_lock"]
    assert descriptor["source_commit"] == payload["source_commit"]
    assert descriptor["schema_version"] == 2
    assert descriptor["compatibility"] == {
        "python_requires": ">=3.11",
        "supported_platforms": ["linux-x86_64"],
    }


def test_managed_runtime_source_binds_draft_asset_and_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _load_module()
    verifier = _load_runtime_asset_module()
    assert candidate._managed_runtime_source_evidence() == (verifier.expected_source_evidence())
    archive = tmp_path / verifier.MANAGED_RUNTIME_ARCHIVE_RELEASE.filename
    archive.write_bytes(b"managed subscription runtime")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    release = dataclasses.replace(
        verifier.MANAGED_RUNTIME_ARCHIVE_RELEASE,
        byte_size=archive.stat().st_size,
        sha256=digest,
        asset_api_digest=f"sha256:{digest}",
    )
    monkeypatch.setattr(verifier, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    monkeypatch.setattr(
        verifier,
        "verify_managed_runtime_archive",
        lambda *_args, **_kwargs: None,
    )
    asset = {
        "digest": f"sha256:{digest}",
        "id": release.asset_id,
        "name": archive.name,
        "size": archive.stat().st_size,
        "state": "uploaded",
    }
    release_json = tmp_path / "release.json"
    asset_json = tmp_path / "asset.json"
    evidence = tmp_path / "managed-runtime-source.json"
    _write_json(
        release_json,
        {
            "assets": [asset],
            "draft": True,
            "id": release.asset_release_id,
            "tag_name": release.asset_release_tag,
        },
    )
    _write_json(asset_json, asset)

    verifier.verify_release_asset(
        release_json,
        asset_json,
        archive,
        evidence,
    )

    assert json.loads(evidence.read_text(encoding="utf-8")) == (
        verifier.expected_source_evidence()
    )

    asset["id"] = release.asset_id + 1
    _write_json(asset_json, asset)
    with pytest.raises(verifier.AssetVerificationError, match="release/asset API identity"):
        verifier.verify_release_asset(
            release_json,
            asset_json,
            archive,
            tmp_path / "rejected.json",
        )


@pytest.mark.parametrize(
    "required_marker",
    [
        "## Supported Workflows",
        "Codex subscription transcript mode: available in this candidate.",
        "Self-Deployed Reference mode: unavailable in this candidate.",
        "## Known Limitations",
        "Parameter evolution is not included in this candidate.",
        "PyPI is not used for this release.",
        "Only the declared architecture was built.",
        "Browser-download quarantine and the Privacy & Security allow flow are not validated by this workflow.",
        "## Validation Results",
        "Benchmark gates completed by this packaging candidate: 0 of 3.",
        "Textual-memory pass@1 rescue count: pending.",
        "Trajectory-to-skill pass@1 rescue count: pending.",
        "Agent-system pass@1 rescue count: pending.",
        "## Security And Privacy",
        "No analytics, crash reporting, telemetry, or diagnostics upload is enabled by default.",
        "Credential-canary verification for release assets: pending.",
        "## Install, Upgrade, And Uninstall",
        "Install:",
        "Upgrade:",
        "Uninstall:",
    ],
)
def test_candidate_manifest_rejects_incomplete_release_notes(
    tmp_path: Path,
    required_marker: str,
) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    notes = tmp_path / "release-notes.md"
    notes.write_text(
        notes.read_text(encoding="utf-8").replace(required_marker, "omitted", 1),
        encoding="utf-8",
    )

    with pytest.raises(candidate.CandidateError, match="Release notes"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="a" * 64,
        )


def test_candidate_manifest_rejects_post_manifest_asset_mutation(tmp_path: Path) -> None:
    candidate = _load_module()
    paths = _write_candidate_inputs(tmp_path)
    manifest = candidate.create_candidate_manifest(
        tmp_path,
        source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        version="0.1.0",
        architecture="aarch64",
        rust_target="aarch64-apple-darwin",
        registry_digest="b" * 64,
    )

    paths["wheel"].write_bytes(paths["wheel"].read_bytes() + b"tampered")

    errors = candidate.validate_candidate_manifest(manifest)
    assert any("digest mismatch" in error and paths["wheel"].name in error for error in errors)


def test_candidate_manifest_rejects_managed_runtime_source_mutation(tmp_path: Path) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    source = tmp_path / candidate.MANAGED_RUNTIME_SOURCE_NAME
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["asset"]["id"] += 1
    _write_json(source, payload)

    with pytest.raises(candidate.CandidateError, match="runtime source evidence"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="b" * 64,
        )


def test_candidate_manifest_rejects_false_universal_architecture(tmp_path: Path) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)

    try:
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="universal",
            rust_target="aarch64-apple-darwin",
            registry_digest="c" * 64,
        )
    except candidate.CandidateError as exc:
        assert "actual host architecture" in str(exc)
    else:
        raise AssertionError("universal candidate architecture must fail closed")


def test_candidate_manifest_rejects_framework_lock_for_other_wheel(tmp_path: Path) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path, locked_digest="d" * 64)

    try:
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="e" * 64,
        )
    except candidate.CandidateError as exc:
        assert "exact Core wheel" in str(exc)
    else:
        raise AssertionError("mismatched framework lock must fail closed")


def test_candidate_manifest_requires_passing_security_evidence(tmp_path: Path) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path, npm_vulnerabilities=1)

    try:
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="f" * 64,
        )
    except candidate.CandidateError as exc:
        assert "security evidence" in str(exc)
    else:
        raise AssertionError("failing security evidence must fail closed")


def test_candidate_manifest_rejects_mismatched_dmg_mach_o_slices(tmp_path: Path) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path, dmg_slices=["x86_64"])

    try:
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="f" * 64,
        )
    except candidate.CandidateError as exc:
        assert str(exc) == (
            "Mounted-DMG app and detached-copy Mach-O evidence do not match"
        )
    else:
        raise AssertionError("mismatched detached-copy Mach-O slices must fail closed")


def test_candidate_manifest_rejects_smoke_bound_to_different_dmg(tmp_path: Path) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    evidence_path = tmp_path / "app-bundle-smoke.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["source_dmg"]["sha256"] = "f" * 64
    _write_json(evidence_path, evidence)

    with pytest.raises(candidate.CandidateError, match="source DMG"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="f" * 64,
        )


def test_candidate_manifest_rejects_wrong_smoke_launch_origin(tmp_path: Path) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    evidence_path = tmp_path / "dmg-copy-smoke.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["launch_origin"] = "mounted_dmg"
    _write_json(evidence_path, evidence)

    with pytest.raises(candidate.CandidateError, match="launch origin"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="f" * 64,
        )


def test_candidate_manifest_rejects_legacy_native_smoke_schema(tmp_path: Path) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    evidence_path = tmp_path / "app-bundle-smoke.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["schema_version"] = 2
    _write_json(evidence_path, evidence)

    with pytest.raises(candidate.CandidateError, match="schema version"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="f" * 64,
        )


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":2,"schema_version":3}\n',
        '{"source_dmg":{"sha256":"' + "a" * 64 + '","sha256":"' + "b" * 64 + '"}}\n',
    ],
)
def test_candidate_json_rejects_duplicate_keys(tmp_path: Path, payload: str) -> None:
    candidate = _load_module()
    path = tmp_path / "duplicate.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(candidate.CandidateError, match="duplicate key"):
        candidate._load_json(path)


def test_candidate_manifest_rejects_mismatched_smoke_binary_digests(
    tmp_path: Path,
) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    evidence_path = tmp_path / "dmg-copy-smoke.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["binary_sha256"]["native_executable"] = "f" * 64
    _write_json(evidence_path, evidence)

    with pytest.raises(candidate.CandidateError, match="binary digests"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="f" * 64,
        )


@pytest.mark.parametrize(
    "filename",
    ("app-bundle-smoke.json", "dmg-copy-smoke.json"),
)
def test_candidate_manifest_rejects_the_display_name_as_executable_identity(
    tmp_path: Path,
    filename: str,
) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    evidence_path = tmp_path / filename
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["native_executable"] = "OpenEvo Desktop"
    _write_json(evidence_path, evidence)

    with pytest.raises(candidate.CandidateError, match="Tauri executable"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="f" * 64,
        )


def test_candidate_manifest_rejects_open_core_compatibility_schema(tmp_path: Path) -> None:
    candidate = _load_module()

    try:
        candidate._validate_core_compatibility(
            {
                "python_requires": ">=3.11",
                "supported_platforms": ["linux-x86_64"],
                "extra": True,
            }
        )
    except candidate.CandidateError as exc:
        assert "closed release schema" in str(exc)
    else:
        raise AssertionError("open Core compatibility schema must fail closed")


def test_candidate_manifest_rejects_unsupported_core_compatibility() -> None:
    candidate = _load_module()

    for compatibility in (
        {"python_requires": ">=3.12", "supported_platforms": ["linux-x86_64"]},
        {"python_requires": ">=3.11", "supported_platforms": ["linux-aarch64"]},
    ):
        try:
            candidate._validate_core_compatibility(compatibility)
        except candidate.CandidateError as exc:
            assert "unsupported" in str(exc)
        else:
            raise AssertionError("unsupported Core compatibility must fail closed")


def test_candidate_manifest_requires_expected_core_platform(tmp_path: Path) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    manifest = candidate.create_candidate_manifest(
        tmp_path,
        source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        version="0.1.0",
        architecture="aarch64",
        rust_target="aarch64-apple-darwin",
        registry_digest="a" * 64,
    )

    errors = candidate.validate_candidate_manifest(
        manifest,
        expected_core_platform="linux-aarch64",
    )

    assert any("Core platform" in error for error in errors)


def test_candidate_manifest_rejects_unclassified_directory(tmp_path: Path) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    manifest = candidate.create_candidate_manifest(
        tmp_path,
        source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        version="0.1.0",
        architecture="aarch64",
        rust_target="aarch64-apple-darwin",
        registry_digest="a" * 64,
    )
    (tmp_path / "unclassified").mkdir()

    errors = candidate.validate_candidate_manifest(manifest)

    assert any("non-regular" in error for error in errors)


def _write_candidate_inputs(
    root: Path,
    *,
    locked_digest: str | None = None,
    npm_vulnerabilities: int = 0,
    dmg_slices: list[str] | None = None,
) -> dict[str, Path]:
    wheel = root / "openevo-0.1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            "openevo-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: openevo\n"
            "Version: 0.1.0\n"
            "Requires-Python: >=3.11\n\n",
        )
        archive.writestr(
            "openevo-0.1.0.dist-info/entry_points.txt",
            "[console_scripts]\n"
            "openevo-backend = openevo.backend.launcher:main\n"
            "openevo-core-service = openevo.backend.service:main\n",
        )
        archive.writestr("openevo/__init__.py", "__version__ = '0.1.0'\n")
    wheel_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    framework_lock = root / "framework-lock.json"
    framework_lock.write_text(
        json.dumps(
            {
                "distribution": "openevo",
                "distribution_digest": locked_digest or wheel_digest,
                "distribution_version": "0.1.0",
                "schema_version": "1",
                "wheel_filename": wheel.name,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    dmg = root / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"dmg")
    (root / "release-notes.md").write_text(_release_notes_text(), encoding="utf-8")
    requirements = root / "python-requirements.txt"
    requirements.write_text("fastapi==1.0\n", encoding="utf-8")
    _write_json(
        root / "dependency-inventory.json",
        {
            "schema_version": 2,
            "ecosystems": {
                "python": {
                    "lockfile_sha256": _sha256(Path("uv.lock")),
                    "packages": 1,
                },
                "npm": {
                    "lockfile_sha256": _sha256(Path("desktop/package-lock.json")),
                    "packages": 1,
                },
                "cargo": {
                    "lockfile_sha256": _sha256(Path("desktop/src-tauri/Cargo.lock")),
                    "packages": 1,
                },
            },
        },
    )
    _write_json(
        root / "license-inventory.json",
        {
            "schema_version": 1,
            "project_license_sha256": _sha256(Path("LICENSE")),
            "ecosystems": {
                ecosystem: {"packages": 1, "unresolved": 0}
                for ecosystem in ("python", "npm", "cargo")
            },
        },
    )
    _write_json(
        root / "security-audit.json",
        {
            "schema_version": 2,
            "audits": {
                "npm-audit-high": {
                    "status": "passed" if npm_vulnerabilities == 0 else "failed",
                    "vulnerabilities": npm_vulnerabilities,
                },
                "pip-audit": {
                    "audited_packages": 1,
                    "requirements_sha256": _sha256(requirements),
                    "status": "passed",
                    "vulnerabilities": 0,
                },
                "cargo-audit": {"status": "passed", "vulnerabilities": 0},
            },
        },
    )
    mounted_smoke_evidence = {
        "schema_version": 3,
        "launch_origin": "mounted_dmg",
        "source_dmg": {
            "filename": dmg.name,
            "sha256": _sha256(dmg),
        },
        "binary_sha256": {
            "native_executable": "1" * 64,
            "bundled_external_bin": "2" * 64,
        },
        "native_executable": "openevo-desktop",
        "bundled_external_bin": "openevo-desktop-sidecar",
        "renderer_ready": True,
        "sidecar_ready": True,
        "bundled_external_bin_resolved": True,
        "native_listener_fd_handoff": True,
        "native_executable_fd_handoff": True,
        "process_group_cleanup": True,
        "mach_o": {
            "native_executable": {
                "file_output": "Mach-O 64-bit executable arm64",
                "slices": ["arm64"],
            },
            "bundled_external_bin": {
                "file_output": "Mach-O 64-bit executable arm64",
                "slices": ["arm64"],
            },
        },
    }
    copied_smoke_evidence = json.loads(json.dumps(mounted_smoke_evidence))
    copied_smoke_evidence["launch_origin"] = "detached_copy"
    for binary in ("native_executable", "bundled_external_bin"):
        copied_smoke_evidence["mach_o"][binary]["slices"] = dmg_slices or ["arm64"]
    _write_json(root / "app-bundle-smoke.json", mounted_smoke_evidence)
    _write_json(root / "dmg-copy-smoke.json", copied_smoke_evidence)
    candidate = _load_module()
    _write_json(
        root / candidate.MANAGED_RUNTIME_SOURCE_NAME,
        candidate._managed_runtime_source_evidence(),
    )
    return {"wheel": wheel, "framework_lock": framework_lock, "dmg": dmg}


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
