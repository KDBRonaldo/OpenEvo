from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType

import pytest


pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="OpenSSH unavailable")


def _load_module() -> ModuleType:
    path = Path("scripts/ci/desktop_real_science_e2e_attestation.py").resolve()
    spec = importlib.util.spec_from_file_location("desktop_real_science_e2e_attestation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_evidence(path: Path, *, marker: int = 1, outcome: str = "passed") -> None:
    payload = {
        "kind": "openevo_desktop_real_science_e2e",
        "marker": marker,
        "outcome": outcome,
        "run_mode": "two_session_subscription_release",
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _key(tmp_path: Path, name: str = "release-key") -> tuple[Path, Path]:
    private = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
        check=True,
    )
    private.chmod(0o600)
    return private, private.with_suffix(".pub")


def test_sign_and_verify_release_evidence(tmp_path: Path) -> None:
    module = _load_module()
    private, public = _key(tmp_path)
    evidence = tmp_path / "evidence.json"
    signature = tmp_path / "evidence.json.sig"
    _write_evidence(evidence)

    digest = module.sign_attestation(
        evidence,
        private_key_path=private,
        public_key_path=public,
        signature_path=signature,
    )

    assert digest == hashlib.sha256(signature.read_bytes()).hexdigest()
    assert module.verify_attestation(
        evidence,
        signature_path=signature,
        public_key_path=public,
        expected_signature_sha256=digest,
    ) == digest


def test_signature_rejects_changed_evidence(tmp_path: Path) -> None:
    module = _load_module()
    private, public = _key(tmp_path)
    evidence = tmp_path / "evidence.json"
    signature = tmp_path / "evidence.json.sig"
    _write_evidence(evidence)
    module.sign_attestation(
        evidence,
        private_key_path=private,
        public_key_path=public,
        signature_path=signature,
    )
    _write_evidence(evidence, marker=2)

    with pytest.raises(module.AttestationError, match="signature is invalid"):
        module.verify_attestation(
            evidence,
            signature_path=signature,
            public_key_path=public,
        )


def test_signature_digest_is_part_of_publication_authority(tmp_path: Path) -> None:
    module = _load_module()
    private, public = _key(tmp_path)
    evidence = tmp_path / "evidence.json"
    signature = tmp_path / "evidence.json.sig"
    _write_evidence(evidence)
    module.sign_attestation(
        evidence,
        private_key_path=private,
        public_key_path=public,
        signature_path=signature,
    )

    with pytest.raises(module.AttestationError, match="signature digest mismatch"):
        module.verify_attestation(
            evidence,
            signature_path=signature,
            public_key_path=public,
            expected_signature_sha256="0" * 64,
        )


def test_signing_key_must_match_committed_public_key(tmp_path: Path) -> None:
    module = _load_module()
    private, _public = _key(tmp_path, "first-key")
    _other_private, other_public = _key(tmp_path, "second-key")
    evidence = tmp_path / "evidence.json"
    _write_evidence(evidence)

    with pytest.raises(module.AttestationError, match="does not match"):
        module.sign_attestation(
            evidence,
            private_key_path=private,
            public_key_path=other_public,
            signature_path=tmp_path / "evidence.json.sig",
        )


def test_failed_or_partial_evidence_cannot_be_signed(tmp_path: Path) -> None:
    module = _load_module()
    private, public = _key(tmp_path)
    evidence = tmp_path / "evidence.json"
    _write_evidence(evidence, outcome="failed")

    with pytest.raises(module.AttestationError, match="passed two-session"):
        module.sign_attestation(
            evidence,
            private_key_path=private,
            public_key_path=public,
            signature_path=tmp_path / "evidence.json.sig",
        )
