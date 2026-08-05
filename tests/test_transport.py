"""Transport policy: retries, backoff, 429/draft-8 handling, request correlation.

0.1.0 had none of this — a single 5xx or read timeout mid-upload aborted an
entire CI build after the screenshots had already been hashed and partly
uploaded. These tests inject the clock, so nothing here actually sleeps.
"""

from __future__ import annotations

import httpx
import pytest

from snapvisor.errors import (
    REQUEST_ID_HEADER,
    RETRY_ATTEMPT_HEADER,
    SnapvisorRateLimitError,
    error_from_response,
)
from snapvisor.transport import (
    AsyncRetryTransport,
    RetryConfig,
    RetryTransport,
    backoff_delay,
    should_retry_exception,
    should_retry_status,
)


class _RecordingTransport(httpx.BaseTransport):
    """Replays a scripted list of responses/exceptions and records every request."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            httpx.Request(request.method, request.url, headers=dict(request.headers))
        )
        outcome = self.script.pop(0) if self.script else httpx.Response(200, json={"ok": True})
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, httpx.Response)
        return outcome


class _AsyncRecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            httpx.Request(request.method, request.url, headers=dict(request.headers))
        )
        outcome = self.script.pop(0) if self.script else httpx.Response(200, json={"ok": True})
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, httpx.Response)
        return outcome


def _client(
    script: list[object], **config: object
) -> tuple[httpx.Client, _RecordingTransport, list]:
    inner = _RecordingTransport(script)
    slept: list[float] = []
    transport = RetryTransport(
        inner,
        config=RetryConfig(jitter=False, **config),  # type: ignore[arg-type]
        sleep=slept.append,
        rand=lambda: 1.0,
    )
    return httpx.Client(transport=transport, base_url="https://api.snapvisor.io/v2"), inner, slept


def test_retries_three_times_then_succeeds_on_server_errors():
    client, inner, slept = _client(
        [
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(502, json={"error": "boom"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    response = client.get("/project")

    assert response.status_code == 200
    assert len(inner.requests) == 3
    assert len(slept) == 2


def test_gives_up_after_max_retries_and_returns_the_last_response():
    client, inner, _ = _client([httpx.Response(500)] * 10, max_retries=3)
    response = client.get("/project")

    assert response.status_code == 500
    # One initial attempt plus three retries.
    assert len(inner.requests) == 4


def test_does_not_retry_a_client_error():
    client, inner, slept = _client([httpx.Response(400, json={"error": "bad"})])
    response = client.post("/builds", json={})

    assert response.status_code == 400
    assert len(inner.requests) == 1
    assert slept == []


def test_request_id_is_stable_across_retries_while_attempt_increments():
    client, inner, _ = _client([httpx.Response(503), httpx.Response(503), httpx.Response(200)])
    client.get("/project")

    request_ids = {request.headers[REQUEST_ID_HEADER] for request in inner.requests}
    attempts = [request.headers[RETRY_ATTEMPT_HEADER] for request in inner.requests]

    assert len(request_ids) == 1, (
        "the request id must identify the logical request, not the attempt"
    )
    assert attempts == ["0", "1", "2"]


def test_429_with_draft8_rate_limit_header_sleeps_until_reset_and_resumes():
    limited = httpx.Response(
        429,
        headers={
            "RateLimit": '"default";r=0;t=7',
            "RateLimit-Policy": '"default";q=100;w=60',
        },
        json={"error": "Too many requests"},
    )
    client, inner, slept = _client([limited, httpx.Response(200, json={"ok": True})])

    response = client.get("/project")

    assert response.status_code == 200
    assert slept == [7.0], "must wait exactly the window the API reported, not a blind backoff"
    assert len(inner.requests) == 2


def test_429_falls_back_to_retry_after_when_draft8_headers_are_absent():
    client, _, slept = _client(
        [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200)]
    )
    client.get("/project")
    assert slept == [3.0]


def test_429_beyond_the_wait_ceiling_is_surfaced_rather_than_slept_through():
    client, inner, slept = _client(
        [httpx.Response(429, headers={"RateLimit": '"default";r=0;t=3600'})],
        max_rate_limit_wait=60.0,
    )
    response = client.get("/project")

    assert response.status_code == 429
    assert slept == []
    assert len(inner.requests) == 1

    with pytest.raises(SnapvisorRateLimitError) as exc:
        raise error_from_response(response)
    assert exc.value.reset == 3600.0


def test_connect_errors_are_retried_for_a_post():
    client, inner, _ = _client(
        [httpx.ConnectError("refused"), httpx.Response(201, json={"build": {}})]
    )
    response = client.post("/builds", json={})
    assert response.status_code == 201
    assert len(inner.requests) == 2


def test_read_timeout_is_not_retried_for_a_post():
    # The server may already have created the build; replaying would duplicate it.
    client, inner, _ = _client([httpx.ReadTimeout("slow"), httpx.Response(201)])
    with pytest.raises(httpx.ReadTimeout):
        client.post("/builds", json={})
    assert len(inner.requests) == 1


def test_read_timeout_is_retried_for_a_get():
    client, inner, _ = _client([httpx.ReadTimeout("slow"), httpx.Response(200)])
    assert client.get("/project").status_code == 200
    assert len(inner.requests) == 2


def test_retrying_can_be_disabled():
    client, inner, _ = _client([httpx.Response(500), httpx.Response(200)], max_retries=0)
    assert client.get("/project").status_code == 500
    assert len(inner.requests) == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, False), (400, False), (404, False), (429, True), (500, True), (503, True)],
)
def test_should_retry_status(status: int, expected: bool):
    assert should_retry_status(status, RetryConfig()) is expected


@pytest.mark.parametrize(
    ("method", "error", "expected"),
    [
        ("GET", httpx.ReadTimeout("x"), True),
        ("PUT", httpx.ConnectError("x"), True),
        ("DELETE", httpx.PoolTimeout("x"), True),
        ("POST", httpx.ConnectTimeout("x"), True),
        ("POST", httpx.ReadTimeout("x"), False),
        ("PATCH", httpx.RemoteProtocolError("x"), False),
        ("GET", ValueError("unrelated"), False),
    ],
)
def test_should_retry_exception(method: str, error: Exception, expected: bool):
    assert should_retry_exception(error, method) is expected


def test_backoff_grows_exponentially_and_is_capped():
    config = RetryConfig(backoff_factor=0.5, max_backoff=2.0, jitter=False)
    delays = [backoff_delay(attempt, config, lambda: 1.0) for attempt in range(5)]
    assert delays == [0.5, 1.0, 2.0, 2.0, 2.0]


def test_jitter_never_exceeds_the_ceiling():
    config = RetryConfig(backoff_factor=1.0, max_backoff=4.0, jitter=True)
    assert backoff_delay(2, config, lambda: 1.0) == 4.0
    assert backoff_delay(2, config, lambda: 0.0) == 0.0


async def test_async_transport_retries_and_correlates_identically():
    inner = _AsyncRecordingTransport(
        [
            httpx.Response(500),
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200),
        ]
    )
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    transport = AsyncRetryTransport(
        inner, config=RetryConfig(jitter=False), sleep=fake_sleep, rand=lambda: 1.0
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.snapvisor.io/v2"
    ) as client:
        response = await client.get("/project")

    assert response.status_code == 200
    assert slept == [0.5, 2.0]
    assert len({r.headers[REQUEST_ID_HEADER] for r in inner.requests}) == 1
    assert [r.headers[RETRY_ATTEMPT_HEADER] for r in inner.requests] == ["0", "1", "2"]
