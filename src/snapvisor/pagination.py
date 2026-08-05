"""Pagination helpers for the five list operations.

Every list endpoint (``listBuilds``, ``listProjects``, ``listComments``,
``listReviews``, ``listBuildDiffs``) shares one envelope —
``{"results": [...], "pageInfo": {"total", "page", "perPage"}}`` — and one
parameter pair, ``page`` (>= 1) and ``perPage`` (1..100). Shipping the iterator
once means no endpoint has to grow its own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")

MIN_PER_PAGE = 1
MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 30


def clamp_per_page(per_page: int) -> int:
    """Clamp ``per_page`` into the documented 1..100 range.

    The API rejects anything outside it with a 400; clamping turns a
    latent runtime failure into the obvious behaviour.
    """
    return max(MIN_PER_PAGE, min(MAX_PER_PAGE, per_page))


@dataclass(frozen=True)
class Page(Generic[T]):
    """One page of a paginated Snapvisor response.

    Attributes:
        results: The items on this page.
        total: Total number of items across every page.
        page: 1-based index of this page.
        per_page: Page size used for this request.
    """

    results: list[T]
    total: int
    page: int
    per_page: int

    def __iter__(self) -> Iterator[T]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    @property
    def has_more(self) -> bool:
        """Whether at least one more page exists after this one."""
        return self.page * self.per_page < self.total


def to_page(envelope: Any) -> Page[Any]:
    """Convert a generated ``…Response200`` envelope into a :class:`Page`.

    Accepts both the attrs models produced by the generated client and a plain
    decoded ``dict``, so handwritten call sites and generated ones share it.

    Raises:
        TypeError: If ``envelope`` carries neither shape.
    """
    if isinstance(envelope, dict):
        info = envelope.get("pageInfo") or {}
        return Page(
            results=list(envelope.get("results") or []),
            total=int(info.get("total", 0)),
            page=int(info.get("page", 1)),
            per_page=int(info.get("perPage", DEFAULT_PER_PAGE)),
        )
    results = getattr(envelope, "results", None)
    info = getattr(envelope, "page_info", None)
    if results is None or info is None:
        raise TypeError(f"Not a paginated Snapvisor response: {envelope!r}")
    return Page(
        results=list(results),
        total=int(info.total),
        page=int(info.page),
        per_page=int(info.per_page),
    )


def iter_pages(
    fetch: Callable[..., Any],
    *,
    per_page: int = DEFAULT_PER_PAGE,
    start_page: int = 1,
    max_pages: int | None = None,
    **kwargs: Any,
) -> Iterator[Page[Any]]:
    """Yield successive :class:`Page` objects from a paginated operation.

    Args:
        fetch: A callable accepting ``page`` and ``per_page`` keyword arguments
            and returning a paginated envelope.
        per_page: Page size, clamped to 1..100.
        start_page: 1-based page to start from.
        max_pages: Stop after this many pages; ``None`` means "until exhausted".
        **kwargs: Forwarded to ``fetch`` unchanged (path params, filters, …).

    Yields:
        One :class:`Page` per request, in order.
    """
    size = clamp_per_page(per_page)
    page_number = max(1, start_page)
    fetched = 0
    while True:
        page = to_page(fetch(page=str(page_number), per_page=str(size), **kwargs))
        yield page
        fetched += 1
        if not page.results or not page.has_more:
            return
        if max_pages is not None and fetched >= max_pages:
            return
        page_number += 1


def auto_paginate(
    fetch: Callable[..., Any],
    *,
    per_page: int = DEFAULT_PER_PAGE,
    start_page: int = 1,
    max_items: int | None = None,
    **kwargs: Any,
) -> Iterator[Any]:
    """Yield every item across every page, transparently walking pages.

    Args:
        fetch: As for :func:`iter_pages`.
        per_page: Page size, clamped to 1..100.
        start_page: 1-based page to start from.
        max_items: Stop after yielding this many items.
        **kwargs: Forwarded to ``fetch`` unchanged.

    Yields:
        Each item, in server order.
    """
    emitted = 0
    for page in iter_pages(fetch, per_page=per_page, start_page=start_page, **kwargs):
        for item in page.results:
            yield item
            emitted += 1
            if max_items is not None and emitted >= max_items:
                return


async def aiter_pages(
    fetch: Callable[..., Awaitable[Any]],
    *,
    per_page: int = DEFAULT_PER_PAGE,
    start_page: int = 1,
    max_pages: int | None = None,
    **kwargs: Any,
) -> AsyncIterator[Page[Any]]:
    """Asynchronous twin of :func:`iter_pages`."""
    size = clamp_per_page(per_page)
    page_number = max(1, start_page)
    fetched = 0
    while True:
        page = to_page(await fetch(page=str(page_number), per_page=str(size), **kwargs))
        yield page
        fetched += 1
        if not page.results or not page.has_more:
            return
        if max_pages is not None and fetched >= max_pages:
            return
        page_number += 1


async def aauto_paginate(
    fetch: Callable[..., Awaitable[Any]],
    *,
    per_page: int = DEFAULT_PER_PAGE,
    start_page: int = 1,
    max_items: int | None = None,
    **kwargs: Any,
) -> AsyncIterator[Any]:
    """Asynchronous twin of :func:`auto_paginate`."""
    emitted = 0
    async for page in aiter_pages(fetch, per_page=per_page, start_page=start_page, **kwargs):
        for item in page.results:
            yield item
            emitted += 1
            if max_items is not None and emitted >= max_items:
                return
