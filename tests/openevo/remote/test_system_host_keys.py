from __future__ import annotations

from pathlib import Path
import os

import pytest

from openevo.deployment.host_keys import (
    SystemHostKeyFailureCode,
    SystemHostKeyReviewAuthority,
    classify_system_openssh_host_key_failure,
    inspect_system_known_hosts_policy,
)
from openevo.deployment.profile import SystemOpenSshAliasProfile
from openevo.deployment.ssh import build_system_ssh_keygen_remove_argv
from openevo.deployment.system_executables import SSH_KEYGEN_EXECUTABLE


_FINGERPRINT = "SHA256:" + ("A" * 43)
_DEFAULT_USER_FILES = object()


def _profile(alias: str = "evolab") -> SystemOpenSshAliasProfile:
    return SystemOpenSshAliasProfile(
        profile_id="profile-1",
        ssh_host_alias=alias,
    )


def _changed_key_stderr(path: Path, *, fingerprint: str = _FINGERPRINT) -> bytes:
    return (
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
        "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
        "The fingerprint for the ED25519 key sent by the remote host is\n"
        f"{fingerprint}.\n"
        f"Offending ED25519 key in {path}:7\n"
        "Host key verification failed.\n"
    ).encode()


def _config(
    known_hosts: Path | None,
    *,
    hostname: str = "gpu.internal.example",
    port: int = 22,
    user_files: str | None | object = _DEFAULT_USER_FILES,
    hash_known_hosts: str = "no",
    extra: tuple[str, ...] = (),
) -> bytes:
    lines = [
        f"hostname {hostname}",
        f"port {port}",
        "canonicalizehostname false",
        f"hashknownhosts {hash_known_hosts}",
        "stricthostkeychecking ask",
        "globalknownhostsfile /etc/ssh/ssh_known_hosts /etc/ssh/ssh_known_hosts2",
    ]
    if isinstance(user_files, str):
        lines.append(f"userknownhostsfile {user_files}")
    elif user_files is _DEFAULT_USER_FILES and known_hosts is not None:
        lines.append(f"userknownhostsfile {known_hosts}")
    lines.extend(extra)
    return ("\n".join(lines) + "\n").encode()


def _known_hosts(tmp_path: Path) -> Path:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    path = ssh_dir / "known_hosts"
    path.write_text("gpu.internal.example ssh-ed25519 AAAATEST\n", encoding="ascii")
    path.chmod(0o600)
    return path


def test_changed_key_failure_retains_only_bounded_fingerprint_evidence(
    tmp_path: Path,
) -> None:
    path = _known_hosts(tmp_path)
    evidence = classify_system_openssh_host_key_failure(_changed_key_stderr(path))

    assert evidence is not None
    assert evidence.code is SystemHostKeyFailureCode.CHANGED
    assert evidence.presented_fingerprints == (("ssh-ed25519", _FINGERPRINT),)
    assert evidence.offending_known_hosts_file == path
    assert evidence.offending_line == 7
    assert str(path) not in repr(evidence)
    assert "gpu.internal.example" not in repr(evidence)


@pytest.mark.parametrize(
    "stderr",
    [
        b"No ED25519 host key is known and you have requested strict checking.\n"
        b"Host key verification failed.\n",
        b"No ECDSA host key is known for server and you have requested strict checking.\n"
        b"Host key verification failed.\n",
    ],
)
def test_first_use_forbidden_is_distinct_from_changed_key(stderr: bytes) -> None:
    evidence = classify_system_openssh_host_key_failure(stderr)

    assert evidence is not None
    assert evidence.code is SystemHostKeyFailureCode.FIRST_USE_FORBIDDEN
    assert evidence.presented_fingerprints == ()
    assert evidence.offending_known_hosts_file is None


@pytest.mark.parametrize(
    "stderr",
    [
        b"Host key verification failed.\n",
        _changed_key_stderr(Path("/Users/private/.ssh/known_hosts"), fingerprint="SHA256:short"),
        _changed_key_stderr(Path("/Users/private/.ssh/known_hosts")).replace(
            b"Offending ED25519", b"Offending UNKNOWN"
        ),
        b"x" * ((64 << 10) + 1),
        b"Host key verification failed.\xff\n",
    ],
)
def test_malformed_or_unproven_host_key_output_fails_closed(stderr: bytes) -> None:
    evidence = classify_system_openssh_host_key_failure(stderr)

    assert evidence is not None
    assert evidence.code is SystemHostKeyFailureCode.VERIFICATION_FAILED
    assert evidence.presented_fingerprints == ()
    assert evidence.offending_known_hosts_file is None
    assert "/Users/private" not in repr(evidence)


def test_simple_owned_user_known_hosts_policy_is_automatically_repairable(
    tmp_path: Path,
) -> None:
    path = _known_hosts(tmp_path)
    policy = inspect_system_known_hosts_policy(
        _config(path, port=2207),
        home=tmp_path,
        offending_known_hosts_file=path,
    )

    assert policy.repair_support == "automatic_replacement_available"
    assert policy.reason == "simple_user_known_hosts"
    assert policy.known_hosts_file == path
    assert policy.lookup_token == "[gpu.internal.example]:2207"
    assert str(path) not in repr(policy)
    assert policy.lookup_token not in repr(policy)


@pytest.mark.parametrize(
    ("config_change", "reason"),
    [
        ({"user_files": "{path} {path2}"}, "multiple_user_known_hosts_files"),
        ({"user_files": None}, "no_user_known_hosts_file"),
        ({"extra": ("knownhostscommand /usr/local/bin/trust-helper",)}, "known_hosts_command"),
        ({"extra": ("hostkeyalias shared-host",)}, "host_key_alias"),
        ({"hash_known_hosts": "yes"}, "unsupported_hash_policy"),
        ({"hostname": "host name"}, "unsupported_lookup_token"),
    ],
)
def test_ambiguous_effective_trust_policy_requires_administrator_action(
    tmp_path: Path,
    config_change: dict[str, object],
    reason: str,
) -> None:
    path = _known_hosts(tmp_path)
    path2 = path.with_name("known_hosts2")
    path2.write_text("other ssh-ed25519 AAAATEST\n", encoding="ascii")
    path2.chmod(0o600)
    values = {
        key: (value.format(path=path, path2=path2) if isinstance(value, str) else value)
        for key, value in config_change.items()
    }
    policy = inspect_system_known_hosts_policy(
        _config(path, **values),  # type: ignore[arg-type]
        home=tmp_path,
        offending_known_hosts_file=path,
    )

    assert policy.repair_support == "administrator_required"
    assert policy.reason == reason
    assert policy.known_hosts_file is None
    assert policy.lookup_token is None


def test_conditional_config_and_changed_key_source_mismatch_fail_closed(
    tmp_path: Path,
) -> None:
    path = _known_hosts(tmp_path)
    other = path.with_name("other_known_hosts")
    other.write_text("gpu.internal.example ssh-ed25519 AAAATEST\n", encoding="ascii")
    other.chmod(0o600)

    conditional = inspect_system_known_hosts_policy(
        _config(path),
        home=tmp_path,
        offending_known_hosts_file=path,
        conditional_config=True,
    )
    mismatch = inspect_system_known_hosts_policy(
        _config(path),
        home=tmp_path,
        offending_known_hosts_file=other,
    )

    assert conditional.reason == "conditional_config"
    assert mismatch.reason == "changed_key_source_mismatch"
    assert conditional.repair_support == mismatch.repair_support == "administrator_required"


@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "world_writable"])
def test_unsafe_known_hosts_file_is_never_automatically_modified(
    tmp_path: Path,
    mutation: str,
) -> None:
    path = _known_hosts(tmp_path)
    if mutation == "symlink":
        target = path.with_name("target")
        path.rename(target)
        path.symlink_to(target)
    elif mutation == "hardlink":
        os.link(path, path.with_name("second-link"))
    else:
        path.chmod(0o666)

    policy = inspect_system_known_hosts_policy(
        _config(path),
        home=tmp_path,
        offending_known_hosts_file=path,
    )

    assert policy.repair_support == "administrator_required"
    assert policy.reason == "unsafe_user_known_hosts_file"


def test_changed_key_review_is_digest_bound_single_use_and_path_free(
    tmp_path: Path,
) -> None:
    path = _known_hosts(tmp_path)
    evidence = classify_system_openssh_host_key_failure(_changed_key_stderr(path))
    assert evidence is not None
    policy = inspect_system_known_hosts_policy(
        _config(path),
        home=tmp_path,
        offending_known_hosts_file=path,
    )
    authority = SystemHostKeyReviewAuthority(hmac_key=b"k" * 32)
    review = authority.issue(
        _profile(),
        connection_generation=9,
        evidence=evidence,
        policy=policy,
    )

    assert review.repair_support == "automatic_replacement_available"
    assert review.key_fingerprints == (("ssh-ed25519", _FINGERPRINT),)
    assert str(path) not in repr(review)
    replacement = authority.claim_replacement(
        review,
        profile=_profile(),
        connection_generation=9,
        review_id=review.review_id,
        review_sha256=review.review_sha256,
    )
    assert replacement.known_hosts_file == path
    assert replacement.lookup_token == "gpu.internal.example"
    assert str(path) not in repr(replacement)

    with pytest.raises(ValueError, match="no longer current"):
        authority.claim_replacement(
            review,
            profile=_profile(),
            connection_generation=9,
            review_id=review.review_id,
            review_sha256=review.review_sha256,
        )


def test_changed_key_review_rejects_forgery_generation_and_ambiguous_policy(
    tmp_path: Path,
) -> None:
    path = _known_hosts(tmp_path)
    evidence = classify_system_openssh_host_key_failure(_changed_key_stderr(path))
    assert evidence is not None
    repairable = inspect_system_known_hosts_policy(
        _config(path), home=tmp_path, offending_known_hosts_file=path
    )
    authority = SystemHostKeyReviewAuthority(hmac_key=b"r" * 32)
    review = authority.issue(
        _profile(), connection_generation=2, evidence=evidence, policy=repairable
    )

    with pytest.raises(ValueError, match="review identity"):
        authority.claim_replacement(
            review,
            profile=_profile(),
            connection_generation=2,
            review_id=review.review_id,
            review_sha256="f" * 64,
        )
    with pytest.raises(ValueError, match="generation"):
        authority.claim_replacement(
            review,
            profile=_profile(),
            connection_generation=3,
            review_id=review.review_id,
            review_sha256=review.review_sha256,
        )

    ambiguous = inspect_system_known_hosts_policy(
        _config(path, user_files=f"{path} {path}"),
        home=tmp_path,
        offending_known_hosts_file=path,
    )
    blocked = authority.issue(
        _profile(), connection_generation=3, evidence=evidence, policy=ambiguous
    )
    with pytest.raises(ValueError, match="administrator"):
        authority.claim_replacement(
            blocked,
            profile=_profile(),
            connection_generation=3,
            review_id=blocked.review_id,
            review_sha256=blocked.review_sha256,
        )


def test_keygen_remove_builder_is_exact_and_rejects_option_or_path_injection(
    tmp_path: Path,
) -> None:
    path = _known_hosts(tmp_path)

    assert build_system_ssh_keygen_remove_argv(
        lookup_token="[gpu.internal.example]:2207",
        known_hosts_file=path,
    ) == [
        SSH_KEYGEN_EXECUTABLE,
        "-R",
        "[gpu.internal.example]:2207",
        "-f",
        str(path),
    ]

    for token in ("-F", "host name", "host,other", "host\nother", ""):
        with pytest.raises(ValueError):
            build_system_ssh_keygen_remove_argv(
                lookup_token=token,
                known_hosts_file=path,
            )
    for invalid_path in (Path("relative"), path.with_name("bad\npath")):
        with pytest.raises(ValueError):
            build_system_ssh_keygen_remove_argv(
                lookup_token="gpu.internal.example",
                known_hosts_file=invalid_path,
            )
