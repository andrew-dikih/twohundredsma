"""Deterministic harness for the docs-only CI/CD trigger-level gating.

This module has two jobs:

1. Read the *actual* ``paths-ignore`` lists out of the real workflow YAML
   files (not a hand-duplicated copy) so drift between ``ci.yml``,
   ``deploy.yml``, and ``label-check.yml`` is caught automatically.
2. Provide a small, pure classification function that mirrors what GitHub's
   own trigger-level path filtering does, so we have deterministic,
   fast, offline test coverage of the intended behavior matrix -- without
   needing to fire real workflow runs.

IMPORTANT: this harness does not (and cannot) change how GitHub evaluates
``paths``/``paths-ignore`` at trigger time. It documents and tests the
*policy* those filters encode. See ``.github/DOCS_ONLY_CI_GATING.md`` for
the full behavior matrix and the known >=3,000-file limitation.
"""
from __future__ import annotations

import fnmatch
import enum
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# GitHub only evaluates paths/paths-ignore filters against the first 3,000
# changed files of a diff. Beyond that, whether the filter saw every file is
# unverified.
GITHUB_PATH_FILTER_FILE_LIMIT = 3000

# The narrow, canonical passive-doc allowlist. This is the single source of
# truth for tests; every workflow's paths-ignore is asserted equal to it.
CANONICAL_DOCS_ONLY_ALLOWLIST = [
    "README.md",
    "LICENSE",
    "OPTIONS_RULES.md",
    "STRATEGY_RULES.md",
]

WORKFLOW_FILES = ["ci.yml", "deploy.yml", "label-check.yml"]


class Classification(str, enum.Enum):
    SKIP = "SKIP"                  # workflow would not trigger
    RUN = "RUN"                    # workflow triggers normally
    INDETERMINATE = "INDETERMINATE"  # diff too large to trust the filter


def _load_workflow(name: str) -> dict:
    with open(WORKFLOWS_DIR / name, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _on_block(workflow: dict) -> dict:
    # PyYAML (YAML 1.1) parses the bare scalar key ``on`` as the boolean
    # True. Handle both so this harness survives a future switch to
    # ``"on":`` quoted syntax.
    if "on" in workflow:
        return workflow["on"]
    return workflow[True]


def _extract_paths_ignore(workflow: dict, trigger: str) -> list[str]:
    """Return the paths-ignore list for a given trigger (e.g. 'pull_request')."""
    on_block = _on_block(workflow)
    trigger_block = on_block.get(trigger)
    if trigger_block is None:
        raise AssertionError(f"trigger {trigger!r} not found")
    return list(trigger_block.get("paths-ignore", []))


def classify_diff(
    changed_files: list[str],
    ignore_patterns: list[str] | None = None,
    max_files: int = GITHUB_PATH_FILTER_FILE_LIMIT,
) -> Classification:
    """Classify a changed-file set the same way GitHub's paths-ignore would.

    - If the diff is at/over GitHub's ~3,000-file filter-evaluation limit,
      classification is INDETERMINATE regardless of content: we cannot
      assert the filter reliably inspected every file.
    - Otherwise, SKIP only if every changed file matches one of the
      ignore glob patterns; RUN if any file does not match (this also
      covers the "no files changed" edge case defensively -> RUN, since an
      empty diff should never be asserted as a verified skip).
    """
    if ignore_patterns is None:
        ignore_patterns = CANONICAL_DOCS_ONLY_ALLOWLIST

    if len(changed_files) >= max_files:
        return Classification.INDETERMINATE

    if not changed_files:
        return Classification.RUN

    for f in changed_files:
        if not any(fnmatch.fnmatch(f, pat) for pat in ignore_patterns):
            return Classification.RUN

    return Classification.SKIP


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_workflow_files_exist():
    for name in WORKFLOW_FILES:
        assert (WORKFLOWS_DIR / name).is_file(), f"missing workflow: {name}"


def test_ci_yml_paths_ignore_matches_canonical():
    wf = _load_workflow("ci.yml")
    assert _extract_paths_ignore(wf, "pull_request") == CANONICAL_DOCS_ONLY_ALLOWLIST


def test_deploy_yml_paths_ignore_matches_canonical():
    wf = _load_workflow("deploy.yml")
    assert _extract_paths_ignore(wf, "push") == CANONICAL_DOCS_ONLY_ALLOWLIST


def test_label_check_yml_paths_ignore_matches_canonical():
    wf = _load_workflow("label-check.yml")
    assert _extract_paths_ignore(wf, "pull_request") == CANONICAL_DOCS_ONLY_ALLOWLIST


def test_dot_github_never_in_allowlist():
    # .github/** changes (including to the workflows themselves) must always
    # remain automation-required.
    assert not any(
        fnmatch.fnmatch(".github/workflows/ci.yml", pat)
        for pat in CANONICAL_DOCS_ONLY_ALLOWLIST
    )


def test_allowlist_has_no_wildcards():
    # Narrow-by-construction: exact filenames only. A wildcard could
    # accidentally swallow a future deployable path (e.g. dist/docs/**) or
    # docs shipped under a runtime/infra directory (e.g. advisor/templates).
    for pattern in CANONICAL_DOCS_ONLY_ALLOWLIST:
        assert "*" not in pattern and "/" not in pattern


def test_deploy_workflow_dispatch_present_and_unfiltered():
    wf = _load_workflow("deploy.yml")
    on_block = _on_block(wf)
    assert "workflow_dispatch" in on_block
    # workflow_dispatch has no paths-ignore key at all -- it is not subject
    # to path filtering and always runs when manually triggered.
    assert on_block["workflow_dispatch"] in ({}, None)


# --- classify_diff behavior matrix -----------------------------------------


def test_pure_docs_only_diff_skips():
    assert classify_diff(["README.md"]) == Classification.SKIP
    assert classify_diff(["README.md", "LICENSE", "STRATEGY_RULES.md"]) == Classification.SKIP


def test_mixed_diff_runs():
    assert classify_diff(["README.md", "advisor/app.py"]) == Classification.RUN


def test_source_only_diff_runs():
    assert classify_diff(["advisor/app.py", "scripts/provision-azure.ps1"]) == Classification.RUN


def test_dot_github_change_always_runs():
    assert classify_diff([".github/workflows/ci.yml"]) == Classification.RUN
    # Even alongside an allowlisted doc file, a .github/** change still runs.
    assert classify_diff(["README.md", ".github/workflows/ci.yml"]) == Classification.RUN


def test_hypothetical_deployable_docs_directory_runs():
    # Guards the "narrow allowlist" property: a wildcard-free allowlist
    # cannot accidentally match generated/deployable docs.
    assert classify_diff(["dist/docs/index.html"]) == Classification.RUN
    assert classify_diff(["advisor/templates/about.html"]) == Classification.RUN


def test_operational_root_markdown_not_in_allowlist_runs():
    # Any root Markdown file not explicitly allowlisted fails closed.
    assert classify_diff(["CHANGELOG.md"]) == Classification.RUN
    assert classify_diff(["CONTRIBUTING.md"]) == Classification.RUN


def test_empty_diff_is_not_asserted_as_skip():
    assert classify_diff([]) == Classification.RUN


def test_large_diff_is_indeterminate_even_if_docs_only():
    many_doc_files = ["README.md"] * GITHUB_PATH_FILTER_FILE_LIMIT
    assert classify_diff(many_doc_files) == Classification.INDETERMINATE


def test_large_mixed_diff_is_also_indeterminate():
    files = ["advisor/app.py"] * GITHUB_PATH_FILTER_FILE_LIMIT
    assert classify_diff(files) == Classification.INDETERMINATE


def test_diff_just_under_limit_is_still_classified_normally():
    files = ["README.md"] * (GITHUB_PATH_FILTER_FILE_LIMIT - 1)
    assert classify_diff(files) == Classification.SKIP


def test_none_of_the_allowlisted_docs_are_imported_by_app_code():
    """Guard against future coupling: if code ever starts reading one of
    these files at runtime, it must be removed from the passive-doc
    allowlist. Scans advisor/, scripts/, and tests/ source for references.
    """
    doc_names = CANONICAL_DOCS_ONLY_ALLOWLIST
    code_dirs = [ROOT / "advisor", ROOT / "scripts", ROOT / "tests"]
    offending: list[str] = []
    for code_dir in code_dirs:
        if not code_dir.exists():
            continue
        for path in code_dir.rglob("*.py"):
            if path.resolve() == Path(__file__).resolve():
                continue  # this harness legitimately references the names as literals
            text = path.read_text(encoding="utf-8", errors="ignore")
            for doc_name in doc_names:
                if doc_name in text:
                    offending.append(f"{path}: references {doc_name}")
    assert not offending, "\n".join(offending)
