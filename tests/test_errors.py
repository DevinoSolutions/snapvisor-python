"""Error taxonomy: one exception class per failure kind, with the server's details.

0.1.0 raised a single ``SnapvisorAPIError`` for every status and dropped the
``details[]`` array entirely, so a validation failure told the caller *that* the
request was rejected but never *which field* was wrong.
"""

from __future__ import annotations

import httpx
import pytest

from snapvisor.errors import (
    REQUEST_ID_HEADER,
    SnapvisorAPIError,
    SnapvisorAuthError,
    SnapvisorConflictError,
    SnapvisorForbiddenError,
    SnapvisorNotFoundError,
    SnapvisorRateLimitError,
    SnapvisorServerError,
    error_from_response,
    parse_error_body,
    parse_rate_limit_headers,
)


def _response(status: int, *, json_body: object = None, headers: dict | None = None, text=None):
    request = httpx.Request(
        "POST", "https://api.snapvisor.io/v2/builds", headers={REQUEST_ID_HEADER: "req-123"}
    )
    if text is not None:
        return httpx.Response(status, text=text, headers=headers, request=request)
    return httpx.Response(status, json=json_body, headers=headers, request=request)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, SnapvisorAuthError),
        (403, SnapvisorForbiddenError),
        (404, SnapvisorNotFoundError),
        (409, SnapvisorConflictError),
        (429, SnapvisorRateLimitError),
        (500, SnapvisorServerError),
        (503, SnapvisorServerError),
        (418, SnapvisorAPIError),
    ],
)
def test_each_status_maps_to_its_own_exception_class(status: int, expected: type):
    error = error_from_response(_response(status, json_body={"error": "nope"}))
    assert type(error) is expected
    assert isinstance(error, SnapvisorAPIError), "every subclass stays catchable as the 0.1.0 type"
    assert error.status_code == status


def test_validation_details_reach_the_message():
    error = error_from_response(
        _response(
            400,
            json_body={
                "error": "Invalid parameters",
                "details": [
                    {"message": "body.commit is invalid"},
                    {"message": "body.screenshots is required"},
                ],
            },
        )
    )
    assert error.details == ["body.commit is invalid", "body.screenshots is required"]
    assert "body.commit is invalid" in str(error)
    assert "body.screenshots is required" in str(error)


def test_request_id_is_carried_on_the_exception():
    error = error_from_response(_response(500, json_body={"error": "boom"}))
    assert error.request_id == "req-123"
    assert "req-123" in str(error)


def test_non_json_body_falls_back_to_raw_text():
    error = error_from_response(_response(502, text="<html>Bad Gateway</html>"))
    assert error.message == "<html>Bad Gateway</html>"


def test_rate_limit_error_exposes_the_window():
    error = error_from_response(
        _response(
            429,
            json_body={"error": "Too many requests"},
            headers={
                "RateLimit": '"default";r=0;t=42',
                "RateLimit-Policy": '"default";q=100;w=60',
            },
        )
    )
    assert isinstance(error, SnapvisorRateLimitError)
    assert (error.limit, error.remaining, error.reset) == (100, 0, 42.0)


def test_parse_rate_limit_headers_prefers_draft8_over_retry_after():
    headers = httpx.Headers({"RateLimit": '"default";r=5;t=9', "Retry-After": "60"})
    assert parse_rate_limit_headers(headers) == (None, 5, 9.0)


def test_parse_rate_limit_headers_handles_an_http_date_retry_after():
    headers = httpx.Headers({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    _, _, reset = parse_rate_limit_headers(headers)
    assert reset == 0.0, "a date in the past means retry immediately, never a negative sleep"


def test_parse_rate_limit_headers_on_an_empty_response():
    assert parse_rate_limit_headers(httpx.Headers({})) == (None, None, None)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"error": "nope"}, ("nope", [])),
        ({"error": "nope", "details": [{"message": "a"}]}, ("nope", ["a"])),
        ({"error": "nope", "details": ["a"]}, ("nope", ["a"])),
        ({"details": [{"message": "a"}]}, (None, ["a"])),
        ({}, (None, [])),
        ("not a dict", (None, [])),
    ],
)
def test_parse_error_body(payload: object, expected: tuple):
    assert parse_error_body(payload) == expected
