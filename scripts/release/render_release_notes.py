from __future__ import annotations

import argparse
from pathlib import Path


VERSION_TOKEN = "{{VERSION}}"
CHANGELOG_TOKEN = "{{CHANGELOG}}"


def render_release_notes(*, template: str, version: str, changelog: str) -> str:
    if template.count(VERSION_TOKEN) == 0:
        raise ValueError(f"release template is missing {VERSION_TOKEN}")
    if template.count(CHANGELOG_TOKEN) != 1:
        raise ValueError(f"release template must contain exactly one {CHANGELOG_TOKEN}")
    if not version.strip():
        raise ValueError("release version must not be empty")

    rendered = template.replace(VERSION_TOKEN, version.strip())
    rendered = rendered.replace(CHANGELOG_TOKEN, changelog.strip() or "No generated changes.")
    return rendered.rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render EvoLab GitHub Release notes.")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--changelog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rendered = render_release_notes(
        template=args.template.read_text(encoding="utf-8"),
        version=args.version,
        changelog=args.changelog.read_text(encoding="utf-8"),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
