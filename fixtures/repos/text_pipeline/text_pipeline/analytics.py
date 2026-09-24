"""Word frequency analytics built on top of the tokenizer."""

from __future__ import annotations

from collections import Counter

from text_pipeline.tokenizer import tokenize


def word_counts(text: str) -> Counter[str]:
    """Count normalised tokens in ``text``."""
    return Counter(tokenize(text))


def top_words(text: str, limit: int = 3) -> list[tuple[str, int]]:
    """Return the ``limit`` most common tokens, ties broken alphabetically."""
    counts = word_counts(text)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ordered[:limit]


def vocabulary_size(text: str) -> int:
    """Number of distinct tokens in ``text``."""
    return len(word_counts(text))
