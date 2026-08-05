"""Error types raised by the Snapvisor SDK.

No silent failures: every failure path raises a :class:`SnapvisorError` (or a
subclass) carrying enough context — endpoint, HTTP status, request id, and the
server's error message *and* its per-field details — to diagnose the problem
without re-running with a debugger.

The Snapvisor API does not emit RFC 9457 ``application/problem+json``. Its error
body is ``{"error": str, "details"?: [{"message": str}]}``, and that is exactly
what :func:`error_from_response` parses.
"""

from __future__ import annotations

from typing import Any

import httpx

REQUEST_ID_HEADER = "x-argos-request-id"
RETRY_ATTEMPT_HEADER = "x-argos-retry-attempt"


class SnapvisorError(Exception):
    """Base class for every error raised by the Snapvisor SDK."""


class SnapvisorConfigError(SnapvisorError):
    """A required configuration value (token, branch, commit, directory) is missing or invalid."""


class SnapvisorAPIError(SnapvisorError):
    """The Snapvisor API returned a non-success HTTP response.

    Attributes:
        status_code: The HTTP status code returned by the API.
        method: The HTTP method of the failing request.
        url: The full URL of the failing request.
        message: The server-provided error message, when available.
        details: The server's per-field validation messages (``details[].message``),
            empty when the response carried none.
        request_id: The ``x-argos-request-id`` sent with the request, which the
            backend logs alongside its own error — quote it in bug reports.
    """

    def __init__(
        self,
        *,
        status_code: int,
        method: str,
        url: str,
        message: str | None,
        details: list[str] | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.url = url
        self.message = message
        self.details = list(details or [])
        self.request_id = request_id
        detail = f" — {message}" if message else ""
        if self.details:
            detail += " (" + "; ".join(self.details) + ")"
        if request_id:
            detail += f" [request id {request_id}]"
        super().__init__(
            f"Snapvisor API request failed: {method} {url} returned HTTP {status_code}{detail}"
        )


class SnapvisorAuthError(SnapvisorAPIError):
    """HTTP 401 — the token is missing, malformed, or not accepted.

    Most Snapvisor operations require a *personal access token*; only 8 of the 37
    accept a project token. A 401 on a read operation usually means a project
    token was used where a PAT is required.
    """


class SnapvisorForbiddenError(SnapvisorAPIError):
    """HTTP 403 — authenticated, but the token lacks the scope or the permission."""


class SnapvisorNotFoundError(SnapvisorAPIError):
    """HTTP 404 — the account, project, build, or comment does not exist."""


class SnapvisorConflictError(SnapvisorAPIError):
    """HTTP 409 — the request conflicts with the current state of the resource."""


class SnapvisorRateLimitError(SnapvisorAPIError):
    """HTTP 429 — the API rate limit was hit and the wait exceeded the SDK's ceiling.

    Attributes:
        limit: The window's quota, from the draft-8 ``RateLimit-Policy`` header.
        remaining: Requests left in the window, from the draft-8 ``RateLimit`` header.
        reset: Seconds until the window resets, from ``RateLimit`` or ``Retry-After``.
    """

    def __init__(
        self,
        *,
        status_code: int,
        method: str,
        url: str,
        message: str | None,
        details: list[str] | None = None,
        request_id: str | None = None,
        limit: int | None = None,
        remaining: int | None = None,
        reset: float | None = None,
    ) -> None:
        self.limit = limit
        self.remaining = remaining
        self.reset = reset
        super().__init__(
            status_code=status_code,
            method=method,
            url=url,
            message=message,
            details=details,
            request_id=request_id,
        )


class SnapvisorServerError(SnapvisorAPIError):
    """HTTP 5xx — the API failed. Retried automatically before this is raised."""


class SnapvisorUploadError(SnapvisorError):
    """A screenshot or trace file could not be uploaded to its storage target."""


_STATUS_TO_CLASS: dict[int, type[SnapvisorAPIError]] = {
    401: SnapvisorAuthError,
    403: SnapvisorForbiddenError,
    404: SnapvisorNotFoundError,
    409: SnapvisorConflictError,
    429: SnapvisorRateLimitError,
}


def parse_error_body(payload: Any) -> tuple[str | None, list[str]]:
    """Split a Snapvisor error body into ``(message, details)``.

    The backend renders each zod issue as ``"<path> is <message>"`` and puts them
    in ``details``; dropping them (as 0.1.0 did) loses the only per-field
    explanation of a validation failure.
    """
    if not isinstance(payload, dict):
        return None, []
    message = payload.get("error")
    message = message if isinstance(message, str) else None
    details: list[str] = []
    raw_details = payload.get("details")
    if isinstance(raw_details, list):
        for item in raw_details:
            if isinstance(item, dict) and isinstance(item.get("message"), str):
                details.append(item["message"])
            elif isinstance(item, str):
                details.append(item)
    return message, details


def error_from_response(response: httpx.Response) -> SnapvisorAPIError:
    """Build the most specific :class:`SnapvisorAPIError` for ``response``."""
    try:
        payload: Any = response.json()
    except ValueError:
        text = response.text.strip()
        message, details = (text or None), []
    else:
        message, details = parse_error_body(payload)

    request = response.request
    request_id = request.headers.get(REQUEST_ID_HEADER)
    status = response.status_code

    if status == 429:
        limit, remaining, reset = parse_rate_limit_headers(response.headers)
        return SnapvisorRateLimitError(
            status_code=status,
            method=request.method,
            url=str(request.url),
            message=message,
            details=details,
            request_id=request_id,
            limit=limit,
            remaining=remaining,
            reset=reset,
        )

    cls = _STATUS_TO_CLASS.get(status)
    if cls is None:
        cls = SnapvisorServerError if status >= 500 else SnapvisorAPIError
    return cls(
        status_code=status,
        method=request.method,
        url=str(request.url),
        message=message,
        details=details,
        request_id=request_id,
    )


def parse_rate_limit_headers(
    headers: httpx.Headers,
) -> tuple[int | None, int | None, float | None]:
    """Read IETF draft-8 rate-limit headers, falling back to ``Retry-After``.

    draft-8 (what ``express-rate-limit`` emits for this API, see
    ``apps/backend/src/web/api/v2.ts``) looks like::

        RateLimit-Policy: "default";q=100;w=60
        RateLimit: "default";r=0;t=42

    ``q`` is the quota, ``r`` the remaining requests, ``t`` the seconds until reset.
    """
    limit = _quoted_param(headers.get("ratelimit-policy"), "q")
    remaining = _quoted_param(headers.get("ratelimit"), "r")
    reset: float | None = _quoted_param(headers.get("ratelimit"), "t")

    if reset is None:
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                reset = float(retry_after.strip())
            except ValueError:
                reset = _http_date_delay(retry_after)
    return limit, remaining, reset


def _quoted_param(header: str | None, key: str) -> int | None:
    """Extract ``key=<int>`` from a draft-8 structured-field value."""
    if not header:
        return None
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name.strip() == key:
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def _http_date_delay(value: str) -> float | None:
    """Convert an HTTP-date ``Retry-After`` into seconds from now."""
    from email.utils import parsedate_to_datetime

    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    import datetime as _datetime

    now = _datetime.datetime.now(tz=target.tzinfo or _datetime.timezone.utc)
    return max(0.0, (target - now).total_seconds())
