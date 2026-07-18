from __future__ import annotations

from pathlib import Path
import dataclasses
import importlib.util

import pytest

from openevo.runtime import managed
from openevo.runtime.managed import (
    MANAGED_RUNTIME_ARCHIVE_RELEASE,
    MANAGED_RUNTIME_RELEASES,
    ManagedRuntimeArchiveVerificationError,
    verify_managed_runtime_archive,
    verified_managed_runtime_image_reference,
)
from tests.managed_runtime_testkit import (
    RUNTIME_ASSET_TAG,
    RUNTIME_ASSET_ID,
    RUNTIME_FILENAME,
    RUNTIME_RELEASE_ID,
    write_test_managed_runtime_archive,
)


def test_release_contract_binds_actual_archive_config_authority() -> None:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE

    assert release.asset_release_tag == RUNTIME_ASSET_TAG
    assert release.asset_release_id == RUNTIME_RELEASE_ID
    assert release.asset_id == RUNTIME_ASSET_ID
    assert release.filename == RUNTIME_FILENAME
    assert release.byte_size == 352_236_726
    assert release.sha256 == "ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149"
    assert release.asset_api_digest == "sha256:" + release.sha256
    assert (
        release.config_id
        == "sha256:0e5783e7839fe06d2df14d7a431c90f0982ca2099ef33bfa4c9e5933149bf5f2"
    )
    assert release.oci_index_id == (
        "sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b"
    )
    assert release.aliases == ("openevo/science-runtime:0.1.1",)
    assert {item.loaded_image_id for item in MANAGED_RUNTIME_RELEASES.values()} == {
        release.oci_index_id
    }


def test_offline_image_authority_is_distinct_from_registry_authority() -> None:
    release = MANAGED_RUNTIME_RELEASES["managed_science"]
    labels = {"io.openevo.managed-runtime": "true"}

    assert (
        verified_managed_runtime_image_reference(
            profile="managed_science",
            image=release.image,
            image_id=release.loaded_image_id,
            repo_digests=[],
            labels=labels,
        )
        == release.loaded_image_id
    )
    for image in (release.image, release.loaded_image_id):
        for repository in (release.repository, f"docker.io/{release.repository}"):
            assert (
                verified_managed_runtime_image_reference(
                    profile="managed_science",
                    image=image,
                    image_id=release.loaded_image_id,
                    repo_digests=[f"{repository}@{release.loaded_image_id}"],
                    labels=labels,
                )
                == release.loaded_image_id
            )
    with pytest.raises(ValueError, match="digest mismatch"):
        verified_managed_runtime_image_reference(
            profile="managed_science",
            image=release.image,
            image_id="sha256:" + "f" * 64,
            repo_digests=[],
            labels=labels,
        )
    with pytest.raises(ValueError, match="registry authority"):
        verified_managed_runtime_image_reference(
            profile="managed_science",
            image=release.loaded_image_id,
            image_id=release.loaded_image_id,
            repo_digests=[release.immutable_reference],
            labels=labels,
        )
    with pytest.raises(ValueError):
        verified_managed_runtime_image_reference(
            profile="python_research",
            image=release.loaded_image_id,
            image_id=release.loaded_image_id,
            repo_digests=[],
            labels=labels,
        )


@pytest.mark.parametrize(
    "image",
    [
        MANAGED_RUNTIME_RELEASES["managed_science"].image,
        MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id,
    ],
)
@pytest.mark.parametrize(
    "repo_digests",
    [
        ["other/science-runtime@" + MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id],
        [
            MANAGED_RUNTIME_RELEASES["managed_science"].repository
            + "@"
            + MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
        ],
        [
            MANAGED_RUNTIME_RELEASES["managed_science"].repository
            + "@"
            + MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id,
            "other/science-runtime@" + MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id,
        ],
        [
            MANAGED_RUNTIME_RELEASES["managed_science"].repository
            + "@"
            + MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id,
        ]
        * 2,
    ],
)
def test_offline_image_authority_rejects_unrelated_or_ambiguous_repository_digests(
    image: str,
    repo_digests: list[str],
) -> None:
    release = MANAGED_RUNTIME_RELEASES["managed_science"]

    with pytest.raises(ValueError):
        verified_managed_runtime_image_reference(
            profile="managed_science",
            image=image,
            image_id=release.loaded_image_id,
            repo_digests=repo_digests,
            labels={"io.openevo.managed-runtime": "true"},
        )


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("os_name", "darwin"),
        ("architecture", "arm64"),
        ("managed_label", None),
        ("managed_label", "false"),
        ("config_reference_digest", "4" * 64),
    ],
)
def test_structured_archive_verifier_rejects_config_authority_mismatch(
    tmp_path: Path,
    mutation: str,
    value: str | None,
) -> None:
    archive = tmp_path / RUNTIME_FILENAME
    release = write_test_managed_runtime_archive(archive, **{mutation: value})

    with pytest.raises(ManagedRuntimeArchiveVerificationError):
        verify_managed_runtime_archive(archive, release=release)


@pytest.mark.parametrize("repo_tags", [(), None])
def test_structured_archive_verifier_accepts_exact_manifest_config_label_and_no_tags(
    tmp_path: Path,
    repo_tags: tuple[str, ...] | None,
) -> None:
    archive = tmp_path / RUNTIME_FILENAME
    release = write_test_managed_runtime_archive(archive, repo_tags=repo_tags)

    authority = verify_managed_runtime_archive(archive, release=release)

    assert authority.config_id == release.config_id
    assert authority.oci_index_id == release.oci_index_id
    assert authority.platform == "linux-amd64"
    assert authority.managed_label is True


def test_structured_archive_verifier_rejects_load_time_alias_publication(
    tmp_path: Path,
) -> None:
    archive = tmp_path / RUNTIME_FILENAME
    release = write_test_managed_runtime_archive(
        archive,
        repo_tags=(MANAGED_RUNTIME_ARCHIVE_RELEASE.aliases[0],),
    )

    with pytest.raises(ManagedRuntimeArchiveVerificationError):
        verify_managed_runtime_archive(archive, release=release)


def test_structured_archive_verifier_rejects_oci_index_alias_publication(
    tmp_path: Path,
) -> None:
    archive = tmp_path / RUNTIME_FILENAME
    release = write_test_managed_runtime_archive(
        archive,
        index_reference="docker.io/openevo/science-runtime:0.1.1",
    )

    with pytest.raises(ManagedRuntimeArchiveVerificationError):
        verify_managed_runtime_archive(archive, release=release)


def test_structured_archive_verifier_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    archive = tmp_path / RUNTIME_FILENAME
    release = write_test_managed_runtime_archive(
        archive,
        duplicate_root_index_key=True,
    )

    with pytest.raises(ManagedRuntimeArchiveVerificationError):
        verify_managed_runtime_archive(archive, release=release)


def test_structured_archive_verifier_rejects_nested_manifest_alias_annotations(
    tmp_path: Path,
) -> None:
    archive = tmp_path / RUNTIME_FILENAME
    release = write_test_managed_runtime_archive(
        archive,
        manifest_reference="docker.io/openevo/science-runtime:0.1.1",
    )

    with pytest.raises(ManagedRuntimeArchiveVerificationError):
        verify_managed_runtime_archive(archive, release=release)


def test_archive_rebuilder_removes_all_non_runtime_descriptor_graphs(tmp_path: Path) -> None:
    source = tmp_path / ("source-" + RUNTIME_FILENAME)
    release = write_test_managed_runtime_archive(
        source,
        index_reference="docker.io/openevo/science-runtime:0.1.1",
        manifest_reference="docker.io/openevo/science-runtime:0.1.1",
        include_attestation=True,
    )
    with pytest.raises(ManagedRuntimeArchiveVerificationError):
        verify_managed_runtime_archive(source, release=release)
    destination = tmp_path / RUNTIME_FILENAME
    script = Path(__file__).resolve().parents[2] / "scripts/ci/rebuild_managed_runtime_archive.py"
    spec = importlib.util.spec_from_file_location("rebuild_managed_runtime_archive", script)
    assert spec is not None and spec.loader is not None
    rebuilder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rebuilder)

    identity = rebuilder.rebuild_archive(source, destination)
    rebuilt = dataclasses.replace(
        release,
        filename=destination.name,
        sha256=identity.archive_sha256,
        asset_api_digest="sha256:" + identity.archive_sha256,
        byte_size=identity.archive_size,
        oci_index_id=identity.oci_index_id,
    )

    authority = verify_managed_runtime_archive(destination, release=rebuilt)
    assert authority.config_id == release.config_id
    assert authority.oci_index_id == identity.oci_index_id
    assert identity.retained_blob_count == 4


def test_structured_archive_verifier_rejects_late_same_inode_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / RUNTIME_FILENAME
    release = write_test_managed_runtime_archive(archive)

    def mutate_after_structure(_descriptor: int) -> None:
        with archive.open("r+b") as stream:
            original = stream.read(1)
            stream.seek(0)
            stream.write(bytes((original[0] ^ 0x01,)))

    monkeypatch.setattr(
        managed,
        "_after_managed_runtime_archive_structure",
        mutate_after_structure,
    )

    with pytest.raises(ManagedRuntimeArchiveVerificationError):
        verify_managed_runtime_archive(archive, release=release)
