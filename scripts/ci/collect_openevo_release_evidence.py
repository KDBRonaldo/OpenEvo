#!/usr/bin/env python3
"""Collect minimal dependency, license, and vulnerability release evidence."""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


class EvidenceError(RuntimeError):
    pass


_HASH_OPTION = re.compile(
    r"^    --hash=sha256:(?P<digest>[0-9a-fA-F]{64})(?P<continued> \\)?$"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"audit report is unreadable: {path}") from exc


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _npm_vulnerabilities(report: Any) -> int:
    try:
        vulnerabilities = report["metadata"]["vulnerabilities"]
        high = vulnerabilities["high"]
        critical = vulnerabilities["critical"]
    except (KeyError, TypeError) as exc:
        raise EvidenceError("npm audit report does not contain vulnerability totals") from exc
    if type(high) is not int or type(critical) is not int or high < 0 or critical < 0:
        raise EvidenceError("npm audit vulnerability totals are invalid")
    return high + critical


def _exported_requirements(payload: str) -> dict[str, str]:
    requirements: dict[str, str] = {}
    seen_packages: set[str] = set()
    seen_hashes: set[str] = set()
    lines = payload.splitlines()
    if "\r" in payload:
        raise EvidenceError("exported Python requirements use invalid line endings")
    index = 0
    while index < len(lines):
        requirement_line = lines[index]
        index += 1
        if not requirement_line:
            continue
        if (
            requirement_line != requirement_line.strip()
            or not requirement_line.endswith(" \\")
        ):
            raise EvidenceError("exported Python requirement block is invalid")
        requirement_text = requirement_line[:-2]
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exc:
            raise EvidenceError("exported Python requirement is invalid") from exc
        name = canonicalize_name(requirement.name)
        if name == "openevo":
            raise EvidenceError("exported Python requirements must exclude the OpenEvo project")
        specifiers = list(requirement.specifier)
        if requirement.url is not None or requirement.extras or len(specifiers) != 1:
            raise EvidenceError("exported Python requirement must be one exact third-party pin")
        specifier = specifiers[0]
        if specifier.operator != "==" or specifier.version.endswith(".*"):
            raise EvidenceError("exported Python requirement must use one exact version")
        if name in seen_packages:
            raise EvidenceError("exported Python requirements contain a duplicate package")
        seen_packages.add(name)

        hashes: set[str] = set()
        while True:
            if index >= len(lines):
                raise EvidenceError("exported Python requirement hash block is incomplete")
            hash_line = lines[index]
            index += 1
            match = _HASH_OPTION.fullmatch(hash_line)
            if match is None:
                raise EvidenceError("exported Python requirement contains an invalid option")
            digest = match.group("digest").lower()
            if digest in hashes or digest in seen_hashes:
                raise EvidenceError("exported Python requirements contain a duplicate hash")
            hashes.add(digest)
            seen_hashes.add(digest)
            if match.group("continued") is None:
                break
        if not hashes:
            raise EvidenceError("exported Python requirement has no SHA-256 hash")
        if requirement.marker is None or requirement.marker.evaluate():
            requirements[name] = specifier.version
    if not requirements:
        raise EvidenceError("exported Python requirements contain no third-party packages")
    return requirements


def _pip_vulnerabilities(report: Any, requirements_payload: str) -> tuple[int, int]:
    if type(report) is not dict or set(report) != {"dependencies", "fixes"}:
        raise EvidenceError("pip-audit report does not use the expected JSON schema")
    if report.get("fixes") != []:
        raise EvidenceError("pip-audit report unexpectedly contains fix mutations")
    dependencies = report.get("dependencies")
    if type(dependencies) is not list:
        raise EvidenceError("pip-audit report does not contain a dependency list")
    expected = _exported_requirements(requirements_payload)
    observed: dict[str, str] = {}
    count = 0
    for dependency in dependencies:
        if type(dependency) is not dict or set(dependency) != {"name", "version", "vulns"}:
            raise EvidenceError("pip-audit dependency entry is invalid")
        name = dependency.get("name")
        version = dependency.get("version")
        vulnerabilities = dependency.get("vulns")
        if type(name) is not str or type(version) is not str or type(vulnerabilities) is not list:
            raise EvidenceError("pip-audit dependency entry is invalid")
        canonical_name = canonicalize_name(name)
        if canonical_name == "openevo" or canonical_name in observed:
            raise EvidenceError("pip-audit report is not a unique third-party dependency report")
        if any(type(vulnerability) is not dict for vulnerability in vulnerabilities):
            raise EvidenceError("pip-audit vulnerability entry is invalid")
        observed[canonical_name] = version
        count += len(vulnerabilities)
    if observed != expected:
        raise EvidenceError("pip-audit report does not cover the exact exported dependency set")
    return count, len(observed)


def _cargo_vulnerabilities(report: Any) -> int:
    try:
        advisory_count = report["database"]["advisory-count"]
        dependency_count = report["lockfile"]["dependency-count"]
        ignored = report["settings"]["ignore"]
        found = report["vulnerabilities"]["found"]
        count = report["vulnerabilities"]["count"]
        vulnerabilities = report["vulnerabilities"]["list"]
    except (KeyError, TypeError) as exc:
        raise EvidenceError("cargo-audit report does not contain authoritative totals") from exc
    if (
        type(advisory_count) is not int
        or advisory_count < 1
        or type(dependency_count) is not int
        or dependency_count < 1
    ):
        raise EvidenceError("cargo-audit database or lockfile inventory is invalid")
    if ignored != []:
        raise EvidenceError("cargo-audit report contains ignored advisories")
    if (
        type(found) is not bool
        or type(count) is not int
        or count < 0
        or type(vulnerabilities) is not list
        or any(type(vulnerability) is not dict for vulnerability in vulnerabilities)
        or count != len(vulnerabilities)
        or found is not (count > 0)
    ):
        raise EvidenceError("cargo-audit vulnerability status is invalid")
    return count


def _python_license_inventory() -> tuple[int, int]:
    packages = 0
    unresolved = 0
    seen: set[tuple[str, str]] = set()
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        version = distribution.version
        if not name or name.lower() == "openevo" or (name.lower(), version) in seen:
            continue
        seen.add((name.lower(), version))
        packages += 1
        license_value = distribution.metadata.get("License-Expression") or distribution.metadata.get(
            "License"
        )
        classifiers = distribution.metadata.get_all("Classifier", [])
        has_classifier = any(value.startswith("License ::") for value in classifiers)
        if not (
            isinstance(license_value, str)
            and license_value.strip()
            and license_value.strip().upper() != "UNKNOWN"
        ) and not has_classifier:
            unresolved += 1
    return packages, unresolved


def _npm_license_inventory(package_lock: Path) -> tuple[int, int]:
    payload = _load_json(package_lock)
    packages = payload.get("packages") if type(payload) is dict else None
    if type(packages) is not dict:
        raise EvidenceError("package-lock.json does not contain package metadata")
    dependencies = [entry for path, entry in packages.items() if path]
    unresolved = sum(
        1
        for entry in dependencies
        if type(entry) is not dict
        or type(entry.get("license")) is not str
        or not entry["license"].strip()
    )
    return len(dependencies), unresolved


def _cargo_metadata(repo: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "cargo",
            "metadata",
            "--locked",
            "--format-version",
            "1",
            "--manifest-path",
            str(repo / "desktop/src-tauri/Cargo.toml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    packages = payload.get("packages")
    if type(packages) is not list:
        raise EvidenceError("cargo metadata does not contain packages")
    return [
        package
        for package in packages
        if type(package) is dict and package.get("name") != "openevo-desktop"
    ]


def _cargo_license_inventory(packages: list[dict[str, Any]]) -> tuple[int, int]:
    unresolved = sum(
        1
        for package in packages
        if not (
            isinstance(package.get("license"), str)
            and package["license"].strip()
            or isinstance(package.get("license_file"), str)
            and package["license_file"].strip()
        )
    )
    return len(packages), unresolved


def collect_evidence(
    repo: Path,
    output_dir: Path,
    *,
    npm_audit: Path,
    pip_audit: Path,
    pip_requirements: Path,
    cargo_audit: Path,
) -> None:
    repo = repo.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    lockfiles = {
        "python": repo / "uv.lock",
        "npm": repo / "desktop/package-lock.json",
        "cargo": repo / "desktop/src-tauri/Cargo.lock",
    }
    if any(not path.is_file() for path in (*lockfiles.values(), repo / "LICENSE")):
        raise EvidenceError("release lockfile or project license is missing")
    uv_payload = tomllib.loads(lockfiles["python"].read_text(encoding="utf-8"))
    python_packages = uv_payload.get("package")
    if type(python_packages) is not list:
        raise EvidenceError("uv.lock does not contain packages")
    npm_payload = _load_json(lockfiles["npm"])
    npm_packages = npm_payload.get("packages") if type(npm_payload) is dict else None
    if type(npm_packages) is not dict:
        raise EvidenceError("package-lock.json does not contain packages")
    cargo_packages = _cargo_metadata(repo)
    try:
        requirements_payload = pip_requirements.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceError("exported Python requirements are unreadable") from exc
    pip_vulnerabilities, pip_packages = _pip_vulnerabilities(
        _load_json(pip_audit),
        requirements_payload,
    )
    dependency_counts = {
        "python": pip_packages,
        "npm": sum(1 for path in npm_packages if path),
        "cargo": len(cargo_packages),
    }
    dependency_inventory = {
        "ecosystems": {
            ecosystem: {
                "lockfile_sha256": _sha256(lockfiles[ecosystem]),
                "packages": dependency_counts[ecosystem],
            }
            for ecosystem in ("python", "npm", "cargo")
        },
        "schema_version": 2,
    }

    python_licenses = _python_license_inventory()
    npm_licenses = _npm_license_inventory(lockfiles["npm"])
    cargo_licenses = _cargo_license_inventory(cargo_packages)
    license_inventory = {
        "ecosystems": {
            ecosystem: {"packages": values[0], "unresolved": values[1]}
            for ecosystem, values in (
                ("python", python_licenses),
                ("npm", npm_licenses),
                ("cargo", cargo_licenses),
            )
        },
        "project_license_sha256": _sha256(repo / "LICENSE"),
        "schema_version": 1,
    }

    vulnerability_counts = {
        "npm-audit-high": _npm_vulnerabilities(_load_json(npm_audit)),
        "pip-audit": pip_vulnerabilities,
        "cargo-audit": _cargo_vulnerabilities(_load_json(cargo_audit)),
    }
    security_audit = {
        "audits": {
            audit: {
                "status": "passed" if count == 0 else "failed",
                "vulnerabilities": count,
            }
            for audit, count in vulnerability_counts.items()
        },
        "schema_version": 2,
    }
    security_audit["audits"]["pip-audit"].update(
        {
            "audited_packages": pip_packages,
            "requirements_sha256": _sha256(pip_requirements),
        }
    )
    _write_json(output_dir / "dependency-inventory.json", dependency_inventory)
    _write_json(output_dir / "license-inventory.json", license_inventory)
    _write_json(output_dir / "security-audit.json", security_audit)
    if any(count != 0 for count in vulnerability_counts.values()):
        raise EvidenceError("release security audit found actionable vulnerabilities")
    if any(values[1] != 0 for values in (python_licenses, npm_licenses, cargo_licenses)):
        raise EvidenceError("release license inventory has unresolved packages")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--npm-audit", type=Path, required=True)
    parser.add_argument("--pip-audit", type=Path, required=True)
    parser.add_argument("--pip-requirements", type=Path, required=True)
    parser.add_argument("--cargo-audit", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        collect_evidence(
            args.repo,
            args.output_dir,
            npm_audit=args.npm_audit,
            pip_audit=args.pip_audit,
            pip_requirements=args.pip_requirements,
            cargo_audit=args.cargo_audit,
        )
    except (EvidenceError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
