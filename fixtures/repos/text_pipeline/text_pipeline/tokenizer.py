"""Tokenisation primitives shared by the analytics layer."""

from __future__ import annotations

import re

SPLIT_RE = re.compile(r"\s+")

STOPWORDS = frozenset({"the", "a", "an", "and", "or", "of", "to", "in", "is"})


def normalize(word: str) -> str:
    """Normalise a single word for counting.

    Words are lowercased and surrounding punctuation is removed so that
    ``"Ship."`` and ``"ship"`` are counted as the same token.
    """
    return word.lower()


def tokenize(text: str) -> list[str]:
    """Split ``text`` into normalised, non-empty, non-stopword tokens."""
    tokens = []
    for raw in SPLIT_RE.split(text):
        token = normalize(raw)
        if not token or token in STOPWORDS:
            continue
        tokens.append(token)
    return tokens
