# Docs-only CI/CD gating

This repo uses **trigger-level `paths-ignore` filters** (not runner-consuming
skip jobs) so that a pull request or push whose *entire* diff is limited to a
narrow set of root-level documentation files never queues a workflow run at
all. That satisfies two goals:

1. Passive documentation-only PRs consume **zero** GitHub-hosted runner
   minutes (the workflow simply doesn't start — GitHub itself decides this
   before any job/runner is provisioned).
2. Passive documentation-only pushes never trigger CI, CD, or deploy.

## The allowlist (deliberately narrow)

```yaml
paths-ignore:
  - 'README.md'
  - 'LICENSE'
  - 'OPTIONS_RULES.md'
  - 'STRATEGY_RULES.md'
```

This exact list is repeated verbatim in `ci.yml`, `deploy.yml`, and
`label-check.yml`, and is asserted to match across all three (and to match
this document) by `tests/test_ci_docs_only_gating.py`.

Rules that keep it fail-closed:

- **Exact root filenames only — never a wildcard** (no `**/*.md`, no
  `docs/**`). A wildcard could accidentally swallow a future deployable
  path such as `dist/docs/**`, generated docs shipped under
  `advisor/templates/`, or an operational root Markdown file that a script
  actually reads. None of the four listed files are imported, parsed, or
  read by any code in `advisor/`, `scripts/`, or `tests/` (verified by
  grep as part of this rollout) — they are pure human documentation.
- `.github/**` is never in the allowlist. Any workflow, action, or CI-config
  change always triggers full automation.
- Source (`advisor/`, `scripts/`, `templates/`, top-level `*.py`), config
  (`requirements.txt`, `dockerfile`, `docker-compose.yml`, `.dockerignore`),
  and any file not explicitly named above remain **automation-required**.
- A **mixed** diff (e.g. `README.md` + `advisor/app.py`) always runs
  automation normally — GitHub only skips a workflow when *every* changed
  file matches `paths-ignore`. There is no partial/best-effort skip.
- `workflow_dispatch` in `deploy.yml` is unaffected by `paths-ignore` — a
  manual dispatch always runs regardless of the last diff's contents.
- `label-check.yml` skipping on docs-only PRs to `main` is intentional: no
  file in the allowlist can ever cause `deploy.yml` to build or deploy an
  image (deploy only triggers off pushes to `develop`), so requiring the
  `release` label for a docs-only PR would add friction with no matching
  risk.

## Known GitHub limitation — NOT a fail-closed guarantee at scale

GitHub's own trigger-level `paths` / `paths-ignore` evaluation is
[documented as only reliable for the first 3,000 files changed in a
diff](https://docs.github.com/actions/using-workflows/workflow-syntax-for-github-actions#example-including-paths).
Beyond that, GitHub itself does not promise the filter inspected every
changed file.

**We make no unqualified fail-closed claim.** Concretely:

- For an ordinary, complete diff (well under 3,000 files), this filter is
  fail-closed: any non-doc file present anywhere in the diff causes the
  workflow to run.
- For a diff at or beyond ~3,000 changed files, classification must be
  treated as **indeterminate**, not "verified docs-only" — even if GitHub
  happens to skip the run. `tests/test_ci_docs_only_gating.py` encodes this
  as an explicit `INDETERMINATE` classification distinct from `SKIP`/`RUN`.
- **Mitigation:** split oversized changes into smaller PRs/pushes. If an
  oversized change nonetheless reaches `main`/`develop`, freeze further
  releases and manually validate or `workflow_dispatch` the affected
  workflows (`ci.yml` has no manual dispatch trigger today — validate via a
  throwaway PR touching a non-doc file, or add dispatch before relying on
  it) rather than trusting the skip.

## Branch protection caveat

As of this rollout, `andrew-dikih/twohundredsma` has **no branch protection
rules or rulesets** configured on `main` or `develop` (verified via the
GitHub API). This means:

- Today, a skipped `ci.yml` run cannot block a PR merge, because there is no
  required status check configured at all.
- **If required status checks are added later** for `ci.yml` or
  `label-check.yml`, a docs-only PR will leave those checks perpetually
  "Expected" / pending (the workflow never runs, so it never reports a
  status), which **would block merge** unless the doc-only paths are also
  exempted in the branch protection / ruleset configuration, or the checks
  are marked not-required for path-filtered workflows. Re-visit this file
  before enabling required checks.

## Non-enforcement PR template reminder

`.github/PULL_REQUEST_TEMPLATE.md` includes a short reminder about this
gating. It is explicitly **non-enforcement** — a human note, not a required
checkbox or bot-verified gate — because GitHub's path filters already do the
technical enforcement; the template note exists only to prevent confusion
when a mixed PR "unexpectedly" runs full CI.
