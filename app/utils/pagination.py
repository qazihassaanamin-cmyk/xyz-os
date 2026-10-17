"""Pagination helpers.

Every list in the application is bounded: a page size and a maximum page number
are always applied, so no query can return an unbounded result set.
"""

from __future__ import annotations

from flask import request

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


class Pagination:
    """A small, explicit pagination result.

    Deliberately not Flask-SQLAlchemy's paginator: this keeps the page size
    bounded, survives a bad ``?page=`` value, and exposes exactly the fields the
    templates need.
    """

    def __init__(self, items, *, page: int, per_page: int, total: int):
        self.items = items
        self.page = page
        self.per_page = per_page
        self.total = total

    @property
    def pages(self) -> int:
        if self.per_page <= 0:
            return 1
        return max(1, -(-self.total // self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def prev_num(self) -> int:
        return max(1, self.page - 1)

    @property
    def next_num(self) -> int:
        return min(self.pages, self.page + 1)

    @property
    def first_index(self) -> int:
        if self.total == 0:
            return 0
        return (self.page - 1) * self.per_page + 1

    @property
    def last_index(self) -> int:
        return min(self.total, self.page * self.per_page)

    def window(self, width: int = 2) -> list[int | None]:
        """Page numbers to render, with ``None`` marking an ellipsis."""
        if self.pages <= 1:
            return [1]
        pages: list[int | None] = []
        low = max(1, self.page - width)
        high = min(self.pages, self.page + width)
        if low > 1:
            pages.append(1)
            if low > 2:
                pages.append(None)
        pages.extend(range(low, high + 1))
        if high < self.pages:
            if high < self.pages - 1:
                pages.append(None)
            pages.append(self.pages)
        return pages


def parse_page(value: str | None) -> int:
    try:
        page = int(value or 1)
    except (TypeError, ValueError):
        return 1
    return max(1, min(page, 10_000))


def parse_per_page(value: str | None, default: int = DEFAULT_PAGE_SIZE) -> int:
    try:
        size = int(value or default)
    except (TypeError, ValueError):
        return default
    return max(5, min(size, MAX_PAGE_SIZE))


def paginate(query, *, page: int | None = None, per_page: int | None = None) -> Pagination:
    """Paginate a SQLAlchemy query with bounded inputs."""
    page = parse_page(str(page)) if page is not None else parse_page(request.args.get("page"))
    if per_page is None:
        per_page = parse_per_page(request.args.get("per_page"))
    else:
        per_page = parse_per_page(str(per_page), default=per_page)

    total = query.order_by(None).count()
    items = query.limit(per_page).offset((page - 1) * per_page).all()
    return Pagination(items, page=page, per_page=per_page, total=total)


def paginate_list(items: list, *, page: int | None = None, per_page: int | None = None) -> Pagination:
    """Paginate an in-memory list, for the rare case a query cannot express it."""
    page = parse_page(str(page)) if page is not None else parse_page(request.args.get("page"))
    if per_page is None:
        per_page = parse_per_page(request.args.get("per_page"))
    else:
        per_page = parse_per_page(str(per_page), default=per_page)

    total = len(items)
    start = (page - 1) * per_page
    return Pagination(items[start : start + per_page], page=page, per_page=per_page, total=total)
