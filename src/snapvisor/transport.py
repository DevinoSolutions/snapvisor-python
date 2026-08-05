"""Retry, rate-limit, and request-correlation policy for every Snapvisor request.

Implemented as an :mod:`httpx` transport so a *single* policy covers both the
handwritten uploader (:mod:`snapvisor.client`) and the generated API client
(:mod:`snapvisor._generated`) — there is no second code path that can miss it.

What it does, in order, per logical request:

1. Stamps ``x-argos-request-id`` (stable across every retry of the same request)
   so a Python failure is correlatable with the backend's own logs, and
   ``x-argos-retry-attempt`` (incrementing) so the backend can see a retry storm.
2. Retries transport failures (connect errors, timeouts) and 5xx responses with
   exponential backoff and full jitter. Non-idempotent methods are retried only
   where a retry cannot duplicate a completed side effect.
3. On 429, honours the IETF draft-8 ``RateLimit`` headers the API emits
   (``standardHeaders: "draft-8"`` in ``apps/backend/src/web/api/v2.ts``) and
   ``Retry-After``: it sleeps until the window resets and resumes, rather than
   failing a CI build. If the required wait exceeds ``max_rate_limit_wait`` the
   429 is surfaced as :class:`~snapvisor.errors.SnapvisorRateLimitError`.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from snapvisor.errors import REQUEST_ID_HEADER, RETRY_ATTEMPT_HEADER, parse_rate_limit_headers

# Transport-level failures that are always safe to retry for idempotent methods.
_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

# A retry of these can only ever repeat a request the server never completed.
_CONNECT_FAILURES: tuple[type[Exception], ...] = (httpx.ConnectError, httpx.ConnectTimeout)

# Methods whose repetition is safe by definition (RFC 9110 §9.2.2).
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


@dataclass(frozen=True)
class RetryConfig:
    """How the SDK retries failed requests.

    Defaults match the TypeScript SDK's ``p-retry({retries: 3})`` policy, plus
    rate-limit handling the TypeScript SDK does not have.

    Attributes:
        max_retries: Retries *after* the first attempt. ``0`` disables retrying.
        backoff_factor: Base delay in seconds; attempt *n* waits
            ``backoff_factor * 2**n``, with full jitter applied.
        max_backoff: Ceiling for a single backoff sleep, in seconds.
        max_rate_limit_wait: Longest the SDK will sleep for one 429 before
            giving up and raising. Set to ``0`` to never wait on 429.
        jitter: Whether to apply full jitter to the backoff (leave on outside tests).
        retry_status_codes: Extra status codes to treat as retryable. 5xx and 429
            are always retryable; 4xx never are.
    """

    max_retries: int = 3
    backoff_factor: float = 0.5
    max_backoff: float = 8.0
    max_rate_limit_wait: float = 60.0
    jitter: bool = True
    retry_status_codes: frozenset[int] = field(default_factory=frozenset)


def should_retry_status(status_code: int, config: RetryConfig) -> bool:
    """Whether a response status warrants another attempt."""
    if status_code == 429:
        return True
    if status_code >= 500:
        return True
    return status_code in config.retry_status_codes


def should_retry_exception(error: Exception, method: str) -> bool:
    """Whether a transport failure warrants another attempt for ``method``."""
    if not isinstance(error, _RETRYABLE_EXCEPTIONS):
        return False
    if method.upper() in _IDEMPOTENT_METHODS:
        return True
    # A POST/PATCH that failed *after* the bytes went out may already have been
    # applied server-side; only replay when the connection never got established.
    return isinstance(error, _CONNECT_FAILURES)


def backoff_delay(attempt: int, config: RetryConfig, rand: Callable[[], float]) -> float:
    """Full-jitter exponential backoff for the given zero-based attempt."""
    ceiling = min(config.max_backoff, config.backoff_factor * (2**attempt))
    return ceiling * rand() if config.jitter else ceiling


def rate_limit_delay(response: httpx.Response, config: RetryConfig) -> float | None:
    """Seconds to wait before retrying a 429, or ``None`` if we should give up."""
    _, _, reset = parse_rate_limit_headers(response.headers)
    # No headers at all: fall back to plain backoff rather than assuming.
    wait = reset if reset is not None else config.backoff_factor
    if wait > config.max_rate_limit_wait:
        return None
    return max(0.0, wait)


def _prepare(request: httpx.Request, attempt: int) -> None:
    """Stamp correlation headers; the request id is generated once and reused."""
    if REQUEST_ID_HEADER not in request.headers:
        request.headers[REQUEST_ID_HEADER] = uuid.uuid4().hex
    request.headers[RETRY_ATTEMPT_HEADER] = str(attempt)


class RetryTransport(httpx.BaseTransport):
    """Synchronous retrying transport. Wraps any other :class:`httpx.BaseTransport`."""

    def __init__(
        self,
        next_transport: httpx.BaseTransport | None = None,
        *,
        config: RetryConfig | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._next = next_transport if next_transport is not None else httpx.HTTPTransport()
        self._config = config or RetryConfig()
        self._sleep = sleep
        self._rand = rand

    @property
    def config(self) -> RetryConfig:
        return self._config

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        config = self._config
        last_error: Exception | None = None
        for attempt in range(config.max_retries + 1):
            _prepare(request, attempt)
            is_last = attempt == config.max_retries
            try:
                response = self._next.handle_request(request)
            except Exception as error:  # noqa: BLE001 - re-raised below when not retryable
                if is_last or not should_retry_exception(error, request.method):
                    raise
                last_error = error
                self._sleep(backoff_delay(attempt, config, self._rand))
                continue

            if not should_retry_status(response.status_code, config):
                return response

            delay = _delay_for_response(response, attempt, config, self._rand)
            if is_last or delay is None:
                return response
            response.read()
            response.close()
            self._sleep(delay)

        raise last_error or httpx.TransportError("retries exhausted")  # pragma: no cover

    def close(self) -> None:
        self._next.close()


class AsyncRetryTransport(httpx.AsyncBaseTransport):
    """Asynchronous twin of :class:`RetryTransport`, with identical semantics."""

    def __init__(
        self,
        next_transport: httpx.AsyncBaseTransport | None = None,
        *,
        config: RetryConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._next = next_transport if next_transport is not None else httpx.AsyncHTTPTransport()
        self._config = config or RetryConfig()
        self._sleep = sleep or asyncio.sleep
        self._rand = rand

    @property
    def config(self) -> RetryConfig:
        return self._config

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        config = self._config
        last_error: Exception | None = None
        for attempt in range(config.max_retries + 1):
            _prepare(request, attempt)
            is_last = attempt == config.max_retries
            try:
                response = await self._next.handle_async_request(request)
            except Exception as error:  # noqa: BLE001 - re-raised below when not retryable
                if is_last or not should_retry_exception(error, request.method):
                    raise
                last_error = error
                await self._sleep(backoff_delay(attempt, config, self._rand))
                continue

            if not should_retry_status(response.status_code, config):
                return response

            delay = _delay_for_response(response, attempt, config, self._rand)
            if is_last or delay is None:
                return response
            await response.aread()
            await response.aclose()
            await self._sleep(delay)

        raise last_error or httpx.TransportError("retries exhausted")  # pragma: no cover

    async def aclose(self) -> None:
        await self._next.aclose()


def _delay_for_response(
    response: httpx.Response,
    attempt: int,
    config: RetryConfig,
    rand: Callable[[], float],
) -> float | None:
    """Seconds to wait before retrying ``response``, or ``None`` to stop retrying."""
    if response.status_code == 429:
        return rate_limit_delay(response, config)
    return backoff_delay(attempt, config, rand)
