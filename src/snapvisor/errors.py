"""Error types raised by the Snapvisor SDK.

No silent failures: every failure path raises a :class:`SnapvisorError` (or a
subclass) carrying enough context — endpoint, HTTP status, and the server's
error message — to diagnose the problem without re-running with a debugger.
"""

from __future__ import annotations


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
    """

    def __init__(
        self,
        *,
        status_code: int,
        method: str,
        url: str,
        message: str | None,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.url = url
        self.message = message
        detail = f" — {message}" if message else ""
        super().__init__(
            f"Snapvisor API request failed: {method} {url} returned HTTP {status_code}{detail}"
        )


class SnapvisorUploadError(SnapvisorError):
    """A screenshot file could not be uploaded to its storage target."""
