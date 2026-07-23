#!/usr/bin/env python3
"""Verify the closed ad-hoc code-signing policy for an unsigned Desktop app."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


MAX_CODESIGN_OUTPUT_BYTES = 64 * 1024
_FLAGS_PATTERN = re.compile(r"\bflags=0x[0-9a-fA-F]+\(([^)]*)\)")
_FORBIDDEN_ENTITLEMENT = "com.apple.security.cs.disable-library-validation"


class CodeSignPolicyError(RuntimeError):
    """The candidate does not satisfy the unsigned ad-hoc signing policy."""


def validate_codesign_description(
    description: str,
    *,
    component: str,
) -> dict[str, object]:
    if len(description.encode("utf-8")) > MAX_CODESIGN_OUTPUT_BYTES:
        raise CodeSignPolicyError(f"{component} code-signing description is oversized")
    lines = tuple(line.strip() for line in description.splitlines() if line.strip())
    if "Signature=adhoc" not in lines:
        raise CodeSignPolicyError(f"{component} is not ad-hoc signed")
    if "TeamIdentifier=not set" not in lines:
        raise CodeSignPolicyError(f"{component} has an unexpected Team identifier")
    flags_lines = tuple(line for line in lines if line.startswith("CodeDirectory "))
    if len(flags_lines) != 1:
        raise CodeSignPolicyError(f"{component} has no unique CodeDirectory flags")
    match = _FLAGS_PATTERN.search(flags_lines[0])
    if match is None:
        raise CodeSignPolicyError(f"{component} CodeDirectory flags are malformed")
    flags = frozenset(value.strip() for value in match.group(1).split(",") if value.strip())
    if "runtime" in flags or any(line.startswith("Runtime Version=") for line in lines):
        raise CodeSignPolicyError(f"{component} unexpectedly enables hardened runtime")
    if flags != {"adhoc"}:
        if "adhoc" not in flags:
            raise CodeSignPolicyError(f"{component} CodeDirectory is not ad-hoc")
        raise CodeSignPolicyError(
            f"{component} CodeDirectory flags are not closed ad-hoc policy"
        )
    return {
        "component": component,
        "hardened_runtime": False,
        "identity": "adhoc",
        "team_identifier": None,
    }


def validate_entitlements_output(output: str, *, component: str) -> None:
    if len(output.encode("utf-8")) > MAX_CODESIGN_OUTPUT_BYTES:
        raise CodeSignPolicyError(f"{component} entitlement output is oversized")
    if _FORBIDDEN_ENTITLEMENT in output:
        raise CodeSignPolicyError(
            f"{component} has the forbidden disable-library-validation entitlement"
        )
    unexpected = tuple(
        line.strip()
        for line in output.splitlines()
        if line.strip() and not line.startswith("Executable=")
    )
    if unexpected:
        raise CodeSignPolicyError(f"{component} has unexpected entitlements")


def _run_codesign(arguments: list[str], *, component: str) -> str:
    completed = subprocess.run(
        ["/usr/bin/codesign", *arguments],
        check=False,
        capture_output=True,
    )
    output = completed.stdout + completed.stderr
    if len(output) > MAX_CODESIGN_OUTPUT_BYTES:
        raise CodeSignPolicyError(f"{component} codesign output is oversized")
    if completed.returncode != 0:
        raise CodeSignPolicyError(f"{component} codesign verification failed")
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodeSignPolicyError(f"{component} codesign output is not UTF-8") from exc


def _component_paths(app: Path) -> tuple[tuple[str, Path], ...]:
    return (
        ("app_bundle", app),
        ("native_executable", app / "Contents/MacOS/openevo-desktop"),
        ("bundled_sidecar", app / "Contents/MacOS/openevo-desktop-sidecar"),
    )


def verify_app(app: Path) -> dict[str, Any]:
    if app.is_symlink() or not app.is_dir():
        raise CodeSignPolicyError("app bundle is missing or symbolic")
    _run_codesign(
        ["--verify", "--deep", "--strict", "--verbose=2", str(app)],
        component="app_bundle",
    )
    components: list[dict[str, object]] = []
    for component, path in _component_paths(app):
        if path.is_symlink() or (component != "app_bundle" and not path.is_file()):
            raise CodeSignPolicyError(f"{component} is missing or symbolic")
        description = _run_codesign(["-d", "--verbose=4", str(path)], component=component)
        entitlements = _run_codesign(["-d", "--entitlements", "-", str(path)], component=component)
        components.append(
            validate_codesign_description(description, component=component)
        )
        validate_entitlements_output(entitlements, component=component)
    return {
        "components": components,
        "policy": {
            "disable_library_validation": False,
            "hardened_runtime": False,
            "identity": "adhoc",
        },
        "schema_version": 1,
    }


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    payload = json.dumps(
        evidence,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--evidence-out", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        evidence = verify_app(args.app)
        _write_evidence(args.evidence_out, evidence)
    except (CodeSignPolicyError, OSError) as exc:
        print(f"Desktop code-signing policy verification failed: {exc}", file=sys.stderr)
        return 1
    print("Desktop unsigned code-signing policy verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
