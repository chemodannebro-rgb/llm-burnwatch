"""Docs lint: every humanized page's intro block avoids the same raw
statistics/internal-identifier jargon the console output already avoids
(see `tests/test_cli.py`'s `_BANNED_CONSOLE_JARGON`) -- these blocks exist
so someone who doesn't already know what a z-score or CUSUM is can tell
what a page is about before hitting the technical section.

Exception: the CUSUM detector pages (`cusum.md`/`cusum.ru.md`) are the one
place this vocabulary is unavoidable in the intro block -- the page's own
title names the algorithm, its pre-existing "Catches" paragraph explains
the detector by contrasting it with the baseline detector's z-score
threshold, and a terminology note documents the frozen
`Alert.detector`/`Alert.kind` string values. Those two pages are excluded
from the banned-word scan below; every other humanized page's intro block
is checked in full.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS_ROOT = Path(__file__).resolve().parent.parent / "docs"

# Group A (detectors) + Group B (concept pages) -- the 9 pages that got a
# from-scratch or merged human-readable intro block. Group C (api.md/
# performance.md) and Group D (faq.md/index.md) are reference/Q&A pages
# that were either left untouched or given only a one-line nudge, so
# they're not covered by this lint.
_HUMANIZED_PAGES = [
    "detectors/baseline.md",
    "detectors/baseline.ru.md",
    "detectors/cusum.md",
    "detectors/cusum.ru.md",
    "detectors/frequency.md",
    "detectors/frequency.ru.md",
    "detectors/rules.md",
    "detectors/rules.ru.md",
    "detectors/budget.md",
    "detectors/budget.ru.md",
    "security.md",
    "security.ru.md",
    "budget-vs-guard.md",
    "budget-vs-guard.ru.md",
    "comparison.md",
    "comparison.ru.md",
    "connecting.md",
    "connecting.ru.md",
]

# See module docstring: CUSUM's own detector page unavoidably names the
# algorithm/statistic it documents.
_JARGON_EXEMPT_PAGES = {"detectors/cusum.md", "detectors/cusum.ru.md"}

_BANNED_JARGON = (
    "z-score",
    "z-оценка",
    "mad",
    "cusum",
    "micros",
    "quantile",
    "квантил",
)


def _intro_block(text: str) -> str:
    """Text between the top-level `# Title` heading and the first `## `
    heading -- the human-readable block every humanized page starts with."""
    lines = text.splitlines()
    assert lines and lines[0].startswith(
        "# "
    ), "page must start with a top-level '# Title' heading"
    for i, line in enumerate(lines[1:], start=1):
        if line.startswith("## "):
            return "\n".join(lines[1:i])
    return "\n".join(lines[1:])


@pytest.mark.parametrize("relpath", _HUMANIZED_PAGES)
def test_intro_block_is_non_empty(relpath):
    text = (DOCS_ROOT / relpath).read_text(encoding="utf-8")
    block = _intro_block(text).strip()
    assert block, f"{relpath}: expected a human-readable intro block before the first '## ' heading"


@pytest.mark.parametrize("relpath", _HUMANIZED_PAGES)
def test_intro_block_avoids_internal_jargon(relpath):
    if relpath in _JARGON_EXEMPT_PAGES:
        pytest.skip("CUSUM's own page unavoidably names the algorithm/statistic it documents")
    text = (DOCS_ROOT / relpath).read_text(encoding="utf-8")
    block = _intro_block(text).lower()
    for term in _BANNED_JARGON:
        # Word-boundary match -- "mad" must not false-positive on "made"/
        # "madness", etc. (the other terms are unambiguous as substrings).
        pattern = r"\b" + re.escape(term) + r"\b"
        assert not re.search(pattern, block), f"{relpath}: banned jargon {term!r} found in intro block"
