#!/usr/bin/env python3
"""Sign or verify bounded OpenEvo Desktop real-science release evidence."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory


IDENTITY = "openevo-desktop-real-science-e2e-v1"
NAMESPACE = IDENTITY
MAX_EVIDENCE_BYTES = 128 * 1024
MAX_PUBLIC_KEY_BYTES = 1024
MAX_SIGNATURE_BYTES = 16 * 1024


class AttestationError(RuntimeError):
    pass


def _read_regular(path: Path, *, label: str, maximum: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise AttestationError(f"{label} is unreadable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 0 < before.st_size <= maximum
    ):
        raise AttestationError(f"{label} is not a bounded regular file")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise AttestationError(f"{label} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise AttestationError(f"{label} changed while it was opened")
        content = b""
        while len(content) < opened.st_size:
            chunk = os.read(descriptor, opened.st_size - len(content))
            if not chunk:
                break
            content += chunk
        if len(content) != opened.st_size or os.read(descriptor, 1):
            raise AttestationError(f"{label} changed while it was read")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise AttestationError(f"{label} changed while it was read")
        return content
    finally:
        os.close(descriptor)


def _canonical_evidence(path: Path) -> bytes:
    content = _read_regular(path, label="evidence", maximum=MAX_EVIDENCE_BYTES)
    try:
        payload = json.loads(content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttestationError("evidence is not valid UTF-8 JSON") from exc
    canonical = (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if content != canonical:
        raise AttestationError("evidence is not canonical JSON")
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "openevo_desktop_real_science_e2e"
        or payload.get("outcome") != "passed"
        or payload.get("run_mode") != "two_task_subscription_release"
    ):
        raise AttestationError("only passed two-Task v2 release evidence can be attested")
    return content


def _public_key(path: Path) -> tuple[bytes, str]:
    content = _read_regular(path, label="public key", maximum=MAX_PUBLIC_KEY_BYTES)
    try:
        line = content.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise AttestationError("public key is not ASCII") from exc
    fields = line.split()
    if len(fields) not in (2, 3) or fields[0] != "ssh-ed25519":
        raise AttestationError("public key must contain one Ed25519 SSH key")
    try:
        decoded = base64.b64decode(fields[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttestationError("public key encoding is invalid") from exc
    if not decoded or b"ssh-ed25519" not in decoded:
        raise AttestationError("public key encoding is invalid")
    return (f"{fields[0]} {fields[1]}\n".encode("ascii"), line)


def _run(command: list[str], *, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    environment = {
        name: os.environ[name]
        for name in ("HOME", "LANG", "LC_ALL", "PATH")
        if name in os.environ
    }
    try:
        return subprocess.run(
            command,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AttestationError("OpenSSH attestation command failed to execute") from exc


def verify_attestation(
    evidence_path: Path,
    *,
    signature_path: Path,
    public_key_path: Path,
    expected_signature_sha256: str | None = None,
) -> str:
    evidence = _canonical_evidence(evidence_path)
    signature = _read_regular(
        signature_path,
        label="signature",
        maximum=MAX_SIGNATURE_BYTES,
    )
    signature_sha256 = hashlib.sha256(signature).hexdigest()
    if (
        expected_signature_sha256 is not None
        and signature_sha256 != expected_signature_sha256
    ):
        raise AttestationError("signature digest mismatch")
    _normalized_key, public_line = _public_key(public_key_path)
    with TemporaryDirectory(prefix="openevo-e2e-attestation-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        allowed_signers = root / "allowed-signers"
        allowed_signers.write_text(f"{IDENTITY} {public_line}\n", encoding="ascii")
        allowed_signers.chmod(0o600)
        stable_signature = root / "evidence.sig"
        stable_signature.write_bytes(signature)
        stable_signature.chmod(0o600)
        result = _run(
            [
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(allowed_signers),
                "-I",
                IDENTITY,
                "-n",
                NAMESPACE,
                "-s",
                str(stable_signature),
            ],
            stdin=evidence,
        )
    if result.returncode != 0:
        raise AttestationError("evidence signature is invalid")
    return signature_sha256


def sign_attestation(
    evidence_path: Path,
    *,
    private_key_path: Path,
    public_key_path: Path,
    signature_path: Path,
) -> str:
    evidence = _canonical_evidence(evidence_path)
    normalized_public, _public_line = _public_key(public_key_path)
    private = _read_regular(
        private_key_path,
        label="private key",
        maximum=16 * 1024,
    )
    private_metadata = private_key_path.stat()
    if private_metadata.st_uid != os.getuid() or stat.S_IMODE(private_metadata.st_mode) != 0o600:
        raise AttestationError("private key must be owned by the signer with mode 0600")
    derived = _run(["ssh-keygen", "-y", "-f", str(private_key_path)])
    derived_fields = derived.stdout.decode("ascii", errors="ignore").split()
    derived_public = (
        f"{derived_fields[0]} {derived_fields[1]}\n".encode("ascii")
        if len(derived_fields) >= 2
        else b""
    )
    if derived.returncode != 0 or derived_public != normalized_public:
        raise AttestationError("private key does not match the release public key")
    if not private.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
        raise AttestationError("private key format is unsupported")
    if signature_path.exists() or signature_path.is_symlink():
        raise AttestationError("signature output already exists")
    with TemporaryDirectory(prefix="openevo-e2e-signing-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        temporary_evidence = root / "evidence.json"
        temporary_evidence.write_bytes(evidence)
        temporary_evidence.chmod(0o600)
        result = _run(
            [
                "ssh-keygen",
                "-Y",
                "sign",
                "-q",
                "-f",
                str(private_key_path),
                "-n",
                NAMESPACE,
                str(temporary_evidence),
            ]
        )
        if result.returncode != 0:
            raise AttestationError("evidence signing failed")
        temporary_signature = temporary_evidence.with_suffix(".json.sig")
        signature = _read_regular(
            temporary_signature,
            label="generated signature",
            maximum=MAX_SIGNATURE_BYTES,
        )
    signature_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            signature_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise AttestationError("signature output cannot be created safely") from exc
    try:
        view = memoryview(signature)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise AttestationError("signature output write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return verify_attestation(
        evidence_path,
        signature_path=signature_path,
        public_key_path=public_key_path,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    sign = subcommands.add_parser("sign")
    sign.add_argument("evidence", type=Path)
    sign.add_argument("--private-key", required=True, type=Path)
    sign.add_argument("--public-key", required=True, type=Path)
    sign.add_argument("--signature", required=True, type=Path)
    verify = subcommands.add_parser("verify")
    verify.add_argument("evidence", type=Path)
    verify.add_argument("--signature", required=True, type=Path)
    verify.add_argument("--public-key", required=True, type=Path)
    verify.add_argument("--expected-signature-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "sign":
            digest = sign_attestation(
                args.evidence,
                private_key_path=args.private_key,
                public_key_path=args.public_key,
                signature_path=args.signature,
            )
        else:
            digest = verify_attestation(
                args.evidence,
                signature_path=args.signature,
                public_key_path=args.public_key,
                expected_signature_sha256=args.expected_signature_sha256,
            )
    except AttestationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
