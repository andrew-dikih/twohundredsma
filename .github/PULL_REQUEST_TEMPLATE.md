<!--
Thanks for the contribution! A couple of quick reminders below.
-->

## What does this change?

<!-- Describe the change. -->

## Docs-only CI gating (reminder, not enforced)

This repo's workflows (`ci.yml`, `deploy.yml`, `label-check.yml`) skip
automatically for PRs/pushes whose diff is limited to `README.md`, `LICENSE`,
`OPTIONS_RULES.md`, or `STRATEGY_RULES.md` — see
[`.github/DOCS_ONLY_CI_GATING.md`](./DOCS_ONLY_CI_GATING.md).

- This is a **reminder only** — nothing here is enforced by a bot or a
  required checkbox. GitHub's trigger-level `paths-ignore` filters do the
  actual gating on their own.
- If this PR is genuinely docs-only, you may see no CI runs at all — that's
  expected, not a failure.
- If this PR mixes docs with any other change, CI/CD run normally; no
  action needed on your part.
- If this PR changes ~3,000+ files, GitHub's path-filter evaluation is not
  guaranteed complete — please split it or manually validate/dispatch the
  affected workflows per the doc above.
