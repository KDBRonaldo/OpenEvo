# PR Process Checks

This repository follows the issue-first and documentation-sync expectations in
`agents.md`. The first automated layer is intentionally non-blocking.

## Templates

- `.github/ISSUE_TEMPLATE/change_request.yml` asks for the primary issue label,
  background, current behavior, expected behavior, impact, reproduction material,
  and acceptance criteria.
- `.github/workflows/issue-labels.yml` reads the selected primary and secondary
  labels from issue-form output, removes stale labels from the allowed label set
  when the issue body is edited, and applies the selected labels.
- `.github/pull_request_template.md` asks for a linked issue, docs status, tests,
  and the pre-commit review checklist.

## Warning Check

`.github/workflows/pr-process.yml` runs `scripts/ci/check_pr_process.py` on pull
requests. The script prints warnings when:

- the PR body has no `Fixes #...`, `Closes #...`, `Resolves #...`, or
  `Part of #...` reference and does not explain `No issue needed:`;
- non-documentation files changed, but the diff has no docs-like file and the PR
  body does not explain `No docs needed:`.

The workflow exits successfully by default. Maintainers can run the same script
with `--strict` after the warning-only phase is accepted:

```bash
python3 scripts/ci/check_pr_process.py \
  --pr-body-file pr_body.md \
  --changed-files-file changed_files.txt \
  --strict
```
