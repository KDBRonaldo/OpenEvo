#!/usr/bin/env python3
"""Warn when PR process expectations from agents.md are missing.

The default mode is non-blocking: warnings are printed and the process exits 0.
Pass --strict to make missing expectations fail CI once the project is ready.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ISSUE_REFERENCE_RE = re.compile(
    r"\b(?:fixes|closes|resolves|part\s+of)\s+#\d+\b",
    re.IGNORECASE,
)
NO_ISSUE_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\[[ xX]\]\s+)?no\s+issue\s+needed\s*:\s*(?P<text>\S.*)$",
    re.IGNORECASE,
)
NO_DOCS_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\[[ xX]\]\s+)?no\s+docs\s+needed\s*:\s*(?P<text>\S.*)$",
    re.IGNORECASE,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-body-file", type=Path, required=True)
    parser.add_argument("--changed-files-file", type=Path, required=True)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when expectations are missing.",
    )
    return parser.parse_args(argv)


def read_changed_files(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def has_issue_reference(body: str) -> bool:
    return bool(ISSUE_REFERENCE_RE.search(body) or _has_explained_line(body, NO_ISSUE_LINE_RE))


def has_docs_explanation(body: str) -> bool:
    return _has_explained_line(body, NO_DOCS_LINE_RE)


def _has_explained_line(body: str, pattern: re.Pattern[str]) -> bool:
    for line in body.splitlines():
        match = pattern.match(line)
        if match and _has_meaningful_text(match.group("text")):
            return True
    return False


def _has_meaningful_text(text: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", text))


def is_docs_like(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized.startswith("docs/")
        or normalized.startswith(".github/ISSUE_TEMPLATE/")
        or normalized == ".github/pull_request_template.md"
        or normalized == "agents.md"
        or name == "README.md"
        or name.endswith(".md")
        or name.endswith(".rst")
    )


def has_docs_change(paths: list[str]) -> bool:
    return any(is_docs_like(path) for path in paths)


def needs_docs_change(paths: list[str]) -> bool:
    return any(not is_docs_like(path) for path in paths)


def find_process_warnings(pr_body: str, changed_files: list[str]) -> list[str]:
    warnings: list[str] = []
    if not has_issue_reference(pr_body):
        warnings.append(
            "PR body should include Fixes/Closes/Resolves/Part of #<issue>, "
            "or explain `No issue needed:`."
        )

    if needs_docs_change(changed_files) and not (
        has_docs_change(changed_files) or has_docs_explanation(pr_body)
    ):
        warnings.append(
            "Non-documentation changes should include docs/README/agents.md updates, "
            "or explain `No docs needed:` in the PR body."
        )

    return warnings


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pr_body = args.pr_body_file.read_text(encoding="utf-8")
    changed_files = read_changed_files(args.changed_files_file)
    warnings = find_process_warnings(pr_body, changed_files)

    if not warnings:
        print("PR process checks passed.")
        return 0

    prefix = "ERROR" if args.strict else "WARNING"
    for warning in warnings:
        print(f"{prefix}: {warning}", file=sys.stderr)
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
