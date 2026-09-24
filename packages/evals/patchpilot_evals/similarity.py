"""Patch similarity -- a secondary signal, never a correctness claim.

Two patches that look alike can behave differently, and two patches that look
nothing alike can both be correct. This score is useful for spotting "the model
edited the right lines but the tests still fail", and for nothing else. The
report labels it accordingly and never folds it into the pass rate.
"""

from __future__ import annotations

import difflib
import re

from patchpilot_core.diffutil import DiffParseError, parse_unified_diff

_WHITESPACE_RE = re.compile(r"\s+")


def changed_lines(diff: str) -> tuple[set[str], set[str]]:
    """Normalised added/removed lines, keyed by file."""
    try:
        patches = parse_unified_diff(diff)
    except DiffParseError:
        return set(), set()
    added: set[str] = set()
    removed: set[str] = set()
    for patch in patches:
        for hunk in patch.hunks:
            for op, text in hunk.lines:
                normalised = f"{patch.target_path}:{_WHITESPACE_RE.sub(' ', text).strip()}"
                if op == "+":
                    added.add(normalised)
                elif op == "-":
                    removed.add(normalised)
    return added, removed


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def patch_similarity(candidate: str, reference: str) -> float:
    """0..1 similarity between two unified diffs.

    Combines set overlap of normalised changed lines (structure) with a sequence
    ratio over the concatenated added text (wording), weighted towards structure.
    """
    if not candidate.strip() or not reference.strip():
        return 0.0

    candidate_added, candidate_removed = changed_lines(candidate)
    reference_added, reference_removed = changed_lines(reference)
    if not (candidate_added or candidate_removed) or not (reference_added or reference_removed):
        # One of them did not parse as a diff; fall back to raw text similarity.
        return round(difflib.SequenceMatcher(None, candidate, reference).ratio(), 4)

    structural = 0.6 * jaccard(candidate_added, reference_added) + 0.4 * jaccard(
        candidate_removed, reference_removed
    )
    textual = difflib.SequenceMatcher(
        None,
        "\n".join(sorted(candidate_added)),
        "\n".join(sorted(reference_added)),
    ).ratio()
    return round(0.7 * structural + 0.3 * textual, 4)


def touched_expected_files(diff: str, expected: list[str]) -> tuple[int, int]:
    """How many of the expected files the patch actually touched."""
    if not expected:
        return (0, 0)
    try:
        patches = parse_unified_diff(diff)
    except DiffParseError:
        return (0, len(expected))
    touched = {patch.target_path for patch in patches}
    hit = sum(1 for path in expected if path in touched)
    return (hit, len(expected))
