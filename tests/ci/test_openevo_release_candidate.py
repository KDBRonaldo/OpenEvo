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


def _load_managed_runtime_smoke_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/smoke_managed_runtime_archive.py"
    spec = importlib.util.spec_from_file_location("smoke_managed_runtime_archive", path)
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
    assert notes.startswith("# OpenEvo Desktop 0.1.0 Preview\n")
    assert "draft" not in notes.casefold()
    assert "Codex subscription transcript mode: packaged and declared in this Preview." in notes
    assert (
        "Candidate-bound real Codex Subscription science E2E: required before public "
        "Preview publication."
    ) in notes
    assert "A candidate that has not passed that gate is not public." in notes
    assert "System OpenSSH remote workspace" in notes
    assert "verified OpenEvo Daemon/Core v2" in notes
    assert "two immutable science Tasks" in notes
    assert "next-Task Runtime Context reuse" in notes
    assert "Remote Core" not in notes
    assert "Codex subscription transcript mode: available in this candidate." not in notes
    assert "Self-Deployed Reference mode: unavailable in this Preview." in notes
    assert "openevo-science-runtime-0.1.1-linux-amd64.tar.gz" in notes
    assert "Managed Science runtime source asset ID: 481361975." in notes
    assert "Credential-canary verification for release assets: pending." in notes
    assert "Current local Desktop data under ~/Library/Application Support" in notes
    assert "Legacy Preview data under ~/.openevo/desktop is preserved without being read" in notes
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
        "docker:29.3-dind@sha256:a8d074fe486e65abe4ce251c264b78727be7b63a789ccb1eff2dcc786b443cb2",
        "smoke_managed_runtime_archive.py",
        "--fixed-docker-fault-injection",
        "--expected-docker-socket-device",
        "--expected-docker-socket-inode",
        "unshare --mount --propagation private",
        'mount --bind "$OPENEVO_DIND_SOCKET" /var/run/docker.sock',
        'sudo docker --host "unix://$dind_socket"',
        'test "$(stat -c "%u:%a" "$smoke_archive")" = "0:600"',
    ):
        assert value in workflow
    assert workflow.count("self-deployed") == 0


def test_candidate_workflow_roundtrips_closed_playwright_evidence() -> None:
    workflow = Path(".github/workflows/openevo-desktop-candidate.yml").read_text(encoding="utf-8")
    linux = workflow[
        workflow.index("  linux-daemon-bundle:") : workflow.index("  macos-candidate:")
    ]
    macos = workflow[
        workflow.index("  macos-candidate:") : workflow.index("  linux-core-candidate:")
    ]
    artifact_name = (
        "openevo-desktop-playwright-${{ github.sha }}-"
        "${{ github.run_id }}-${{ github.run_attempt }}"
    )

    for marker in (
        "Gate non-release simulator preview",
        "CI=1 npm run test:product-browser:preview",
        "Produce packaged release-composition Playwright report",
        'PLAYWRIGHT_BLOB_OUTPUT_FILE="$blob_dir/release-packaged.zip"',
        "npm run test:product-browser:release-readonly -- --reporter=blob",
        'PLAYWRIGHT_JSON_OUTPUT_NAME="$RUNNER_TEMP/playwright-raw-report.json"',
        'npx playwright merge-reports --reporter=json "$blob_dir"',
        "write-playwright-evidence",
        '--raw-report "$RUNNER_TEMP/playwright-raw-report.json"',
        '--sanitized-report-output "$evidence_dir/playwright-report.json"',
        "validate-playwright-evidence",
        '--source-commit "$GITHUB_SHA"',
        '--run-id "$GITHUB_RUN_ID"',
        '--run-attempt "$GITHUB_RUN_ATTEMPT"',
        "playwright-candidate-evidence.json",
        "playwright-report.json",
        "packaged-web-manifest.json",
        artifact_name,
    ):
        assert marker in linux
    preview_step = linux[
        linux.index("      - name: Gate non-release simulator preview") : linux.index(
            "      - name: Produce packaged release-composition Playwright report"
        )
    ]
    release_step = linux[
        linux.index(
            "      - name: Produce packaged release-composition Playwright report"
        ) : linux.index("      - uses: actions/setup-python")
    ]
    assert "PLAYWRIGHT_BLOB_OUTPUT_FILE" not in preview_step
    assert "merge-reports" not in preview_step
    assert "test:product-browser:preview" not in release_step
    assert (
        linux.index("npm run test:product-browser:preview")
        < linux.index("npm run test:product-browser:release-readonly")
        < linux.index("npx playwright merge-reports")
        < linux.index("write-playwright-evidence")
        < linux.index("Upload immutable candidate Playwright evidence")
    )
    for marker in (
        artifact_name,
        "Revalidate candidate Playwright evidence on macOS",
        "validate-playwright-evidence",
        'cmp "$RUNNER_TEMP/openevo-desktop-playwright/packaged-web-manifest.json"',
        "candidate-artifacts/",
    ):
        assert marker in macos
    copy_step = macos[macos.index("      - name: Copy exact candidate bytes") :]
    assert (
        macos.index("Download exact candidate Playwright evidence")
        < macos.index("Revalidate candidate Playwright evidence on macOS")
        < macos.index("Build unsigned Desktop DMG")
    )
    assert copy_step.index("playwright-candidate-evidence.json") < copy_step.index(
        "openevo_release_candidate.py create"
    )
    assert "path: candidate-artifacts/*" in macos


@pytest.mark.parametrize(
    ("mode", "markers", "calls"),
    [
        ("fail_tag", ["fail_tag"], ["tag"]),
        ("cancel_tag", ["cancel_tag"], ["tag"]),
        (
            "fail_remove",
            ["fail_remove_tag", "fail_remove_remove"],
            ["tag", "remove"],
        ),
    ],
)
def test_managed_runtime_smoke_requires_exact_fault_injection_evidence(
    tmp_path: Path,
    mode: str,
    markers: list[str],
    calls: list[str],
) -> None:
    smoke = _load_managed_runtime_smoke_module()
    release = smoke.MANAGED_RUNTIME_ARCHIVE_RELEASE
    encoded_calls = {
        "tag": ["tag", release.oci_index_id, release.aliases[0]],
        "remove": ["image", "rm", release.aliases[0]],
    }
    (tmp_path / "docker-proxy-injections.log").write_text(
        "\n".join(markers) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "docker-proxy.log").write_text(
        "".join(json.dumps(encoded_calls[name]) + "\n" for name in calls),
        encoding="utf-8",
    )

    smoke._assert_fault_injection(tmp_path, mode)

    (tmp_path / "docker-proxy-injections.log").write_text("", encoding="utf-8")
    with pytest.raises(smoke.SmokeError, match="exact branch"):
        smoke._assert_fault_injection(tmp_path, mode)

    (tmp_path / "docker-proxy-injections.log").write_text(
        "\n".join([*markers, markers[-1]]) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(smoke.SmokeError, match="exact branch"):
        smoke._assert_fault_injection(tmp_path, mode)


def test_managed_runtime_smoke_checks_isolation_before_docker_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_managed_runtime_smoke_module()
    monkeypatch.setattr(smoke, "verify_managed_runtime_archive", lambda *_args, **_kwargs: None)

    def reject_authority(**_kwargs):
        raise smoke.SmokeError("not isolated")

    def unexpected_cleanup(*_args, **_kwargs):
        raise AssertionError("Docker cleanup ran before isolation was verified")

    monkeypatch.setattr(smoke, "_require_isolated_docker_authority", reject_authority)
    monkeypatch.setattr(smoke, "_cleanup_images", unexpected_cleanup)

    with pytest.raises(smoke.SmokeError, match="not isolated"):
        smoke.smoke(
            tmp_path / "runtime.tar.gz",
            tmp_path / "evidence.json",
            fixed_docker_fault_injection=True,
            expected_socket_device=1,
            expected_socket_inode=1,
        )


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
        "apiUrl": ("https://api.github.com/repos/CompLifeLab-ZJU/OpenEvo/releases/356072935"),
        "body": body,
        "isDraft": True,
        "isPrerelease": True,
        "name": "OpenEvo Desktop 0.1.0 Preview",
        "tagName": "openevo-desktop-v0.1.0-exhibition.123.2",
        "targetCommitish": "8e45af371eef49a86530a849041f7dcf047620ec",
        "url": (
            "https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/untagged-7a9ca728f876fa16a90d"
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
        "OpenEvo Desktop 0.1.0 Preview",
        "--expected-repository",
        "CompLifeLab-ZJU/OpenEvo",
        "--expected-owner",
        "d" * 32,
        "--release-id-output",
        str(release_id),
    ]

    assert (
        candidate.validate_draft_release_metadata(
            metadata,
            release_notes=notes,
            expected_tag="openevo-desktop-v0.1.0-exhibition.123.2",
            expected_target="8e45af371eef49a86530a849041f7dcf047620ec",
            expected_title="OpenEvo Desktop 0.1.0 Preview",
            expected_repository="CompLifeLab-ZJU/OpenEvo",
            expected_owner="d" * 32,
        )
        == []
    )
    assert candidate.main(validation_arguments) == 0
    assert release_id.read_text(encoding="ascii") == "356072935\n"
    assert release_id.stat().st_mode & 0o777 == 0o600
    assert candidate.main(validation_arguments) == 1
    assert release_id.read_text(encoding="ascii") == "356072935\n"


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
            "https://api.github.com/repos/attacker/unrelated/releases/356072935",
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
        expected_title="OpenEvo Desktop 0.1.0 Preview",
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

    assert (
        candidate.validate_candidate_manifest(
            manifest,
            expected_source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        )
        == []
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 10
    assert payload["desktop_contract"] == candidate._desktop_contract_manifest("0.1.0")
    assert payload["lifecycle_evidence"] == candidate._lifecycle_evidence_requirements()
    assert payload["lifecycle_evidence"]["require_renderer_secret_canary_absence"] is True
    assert payload["macos"]["ssh_askpass_helper"] == {
        "architecture": "arm64",
        "byte_size": 51,
        "mode": "0755",
        "relative_path": "Contents/MacOS/openevo-ssh-askpass",
        "sha256": "e" * 64,
        "signature": "adhoc",
    }
    assert payload["release"] == {
        "app_bundle_signature": "adhoc",
        "channel": "unsigned-preview",
        "developer_id_signed": False,
        "macos_code_signing": {
            "disable_library_validation": False,
            "hardened_runtime": False,
            "identity": "adhoc",
        },
        "notarized": False,
        "quarantine_removal_tested": True,
    }
    assert payload["macos"] == {
        "architecture": "aarch64",
        "minimum_system_version": "12.0",
        "native_architectures": {
            "bundled_external_bin": ["arm64"],
            "native_executable": ["arm64"],
        },
        "rust_target": "aarch64-apple-darwin",
        "rust_toolchain": "1.95.0",
        "ssh_askpass_helper": {
            "architecture": "arm64",
            "byte_size": 51,
            "mode": "0755",
            "relative_path": "Contents/MacOS/openevo-ssh-askpass",
            "sha256": "e" * 64,
            "signature": "adhoc",
        },
    }
    by_role = {entry["role"]: entry for entry in payload["files"]}
    assert by_role["desktop_dmg"]["filename"] == paths["dmg"].name
    assert (
        by_role["core_wheel"]["sha256"] == hashlib.sha256(paths["wheel"].read_bytes()).hexdigest()
    )
    assert by_role["framework_lock"]["filename"] == "framework-lock.json"
    assert by_role["daemon_bundle"]["filename"] == candidate.DAEMON_BUNDLE_NAME
    assert by_role["daemon_manifest"]["filename"] == candidate.DAEMON_MANIFEST_NAME
    assert by_role["daemon_mounted_resource"]["filename"] == (
        candidate.DAEMON_MOUNTED_EVIDENCE_NAME
    )
    assert by_role["daemon_copy_resource"]["filename"] == candidate.DAEMON_COPY_EVIDENCE_NAME
    assert by_role["core_descriptor"]["filename"] == "core-install-artifact.json"
    assert by_role["checksums"]["filename"] == "SHA256SUMS"
    assert by_role["app_bundle_smoke"]["filename"] == "app-bundle-smoke.json"
    assert by_role["dmg_copy_smoke"]["filename"] == "dmg-copy-smoke.json"
    assert by_role["launchservices_smoke"]["filename"] == "launchservices-smoke.json"
    assert by_role["managed_runtime_source"]["filename"] == "managed-runtime-source.json"
    assert by_role["playwright_evidence"]["filename"] == candidate.PLAYWRIGHT_EVIDENCE_NAME
    assert by_role["playwright_report"]["filename"] == candidate.PLAYWRIGHT_REPORT_NAME
    assert by_role["packaged_web_manifest"]["filename"] == candidate.PACKAGED_WEB_MANIFEST_NAME
    assert payload["core"]["registry_digest"] == "a" * 64
    assert payload["daemon"]["artifact_sha256"] == by_role["daemon_bundle"]["sha256"]
    assert payload["daemon"]["manifest_sha256"] == by_role["daemon_manifest"]["sha256"]
    assert payload["daemon"]["release_identity"] == "b" * 64
    assert payload["managed_runtime"] == candidate._managed_runtime_manifest()
    assert payload["managed_runtime"]["capability"] == {
        "capture_mode": "transcript",
        "execution_mode": "codex_subscription_transcript",
        "harness_id": "codex",
        "token_level_metrics_available": False,
    }
    assert "self-deployed" not in json.dumps(payload["managed_runtime"])
    descriptor = json.loads((tmp_path / "core-install-artifact.json").read_text(encoding="utf-8"))
    assert descriptor["artifact"] == by_role["core_wheel"]
    assert descriptor["framework_lock"] == by_role["framework_lock"]
    assert descriptor["source_commit"] == payload["source_commit"]
    assert descriptor["schema_version"] == 2
    assert descriptor["compatibility"] == {
        "python_requires": ">=3.11",
        "supported_platforms": ["linux-x86_64"],
    }


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("desktop_contract", "openapi_sha256", "f" * 64),
        ("desktop_contract", "feature_flags", []),
        ("lifecycle_evidence", "maximum_reservation_latency_ms", 0),
        ("lifecycle_evidence", "minimum_terminal_duration_ms", 0),
        ("lifecycle_evidence", "require_relaunch_recovery", False),
        ("lifecycle_evidence", "require_secret_canary_absence", False),
        ("lifecycle_evidence", "require_renderer_secret_canary_absence", False),
    ],
)
def test_candidate_manifest_rejects_changed_lifecycle_contract(
    tmp_path: Path,
    section: str,
    field: str,
    value: object,
) -> None:
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
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[section][field] = value
    _write_json(manifest, payload)

    assert candidate.validate_candidate_manifest(manifest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signed", False),
        ("developer_id_signed", True),
        ("app_bundle_signature", "developer_id"),
        ("notarized", True),
        ("quarantine_removal_tested", False),
    ],
)
def test_candidate_manifest_rejects_ambiguous_or_false_signature_claims(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
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
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["release"][field] = value
    _write_json(manifest, payload)

    errors = candidate.validate_candidate_manifest(manifest)

    assert errors


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("identity", "developer_id"),
        ("hardened_runtime", True),
        ("disable_library_validation", True),
        ("unexpected", False),
    ],
)
def test_candidate_manifest_rejects_macos_signing_policy_changes(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
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
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["release"]["macos_code_signing"][field] = value
    _write_json(manifest, payload)

    errors = candidate.validate_candidate_manifest(manifest)

    assert errors


def test_candidate_manifest_rejects_daemon_for_other_registry(tmp_path: Path) -> None:
    candidate = _load_module()
    paths = _write_candidate_inputs(tmp_path)
    daemon = json.loads(paths["daemon_manifest"].read_text(encoding="utf-8"))
    daemon["core"]["registry_digest"] = "f" * 64
    _write_json(paths["daemon_manifest"], daemon)

    with pytest.raises(candidate.CandidateError, match="candidate Core wheel and lock"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="a" * 64,
        )


def test_candidate_manifest_rejects_unverified_daemon_dmg_evidence(tmp_path: Path) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    evidence_path = tmp_path / candidate.DAEMON_COPY_EVIDENCE_NAME
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    next(
        entry
        for entry in evidence["release_assets"]["files"]
        if entry["relative_path"].endswith("/openevo-daemon-linux-x86_64")
    )["sha256"] = "f" * 64
    _write_json(evidence_path, evidence)

    with pytest.raises(candidate.CandidateError, match="exact packaged release assets"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="a" * 64,
        )


def test_candidate_manifest_rejects_invalid_or_mismatched_askpass_inventory(
    tmp_path: Path,
) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    copied = tmp_path / candidate.DAEMON_COPY_EVIDENCE_NAME
    evidence = json.loads(copied.read_text(encoding="utf-8"))
    evidence["ssh_askpass_helper"]["sha256"] = "f" * 64
    _write_json(copied, evidence)

    with pytest.raises(candidate.CandidateError, match="askpass helper.*differ"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="a" * 64,
        )

    evidence["ssh_askpass_helper"]["sha256"] = "not-a-digest"
    _write_json(copied, evidence)
    with pytest.raises(candidate.CandidateError, match="askpass helper inventory"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="a" * 64,
        )


def test_managed_runtime_source_binds_prerelease_asset_and_download(
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
            "draft": False,
            "id": release.asset_release_id,
            "prerelease": True,
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
        "Codex subscription transcript mode: packaged and declared in this Preview.",
        "Candidate-bound real Codex Subscription science E2E: required before public Preview publication.",
        "A candidate that has not passed that gate is not public.",
        "Self-Deployed Reference mode: unavailable in this Preview.",
        "## Known Limitations",
        "Parameter evolution is not included in this Preview.",
        "PyPI is not used for this release.",
        "Only the declared architecture was built.",
        "command-line quarantine removal is validated.",
        "## Validation Results",
        "Benchmark gates completed by this Preview: 0 of 3.",
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
        registry_digest="a" * 64,
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
            registry_digest="a" * 64,
        )


def test_playwright_candidate_evidence_binds_report_web_build_and_run(
    tmp_path: Path,
) -> None:
    candidate = _load_module()
    paths = _write_playwright_inputs(tmp_path)

    evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))

    assert evidence["schema_version"] == 2
    assert evidence["simulator"] is False
    assert evidence["provider_kind"] == "desktop_sidecar"
    assert evidence["composition"] == "packaged_web"
    assert evidence["source_commit"] == "8e45af371eef49a86530a849041f7dcf047620ec"
    assert evidence["run"] == {"attempt": 2, "id": 123456}
    assert evidence["browser"] == {"name": "chromium", "version": "149.0.7827.55"}
    assert evidence["status"] == "passed"
    assert len(evidence["tests"]) == 3
    assert {entry["project"] for entry in evidence["tests"]} == {
        "release-packaged-1440",
        "release-packaged-1024",
        "release-packaged-760",
    }
    assert evidence["packaged_web"]["manifest"]["sha256"] == _sha256(paths["web_manifest"])
    assert evidence["report"]["sha256"] == _sha256(paths["report"])
    sanitized_report = paths["report"].read_text(encoding="utf-8")
    assert "/home/runner" not in sanitized_report
    assert "npm run" not in sanitized_report
    assert "rootDir" not in sanitized_report
    assert "webServer" not in sanitized_report
    candidate._validate_playwright_candidate_evidence(
        paths["evidence"],
        report_path=paths["report"],
        packaged_web_manifest_path=paths["web_manifest"],
        expected_source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        expected_run_id=123456,
        expected_run_attempt=2,
    )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("source_commit", "f" * 40, "different candidate run"),
        ("status", "pending", "identity or status"),
    ],
)
def test_playwright_candidate_evidence_rejects_rewritten_identity_or_status(
    tmp_path: Path,
    field: str,
    value: str,
    error: str,
) -> None:
    candidate = _load_module()
    paths = _write_playwright_inputs(tmp_path)
    evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
    evidence[field] = value
    _write_json(paths["evidence"], evidence)

    with pytest.raises(candidate.CandidateError, match=error):
        candidate._validate_playwright_candidate_evidence(
            paths["evidence"],
            report_path=paths["report"],
            packaged_web_manifest_path=paths["web_manifest"],
            expected_source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("skipped", "first-attempt pass"),
        ("retry", "first-attempt pass"),
        ("flaky", "aggregate status"),
    ],
)
def test_playwright_candidate_evidence_rejects_non_clean_results(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    candidate = _load_module()
    paths = _write_playwright_inputs(tmp_path)
    report = json.loads(paths["raw_report"].read_text(encoding="utf-8"))
    test = report["suites"][0]["specs"][0]["tests"][0]
    if mutation == "skipped":
        test["status"] = "skipped"
    elif mutation == "retry":
        test["results"][0]["retry"] = 1
    else:
        report["stats"]["flaky"] = 1
    _write_json(paths["raw_report"], report)

    with pytest.raises(candidate.CandidateError, match=error):
        candidate._playwright_test_results(paths["raw_report"])


def test_playwright_candidate_evidence_rejects_simulator_project(
    tmp_path: Path,
) -> None:
    candidate = _load_module()
    paths = _write_playwright_inputs(tmp_path)
    report = json.loads(paths["raw_report"].read_text(encoding="utf-8"))
    report["config"]["projects"][0] = {
        "id": "desktop-1440",
        "name": "desktop-1440",
    }
    _write_json(paths["raw_report"], report)

    with pytest.raises(candidate.CandidateError, match="project identity"):
        candidate._playwright_test_results(paths["raw_report"])


def test_playwright_candidate_evidence_rejects_web_build_digest_mutation(
    tmp_path: Path,
) -> None:
    candidate = _load_module()
    paths = _write_playwright_inputs(tmp_path)
    manifest = json.loads(paths["web_manifest"].read_text(encoding="utf-8"))
    manifest["build_digest"] = "f" * 64
    _write_json(paths["web_manifest"], manifest)

    with pytest.raises(candidate.CandidateError, match="build digest"):
        candidate._validate_playwright_candidate_evidence(
            paths["evidence"],
            report_path=paths["report"],
            packaged_web_manifest_path=paths["web_manifest"],
        )


def test_playwright_candidate_evidence_requires_exact_packaged_release_matrix(
    tmp_path: Path,
) -> None:
    candidate = _load_module()
    paths = _write_playwright_inputs(tmp_path)
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    report["tests"].pop()
    _write_json(paths["report"], report)

    with pytest.raises(candidate.CandidateError, match="test inventory"):
        candidate._validate_playwright_candidate_evidence(
            paths["evidence"],
            report_path=paths["report"],
            packaged_web_manifest_path=paths["web_manifest"],
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("simulator", True),
        ("provider_kind", "contract_simulator"),
        ("composition", "source_preview"),
    ],
)
def test_playwright_candidate_evidence_rejects_false_release_provenance(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    candidate = _load_module()
    paths = _write_playwright_inputs(tmp_path)
    evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
    evidence[field] = value
    _write_json(paths["evidence"], evidence)

    with pytest.raises(candidate.CandidateError, match="identity or status"):
        candidate._validate_playwright_candidate_evidence(
            paths["evidence"],
            report_path=paths["report"],
            packaged_web_manifest_path=paths["web_manifest"],
        )


def test_candidate_manifest_requires_playwright_evidence_role(tmp_path: Path) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    (tmp_path / candidate.PLAYWRIGHT_EVIDENCE_NAME).unlink()

    with pytest.raises(candidate.CandidateError, match="Candidate input is missing"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="a" * 64,
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
            registry_digest="a" * 64,
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
            registry_digest="a" * 64,
        )
    except candidate.CandidateError as exc:
        assert str(exc) == ("Mounted-DMG app and detached-copy Mach-O evidence do not match")
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
            registry_digest="a" * 64,
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
            registry_digest="a" * 64,
        )


def test_candidate_manifest_rejects_failed_launchservices_quarantine_evidence(
    tmp_path: Path,
) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    evidence_path = tmp_path / "launchservices-smoke.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["quarantine_present_before_allow"] = False
    _write_json(evidence_path, evidence)

    with pytest.raises(candidate.CandidateError, match="quarantine-allow evidence"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="a" * 64,
        )


def test_candidate_manifest_rejects_launchservices_evidence_from_another_build(
    tmp_path: Path,
) -> None:
    candidate = _load_module()
    _write_candidate_inputs(tmp_path)
    evidence_path = tmp_path / "launchservices-smoke.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["binary_sha256"]["bundled_external_bin"] = "f" * 64
    _write_json(evidence_path, evidence)

    with pytest.raises(candidate.CandidateError, match="candidate binaries"):
        candidate.create_candidate_manifest(
            tmp_path,
            source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            version="0.1.0",
            architecture="aarch64",
            rust_target="aarch64-apple-darwin",
            registry_digest="a" * 64,
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
            registry_digest="a" * 64,
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
            registry_digest="a" * 64,
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
            registry_digest="a" * 64,
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


def test_preview_snapshot_revalidates_unchanged_public_release(
    tmp_path: Path,
) -> None:
    candidate, fixture = _write_preview_release_fixture(tmp_path)
    public_metadata = json.loads(fixture["metadata"].read_text(encoding="utf-8"))
    public_metadata["draft"] = False
    public_metadata["immutable"] = True
    public_metadata["html_url"] = (
        "https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/"
        "openevo-desktop-v0.1.0-exhibition.123.2"
    )
    public_path = tmp_path / "public-release.json"
    _write_json(public_path, public_metadata)

    candidate.write_preview_release_snapshot(
        tmp_path / "public-snapshot.json",
        metadata_path=public_path,
        candidate_root=fixture["candidate_root"],
        baseline_path=fixture["snapshot"],
        expected_repository="CompLifeLab-ZJU/OpenEvo",
        expected_release_id=356072935,
        expected_tag="openevo-desktop-v0.1.0-exhibition.123.2",
        expected_source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        expected_manifest_sha256=fixture["manifest_sha256"],
        expected_run_id=123456,
        expected_run_attempt=2,
        expected_draft=False,
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("body", "reviewer edited the draft"),
        ("name", "Edited Preview title"),
        ("target_commitish", "f" * 40),
        ("id", 356072936),
        ("immutable", True),
    ],
)
def test_preview_asset_plan_rejects_mutated_draft_metadata(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    candidate, fixture = _write_preview_release_fixture(tmp_path)
    metadata = json.loads(fixture["metadata"].read_text(encoding="utf-8"))
    metadata[field] = replacement
    mutated = tmp_path / "mutated-draft.json"
    _write_json(mutated, metadata)

    with pytest.raises(candidate.CandidateError):
        candidate.write_preview_asset_plan(
            tmp_path / "asset-plan.tsv",
            metadata_path=mutated,
            baseline_path=fixture["snapshot"],
            expected_draft=True,
        )


def test_preview_asset_plan_rejects_replaced_asset_identity(tmp_path: Path) -> None:
    candidate, fixture = _write_preview_release_fixture(tmp_path)
    metadata = json.loads(fixture["metadata"].read_text(encoding="utf-8"))
    metadata["assets"][0]["digest"] = "sha256:" + "f" * 64
    mutated = tmp_path / "mutated-assets.json"
    _write_json(mutated, metadata)

    with pytest.raises(candidate.CandidateError, match="asset identities changed"):
        candidate.write_preview_asset_plan(
            tmp_path / "asset-plan.tsv",
            metadata_path=mutated,
            baseline_path=fixture["snapshot"],
            expected_draft=True,
        )


def test_preview_snapshot_rejects_wrong_expected_manifest_digest(tmp_path: Path) -> None:
    candidate, fixture = _write_preview_release_fixture(tmp_path)

    with pytest.raises(candidate.CandidateError, match="manifest digest"):
        candidate.write_preview_release_snapshot(
            tmp_path / "wrong-digest-snapshot.json",
            metadata_path=fixture["metadata"],
            candidate_root=fixture["candidate_root"],
            baseline_path=fixture["snapshot"],
            expected_repository="CompLifeLab-ZJU/OpenEvo",
            expected_release_id=356072935,
            expected_tag="openevo-desktop-v0.1.0-exhibition.123.2",
            expected_source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            expected_manifest_sha256="f" * 64,
            expected_run_id=123456,
            expected_run_attempt=2,
            expected_draft=True,
        )


def test_preview_snapshot_rejects_changed_downloaded_asset(tmp_path: Path) -> None:
    candidate, fixture = _write_preview_release_fixture(tmp_path)
    (fixture["candidate_root"] / "release-notes.md").write_text(
        "changed after upload\n",
        encoding="utf-8",
    )

    with pytest.raises(candidate.CandidateError, match="digest mismatch"):
        candidate.write_preview_release_snapshot(
            tmp_path / "changed-asset-snapshot.json",
            metadata_path=fixture["metadata"],
            candidate_root=fixture["candidate_root"],
            baseline_path=fixture["snapshot"],
            expected_repository="CompLifeLab-ZJU/OpenEvo",
            expected_release_id=356072935,
            expected_tag="openevo-desktop-v0.1.0-exhibition.123.2",
            expected_source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
            expected_manifest_sha256=fixture["manifest_sha256"],
            expected_run_id=123456,
            expected_run_attempt=2,
            expected_draft=True,
        )


def test_preview_snapshot_identity_validation_accepts_exact_candidate(
    tmp_path: Path,
) -> None:
    candidate, fixture = _write_preview_release_fixture(tmp_path)

    candidate.validate_preview_release_snapshot_identity(
        fixture["snapshot"],
        expected_repository="CompLifeLab-ZJU/OpenEvo",
        expected_release_id=356072935,
        expected_tag="openevo-desktop-v0.1.0-exhibition.123.2",
        expected_source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        expected_manifest_sha256=fixture["manifest_sha256"],
        expected_run_id=123456,
        expected_run_attempt=2,
    )


@pytest.mark.parametrize(
    ("argument", "replacement"),
    [
        ("expected_repository", "CompLifeLab-ZJU/Other"),
        ("expected_release_id", 356072936),
        ("expected_tag", "openevo-desktop-v0.1.0-other.123.2"),
        ("expected_source_commit", "f" * 40),
        ("expected_manifest_sha256", "f" * 64),
        ("expected_run_id", 123457),
        ("expected_run_attempt", 3),
    ],
)
def test_preview_snapshot_identity_validation_rejects_mismatched_input(
    tmp_path: Path,
    argument: str,
    replacement: object,
) -> None:
    candidate, fixture = _write_preview_release_fixture(tmp_path)
    arguments: dict[str, object] = {
        "expected_repository": "CompLifeLab-ZJU/OpenEvo",
        "expected_release_id": 356072935,
        "expected_tag": "openevo-desktop-v0.1.0-exhibition.123.2",
        "expected_source_commit": "8e45af371eef49a86530a849041f7dcf047620ec",
        "expected_manifest_sha256": fixture["manifest_sha256"],
        "expected_run_id": 123456,
        "expected_run_attempt": 2,
    }
    arguments[argument] = replacement

    with pytest.raises(
        candidate.CandidateError,
        match="snapshot identity|workflow identity",
    ):
        candidate.validate_preview_release_snapshot_identity(
            fixture["snapshot"],
            **arguments,
        )


def test_release_inventory_rejects_wrong_numeric_release_id(tmp_path: Path) -> None:
    candidate = _load_module()
    inventory = tmp_path / "release-ids.jsonl"
    inventory.write_text(
        '{"id":356072936,"tag_name":"openevo-desktop-v0.1.0-exhibition.123.2"}\n',
        encoding="utf-8",
    )

    with pytest.raises(candidate.CandidateError, match="numeric release ID"):
        candidate.assert_release_id_inventory(
            inventory,
            expected_tag="openevo-desktop-v0.1.0-exhibition.123.2",
            expected_release_id=356072935,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("path", ".github/workflows/unrelated.yml"),
        ("head_sha", "f" * 40),
        ("run_attempt", 3),
        ("conclusion", "failure"),
    ],
)
def test_preview_publisher_rejects_wrong_candidate_workflow_run(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    candidate = _load_module()
    metadata = {
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_branch": "stable",
        "head_sha": "8e45af371eef49a86530a849041f7dcf047620ec",
        "id": 123456,
        "path": ".github/workflows/openevo-desktop-candidate.yml",
        "repository": "CompLifeLab-ZJU/OpenEvo",
        "run_attempt": 2,
        "status": "completed",
    }
    metadata[field] = replacement
    path = tmp_path / "candidate-run.json"
    _write_json(path, metadata)

    with pytest.raises(candidate.CandidateError, match="run identity or result"):
        candidate.validate_candidate_workflow_run(
            path,
            expected_repository="CompLifeLab-ZJU/OpenEvo",
            expected_run_id=123456,
            expected_run_attempt=2,
            expected_source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        )


def test_postpublication_tag_must_point_to_exact_source(tmp_path: Path) -> None:
    candidate = _load_module()
    inventory = tmp_path / "published-tag.txt"
    inventory.write_text(
        f"{'f' * 40}\trefs/tags/openevo-desktop-v0.1.0-exhibition.123.2\n",
        encoding="utf-8",
    )

    with pytest.raises(candidate.CandidateError, match="expected source commit"):
        candidate.validate_published_tag_target(
            inventory,
            expected_tag="openevo-desktop-v0.1.0-exhibition.123.2",
            expected_source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        )


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
            "Metadata-Version: 2.4\nName: openevo\nVersion: 0.1.0\nRequires-Python: >=3.11\n\n",
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
    daemon_bundle = root / "openevo-daemon-linux-x86_64"
    daemon_bundle.write_bytes(b"self-contained linux daemon")
    daemon_bundle.chmod(0o755)
    daemon_manifest = root / "openevo-daemon-bundle.json"
    _write_json(
        daemon_manifest,
        {
            "artifact": {
                "filename": daemon_bundle.name,
                "sha256": _sha256(daemon_bundle),
                "size": daemon_bundle.stat().st_size,
            },
            "build_environment_distributions": [
                {"name": "openevo", "version": "0.1.0"},
                {"name": "pyinstaller", "version": "6.0.0"},
            ],
            "core": {
                "framework_lock": {
                    "filename": framework_lock.name,
                    "sha256": _sha256(framework_lock),
                },
                "registry_digest": "a" * 64,
                "wheel": {
                    "filename": wheel.name,
                    "sha256": wheel_digest,
                    "size": wheel.stat().st_size,
                    "version": "0.1.0",
                },
            },
            "dependency_lock": {
                "filename": "uv.lock",
                "sha256": _sha256(Path("uv.lock")),
            },
            "platform": {"architecture": "x86_64", "system": "linux"},
            "release": {
                "identity": "b" * 64,
                "source_commit": "8e45af371eef49a86530a849041f7dcf047620ec",
            },
            "runtime": {
                "format": "pyinstaller-onefile",
                "python": {"implementation": "CPython", "version": "3.11.13"},
                "system_python_required": False,
                "target_pypi_required": False,
            },
            "schema_version": 1,
            "smoke": {
                "backend_readiness": "passed",
                "controlled_exit": "passed",
                "identity": "passed",
            },
        },
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
    _write_json(
        root / "launchservices-smoke.json",
        {
            "architecture": "arm64",
            "binary_sha256": mounted_smoke_evidence["binary_sha256"],
            "build_version": "0.1.0",
            "cleanup": {
                "authority_limited_to_observed_tree": True,
                "owned_processes_exited": True,
                "sidecar_descendants_exited": True,
            },
            "launch_origin": "launchservices_open_n_post_quarantine_allow",
            "os_major": 14,
            "process_image_bound": True,
            "quarantine_present_before_allow": True,
            "quarantine_removed_before_launch": True,
            "schema_version": 1,
            "sidecar_ready": True,
            "source_dmg": mounted_smoke_evidence["source_dmg"],
            "version_verified": True,
        },
    )
    candidate = _load_module()
    source_commit = "8e45af371eef49a86530a849041f7dcf047620ec"
    release_files = [
        {
            "relative_path": "core/framework-lock.json",
            "sha256": _sha256(framework_lock),
            "byte_size": framework_lock.stat().st_size,
        },
        {
            "relative_path": f"core/{wheel.name}",
            "sha256": _sha256(wheel),
            "byte_size": wheel.stat().st_size,
        },
        {
            "relative_path": f"daemon/{daemon_manifest.name}",
            "sha256": _sha256(daemon_manifest),
            "byte_size": daemon_manifest.stat().st_size,
        },
        {
            "relative_path": f"daemon/{daemon_bundle.name}",
            "sha256": _sha256(daemon_bundle),
            "byte_size": daemon_bundle.stat().st_size,
        },
        {
            "relative_path": f"runtime/{candidate.MANAGED_RUNTIME_ARCHIVE_NAME}",
            "sha256": candidate.MANAGED_RUNTIME_ARCHIVE_SHA256,
            "byte_size": candidate.MANAGED_RUNTIME_ARCHIVE_SIZE,
        },
    ]
    release_manifest = candidate._canonical_json(
        {"files": release_files, "schema_version": 1, "source_commit": source_commit}
    )
    daemon_resource = {
        "launch_origin": "mounted_dmg",
        "release_assets": {
            "files": [
                {
                    **entry,
                    "relative_path": f"{candidate.RELEASE_ASSETS_RESOURCE_ROOT}/{entry['relative_path']}",
                }
                for entry in release_files
            ],
            "manifest": {
                "byte_size": len(release_manifest),
                "relative_path": f"{candidate.RELEASE_ASSETS_RESOURCE_ROOT}/{candidate.RELEASE_ASSETS_MANIFEST_NAME}",
                "sha256": hashlib.sha256(release_manifest).hexdigest(),
            },
        },
        "schema_version": 3,
        "ssh_askpass_helper": {
            "architecture": "arm64",
            "byte_size": 51,
            "mode": "0755",
            "relative_path": "Contents/MacOS/openevo-ssh-askpass",
            "sha256": "e" * 64,
            "signature": "adhoc",
        },
        "source_dmg": {"filename": dmg.name, "sha256": _sha256(dmg)},
    }
    _write_json(root / "daemon-mounted-resource.json", daemon_resource)
    copied_daemon_resource = json.loads(json.dumps(daemon_resource))
    copied_daemon_resource["launch_origin"] = "detached_copy"
    _write_json(root / "daemon-copy-resource.json", copied_daemon_resource)
    _write_json(
        root / candidate.MANAGED_RUNTIME_SOURCE_NAME,
        candidate._managed_runtime_source_evidence(),
    )
    _write_playwright_inputs(root, retain_raw=False)
    return {
        "wheel": wheel,
        "framework_lock": framework_lock,
        "dmg": dmg,
        "daemon_bundle": daemon_bundle,
        "daemon_manifest": daemon_manifest,
    }


def _write_preview_release_fixture(
    root: Path,
) -> tuple[object, dict[str, object]]:
    candidate = _load_module()
    candidate_root = root / "candidate"
    candidate_root.mkdir()
    _write_candidate_inputs(candidate_root)
    manifest = candidate.create_candidate_manifest(
        candidate_root,
        source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        version="0.1.0",
        architecture="aarch64",
        rust_target="aarch64-apple-darwin",
        registry_digest="a" * 64,
    )
    manifest_sha256 = _sha256(manifest)
    assets = [
        {
            "digest": f"sha256:{_sha256(path)}",
            "id": 400000000 + index,
            "name": path.name,
            "size": path.stat().st_size,
            "state": "uploaded",
        }
        for index, path in enumerate(sorted(candidate_root.iterdir()), start=1)
    ]
    body = candidate.render_draft_release_body(
        release_notes=(candidate_root / "release-notes.md").read_text(encoding="utf-8"),
        ownership_token="d" * 32,
    )
    metadata = root / "draft-release-rest.json"
    _write_json(
        metadata,
        {
            "assets": assets,
            "body": body,
            "draft": True,
            "html_url": (
                "https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/"
                "untagged-7a9ca728f876fa16a90d"
            ),
            "id": 356072935,
            "immutable": False,
            "name": "OpenEvo Desktop 0.1.0 Preview",
            "prerelease": True,
            "tag_name": "openevo-desktop-v0.1.0-exhibition.123.2",
            "target_commitish": "8e45af371eef49a86530a849041f7dcf047620ec",
        },
    )
    snapshot = root / "preview-release-snapshot.json"
    candidate.write_preview_release_snapshot(
        snapshot,
        metadata_path=metadata,
        candidate_root=candidate_root,
        baseline_path=None,
        expected_repository="CompLifeLab-ZJU/OpenEvo",
        expected_release_id=356072935,
        expected_tag="openevo-desktop-v0.1.0-exhibition.123.2",
        expected_source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        expected_manifest_sha256=manifest_sha256,
        expected_run_id=123456,
        expected_run_attempt=2,
        expected_draft=True,
    )
    return candidate, {
        "candidate_root": candidate_root,
        "manifest_sha256": manifest_sha256,
        "metadata": metadata,
        "snapshot": snapshot,
    }


def _write_playwright_inputs(
    root: Path,
    *,
    retain_raw: bool = True,
) -> dict[str, Path]:
    candidate = _load_module()
    raw_report = root / "raw-playwright-report.json"
    specs: list[dict[str, object]] = []
    lines = {
        (
            "scientific-project-sample.pw.ts",
            "first-run sample is accessible, keyboard-operable, and viewport-safe",
        ): 9,
        (
            "system-recovery.pw.ts",
            "System recovery is keyboard-operable, accessible, and viewport-safe",
        ): 11,
        (
            "system-recovery.pw.ts",
            "System remains reachable at the minimum width and a constrained window height",
        ): 66,
        (
            "release-readonly.pw.ts",
            "first launch uses the release sidecar composition and keeps demo navigation non-mutating",
        ): 33,
    }
    for project, file, title in sorted(candidate.PLAYWRIGHT_REQUIRED_CASES):
        specs.append(
            {
                "column": 1,
                "file": file,
                "id": f"{project}-{len(specs)}",
                "line": lines[(file, title)],
                "ok": True,
                "tags": [],
                "tests": [
                    {
                        "annotations": [],
                        "expectedStatus": "passed",
                        "projectId": project,
                        "projectName": project,
                        "results": [{"retry": 0, "status": "passed"}],
                        "status": "expected",
                        "timeout": 30000,
                    }
                ],
                "title": title,
            }
        )
    _write_json(
        raw_report,
        {
            "config": {
                "rootDir": "/home/runner/work/private-checkout/desktop/tests/product-browser",
                "projects": [
                    {"id": project, "name": project} for project in candidate.PLAYWRIGHT_VIEWPORTS
                ],
                "webServer": {
                    "command": "npm run dev -- --host 127.0.0.1",
                    "url": "http://127.0.0.1:4174/",
                },
            },
            "errors": [],
            "stats": {
                "expected": len(specs),
                "flaky": 0,
                "skipped": 0,
                "unexpected": 0,
            },
            "suites": [
                {
                    "file": "candidate-matrix",
                    "specs": specs,
                    "title": "candidate-matrix",
                }
            ],
        },
    )
    web_manifest = root / candidate.PACKAGED_WEB_MANIFEST_NAME
    web_files = [
        {
            "path": "index.html",
            "sha256": hashlib.sha256(b"<html>OpenEvo</html>").hexdigest(),
            "byte_size": len(b"<html>OpenEvo</html>"),
        }
    ]
    build_digest = hashlib.sha256(
        json.dumps(web_files, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _write_json(
        web_manifest,
        {
            "build_digest": build_digest,
            "files": web_files,
            "schema_version": "1",
        },
    )
    evidence = root / candidate.PLAYWRIGHT_EVIDENCE_NAME
    report = root / candidate.PLAYWRIGHT_REPORT_NAME
    candidate.write_playwright_candidate_evidence(
        evidence,
        raw_report_path=raw_report,
        sanitized_report_path=report,
        packaged_web_manifest_path=web_manifest,
        source_commit="8e45af371eef49a86530a849041f7dcf047620ec",
        run_id=123456,
        run_attempt=2,
        browser_version="149.0.7827.55",
    )
    if not retain_raw:
        raw_report.unlink()
    return {
        "evidence": evidence,
        "raw_report": raw_report,
        "report": report,
        "web_manifest": web_manifest,
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
