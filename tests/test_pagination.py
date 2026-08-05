"""Pagination: one iterator shared by all five list endpoints."""

from __future__ import annotations

import pytest

from snapvisor.pagination import (
    MAX_PER_PAGE,
    Page,
    auto_paginate,
    clamp_per_page,
    iter_pages,
    to_page,
)


def _envelope(page: int, per_page: int, total: int) -> dict:
    start = (page - 1) * per_page
    return {
        "results": [{"id": index} for index in range(start, min(start + per_page, total))],
        "pageInfo": {"total": total, "page": page, "perPage": per_page},
    }


class _Server:
    """A fake paginated endpoint that records how it was called."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.calls: list[dict] = []

    def __call__(self, *, page: str, per_page: str, **kwargs: object) -> dict:
        self.calls.append({"page": page, "per_page": per_page, **kwargs})
        return _envelope(int(page), int(per_page), self.total)


def test_auto_paginate_yields_every_item_and_issues_one_request_per_page():
    server = _Server(total=7)
    items = list(auto_paginate(server, per_page=3))

    assert len(items) == 7
    assert [item["id"] for item in items] == list(range(7))
    assert len(server.calls) == 3, "3 pages of 3, 3, 1 — no speculative fourth request"
    assert [call["page"] for call in server.calls] == ["1", "2", "3"]


def test_auto_paginate_forwards_path_parameters_untouched():
    server = _Server(total=1)
    list(auto_paginate(server, per_page=10, owner="acme", project="web"))
    assert server.calls[0]["owner"] == "acme"
    assert server.calls[0]["project"] == "web"


def test_auto_paginate_stops_at_max_items_without_fetching_further_pages():
    server = _Server(total=100)
    items = list(auto_paginate(server, per_page=10, max_items=12))
    assert len(items) == 12
    assert len(server.calls) == 2


def test_iter_pages_exposes_page_metadata():
    server = _Server(total=5)
    pages = list(iter_pages(server, per_page=2))
    assert [(p.page, p.per_page, p.total, len(p)) for p in pages] == [
        (1, 2, 5, 2),
        (2, 2, 5, 2),
        (3, 2, 5, 1),
    ]
    assert pages[0].has_more is True
    assert pages[-1].has_more is False


def test_iter_pages_honours_max_pages():
    server = _Server(total=100)
    assert len(list(iter_pages(server, per_page=5, max_pages=2))) == 2


def test_empty_result_set_makes_exactly_one_request():
    server = _Server(total=0)
    assert list(auto_paginate(server)) == []
    assert len(server.calls) == 1


@pytest.mark.parametrize(
    ("requested", "expected"), [(0, 1), (-5, 1), (1, 1), (30, 30), (100, 100), (5000, MAX_PER_PAGE)]
)
def test_per_page_is_clamped_to_the_documented_range(requested: int, expected: int):
    assert clamp_per_page(requested) == expected


def test_clamping_is_applied_to_the_wire():
    server = _Server(total=1)
    list(auto_paginate(server, per_page=9999))
    assert server.calls[0]["per_page"] == str(MAX_PER_PAGE)


def test_to_page_accepts_a_generated_attrs_envelope():
    class _PageInfo:
        total, page, per_page = 3.0, 1.0, 30.0

    class _Envelope:
        results = ["a", "b", "c"]
        page_info = _PageInfo()

    page = to_page(_Envelope())
    assert page == Page(results=["a", "b", "c"], total=3, page=1, per_page=30)


def test_to_page_rejects_a_non_paginated_payload():
    with pytest.raises(TypeError, match="Not a paginated"):
        to_page(object())
