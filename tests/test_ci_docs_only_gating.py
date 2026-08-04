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

Deliberately dependency-free: the workflow YAML used here is a small,
predictable subset (2-space indents, no tabs, no flow-style block
scalars) so we extract the handful of fields we need with plain text/
indentation parsing instead of pulling in a YAML library. A YAML parser
is not part of this repo's runtime and must not become one just for a
policy test.
"""
from __future__ import annotations

import fnmatch
import enum
from pathlib import Path

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


def _read_workflow(name: str) -> list[str]:
    return (WORKFLOWS_DIR / name).read_text(encoding="utf-8").splitlines()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find_key_line(lines: list[str], key: str, min_indent: int = 0, max_indent: int | None = None) -> int:
    """Return the index of a line whose stripped content is exactly ``key``
    (e.g. ``"on:"``), honoring an indent range so we don't match a
    same-named key nested somewhere else."""
    for i, line in enumerate(lines):
        stripped = line.strip()
        ind = _indent(line)
        if stripped == key and ind >= min_indent and (max_indent is None or ind <= max_indent):
            return i
    raise AssertionError(f"key {key!r} not found (indent range {min_indent}-{max_indent})")


def _find_trigger_block_range(lines: list[str], trigger: str) -> tuple[int, int, int]:
    """Locate the ``on:`` block and a specific trigger key inside it
    (e.g. 'pull_request', 'push'). Returns (on_indent, trigger_line_index,
    trigger_indent)."""
    on_idx = _find_key_line(lines, "on:", min_indent=0, max_indent=0)
    on_indent = _indent(lines[on_idx])
    # The trigger key is a line more indented than "on:" whose stripped
    # content is exactly "<trigger>:".
    trigger_idx = None
    trigger_indent = None
    for i in range(on_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        ind = _indent(line)
        if ind <= on_indent:
            break  # left the "on:" block entirely
        if line.strip() == f"{trigger}:":
            trigger_idx = i
            trigger_indent = ind
            break
    if trigger_idx is None:
        raise AssertionError(f"trigger {trigger!r} not found under 'on:'")
    return on_indent, trigger_idx, trigger_indent


def _extract_paths_ignore(name: str, trigger: str) -> list[str]:
    """Extract the paths-ignore glob list nested under on.<trigger>."""
    lines = _read_workflow(name)
    _on_indent, trigger_idx, trigger_indent = _find_trigger_block_range(lines, trigger)

    # Find "paths-ignore:" nested inside this trigger's block (indent >
    # trigger_indent), stopping once we dedent back to trigger_indent or
    # less (i.e. we've left the trigger block without finding it).
    pi_idx = None
    pi_indent = None
    for i in range(trigger_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        ind = _indent(line)
        if ind <= trigger_indent:
            break
        if line.strip() == "paths-ignore:":
            pi_idx = i
            pi_indent = ind
            break
    if pi_idx is None:
        raise AssertionError(f"paths-ignore not found under on.{trigger} in {name}")

    items: list[str] = []
    for i in range(pi_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        ind = _indent(line)
        stripped = line.strip()
        if ind <= pi_indent:
            break
        if not stripped.startswith("- "):
            break
        value = stripped[2:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        items.append(value)
    return items


def _trigger_exists(name: str, trigger: str) -> bool:
    """A trigger exists if it appears as a key directly under 'on:',
    whether as a block (``trigger:`` followed by nested lines) or an
    inline empty mapping (``trigger: {}``)."""
    lines = _read_workflow(name)
    on_idx = _find_key_line(lines, "on:", min_indent=0, max_indent=0)
    on_indent = _indent(lines[on_idx])
    for i in range(on_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        ind = _indent(line)
        if ind <= on_indent:
            break
        stripped = line.strip()
        if stripped == f"{trigger}:" or stripped.startswith(f"{trigger}:"):
            return True
    return False


def _workflow_dispatch_is_unfiltered(name: str) -> bool:
    """workflow_dispatch must appear as a bare/empty trigger (e.g.
    ``workflow_dispatch: {}``) directly under 'on:', i.e. it carries no
    paths/paths-ignore filter of its own."""
    lines = _read_workflow(name)
    on_idx = _find_key_line(lines, "on:", min_indent=0, max_indent=0)
    on_indent = _indent(lines[on_idx])
    for i in range(on_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        ind = _indent(line)
        if ind <= on_indent:
            break
        stripped = line.strip()
        if stripped.startswith("workflow_dispatch:"):
            rest = stripped[len("workflow_dispatch:"):].strip()
            if rest in ("", "{}"):
                # If nothing follows inline, make sure no nested
                # "paths"/"paths-ignore" key is indented under this key.
                for j in range(i + 1, len(lines)):
                    nxt = lines[j]
                    if not nxt.strip():
                        continue
                    nxt_ind = _indent(nxt)
                    if nxt_ind <= ind:
                        return True  # nothing nested under workflow_dispatch
                    if nxt.strip().startswith("paths"):
                        return False
                return True
            return False
    return False


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
    assert _extract_paths_ignore("ci.yml", "pull_request") == CANONICAL_DOCS_ONLY_ALLOWLIST


def test_deploy_yml_paths_ignore_matches_canonical():
    assert _extract_paths_ignore("deploy.yml", "push") == CANONICAL_DOCS_ONLY_ALLOWLIST


def test_label_check_yml_paths_ignore_matches_canonical():
    assert _extract_paths_ignore("label-check.yml", "pull_request") == CANONICAL_DOCS_ONLY_ALLOWLIST


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


def test_ci_and_deploy_have_manual_workflow_dispatch_recovery_control():
    # Both the CI and CD workflows must expose an un-path-filtered manual
    # dispatch trigger so an oversized (>=3,000-file) diff can be validated/
    # deployed by hand instead of trusting GitHub's path-filter evaluation.
    assert _trigger_exists("ci.yml", "workflow_dispatch")
    assert _trigger_exists("deploy.yml", "workflow_dispatch")
    assert _workflow_dispatch_is_unfiltered("ci.yml")
    assert _workflow_dispatch_is_unfiltered("deploy.yml")


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
