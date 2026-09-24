"""Pagination helpers for the task listing endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 2


@dataclass(frozen=True)
class Page:
    items: list
    page: int
    per_page: int
    total: int

    @property
    def total_pages(self) -> int:
        if self.per_page <= 0:
            return 0
        return (self.total + self.per_page - 1) // self.per_page

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


def paginate(items: list[T], page: int = 1, per_page: int = DEFAULT_PAGE_SIZE) -> Page:
    """Return one 1-indexed page of ``items``.

    ``page=1`` must return the first ``per_page`` items. Pages beyond the end
    return an empty item list rather than raising.
    """
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    if page < 1:
        raise ValueError("page is 1-indexed")
    start = page * per_page
    end = start + per_page
    return Page(items=list(items[start:end]), page=page, per_page=per_page, total=len(items))
