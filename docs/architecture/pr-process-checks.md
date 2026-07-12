# PR Process Checks

This repository follows the issue-first and documentation-sync expectations in
`AGENTS.md`. The pull-request workflow enforces those expectations as a
blocking CI gate for release-bound changes.

## Templates

- `.github/ISSUE_TEMPLATE/change_request.yml` asks for the primary issue label,
  background, current behavior, expected behavior, impact, reproduction
  material, and acceptance criteria.
- `.github/workflows/issue-labels.yml` applies labels selected in the issue
  form.
- `.github/pull_request_template.md` asks for a linked issue, productization and
  protected-algorithm impact when applicable, docs, tests, and pre-commit review.

## CI Gate

`.github/workflows/pr-process.yml` runs `scripts/ci/check_pr_process.py` with
`--strict`. The job fails when:

- Core, Desktop, benchmark, release-facing README/user/Core/architecture docs,
  productization, release-workflow, or release-automation files change without
  a linked issue; `No issue needed:` is not valid for that scope;
- the PR body has no `Fixes #...`, `Closes #...`, `Resolves #...`, or
  `Part of #...` reference and does not explain `No issue needed:`;
- non-documentation files changed, but the diff has no docs-like file and the PR
body does not explain `No docs needed:`.

The override remains available for narrow non-release mechanical changes and
local experiments described by `AGENTS.md`; it is not a release-process bypass.

Maintainers can run the same script locally without `--strict` for a
warning-only dry run:

```bash
python3 scripts/ci/check_pr_process.py \
  --pr-body-file pr_body.md \
  --changed-files-file changed_files.txt
```

Use `--strict` to reproduce CI's blocking behavior.
