#!/usr/bin/env python3
"""Extract GitHub labels selected in the repository issue form."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ALLOWED_LABELS = (
    "bug",
    "documentation",
    "duplicate",
    "enhancement",
    "good first issue",
    "help wanted",
    "invalid",
    "question",
    "wontfix",
)
_LABEL_BY_NORMALIZED = {label.casefold(): label for label in ALLOWED_LABELS}
_HEADING_RE = re.compile(r"^###\s+(?P<heading>.+?)\s*$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-body-file", type=Path, required=True)
    parser.add_argument(
        "--github-output",
        type=Path,
        help="Optional path from $GITHUB_OUTPUT where labels=<json> should be appended.",
    )
    parser.add_argument(
        "--current-labels-json",
        default="[]",
        help="JSON array of current issue labels from the GitHub event payload.",
    )
    return parser.parse_args(argv)


def extract_issue_form_labels(body: str) -> list[str]:
    sections = _issue_form_sections(body)
    labels: list[str] = []
    labels.extend(_labels_from_text(sections.get("primary label", ""), first_only=True))
    labels.extend(_labels_from_text(sections.get("secondary labels", ""), first_only=False))
    return _dedupe(labels)


def labels_to_remove(current_labels: list[str], selected_labels: list[str]) -> list[str]:
    selected = {label.casefold() for label in selected_labels}
    remove: list[str] = []
    for label in current_labels:
        canonical = _LABEL_BY_NORMALIZED.get(label.casefold())
        if canonical is not None and canonical.casefold() not in selected:
            remove.append(canonical)
    return _dedupe(remove)


def build_label_update_plan(
    *, issue_body: str, current_labels: list[Any] | None = None
) -> dict[str, list[str]]:
    labels_to_add = extract_issue_form_labels(issue_body)
    return {
        "labels_to_add": labels_to_add,
        "labels_to_remove": labels_to_remove(
            _label_names(current_labels or []), labels_to_add
        ),
    }


def _label_names(labels: list[Any]) -> list[str]:
    names: list[str] = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
    return names


def _parse_current_labels_json(value: str) -> list[Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        msg = "--current-labels-json must be a JSON array"
        raise ValueError(msg)
    return parsed


def _issue_form_sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            current = _normalize_heading(match.group("heading"))
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {heading: "\n".join(lines).strip() for heading, lines in sections.items()}


def _normalize_heading(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _labels_from_text(value: str, *, first_only: bool) -> list[str]:
    labels: list[str] = []
    for raw in re.split(r"[,;\n]+", value):
        normalized = raw.strip().strip("`").lstrip("-* ").casefold()
        label = _LABEL_BY_NORMALIZED.get(normalized)
        if label:
            labels.append(label)
            if first_only:
                break
    return labels


def _dedupe(labels: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for label in labels:
        if label in seen:
            continue
        seen.add(label)
        deduped.append(label)
    return deduped


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    plan = build_label_update_plan(
        issue_body=args.issue_body_file.read_text(encoding="utf-8"),
        current_labels=_parse_current_labels_json(args.current_labels_json),
    )
    labels_json = json.dumps(plan["labels_to_add"])
    labels_to_remove_json = json.dumps(plan["labels_to_remove"])
    print(json.dumps(plan))
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"labels={labels_json}\n")
            output.write(f"labels_to_add={labels_json}\n")
            output.write(f"labels_to_remove={labels_to_remove_json}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
