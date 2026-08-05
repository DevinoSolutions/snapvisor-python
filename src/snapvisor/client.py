"""Thin typed HTTP client over the Snapvisor v2 API and its storage targets.

Wraps :mod:`httpx`. Authenticates with ``Authorization: Bearer <token>`` (the
scheme every one of the API's three security schemes declares) and turns each
non-success API response into the most specific
:class:`~snapvisor.errors.SnapvisorAPIError` subclass, carrying the server's
``details[]`` and the request id.

Retries, 429 handling, and request correlation live in
:mod:`snapvisor.transport` so the generated client shares them.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from snapvisor.errors import SnapvisorAPIError, SnapvisorUploadError, error_from_response
from snapvisor.transport import AsyncRetryTransport, RetryConfig, RetryTransport

DEFAULT_API_BASE_URL = "https://api.snapvisor.io/v2/"

# Upload targets are signed for a bounded lifetime; keep a generous per-file timeout.
_UPLOAD_TIMEOUT = 60.0
_API_TIMEOUT = 30.0


def normalise_base_url(api_base_url: str | None) -> str:
    """Return a base URL with exactly one trailing slash."""
    return (api_base_url or DEFAULT_API_BASE_URL).rstrip("/") + "/"


def auth_headers(token: str, sdk_identifier: str) -> dict[str, str]:
    """The headers every Snapvisor request carries."""
    return {"Authorization": f"Bearer {token}", "User-Agent": sdk_identifier}


def build_httpx_client(
    *,
    token: str,
    api_base_url: str | None,
    sdk_identifier: str,
    retry: RetryConfig | None = None,
) -> httpx.Client:
    """Build the shared, retrying :class:`httpx.Client` used across the SDK."""
    return httpx.Client(
        base_url=normalise_base_url(api_base_url),
        headers=auth_headers(token, sdk_identifier),
        timeout=_API_TIMEOUT,
        transport=RetryTransport(config=retry or RetryConfig()),
    )


def build_async_httpx_client(
    *,
    token: str,
    api_base_url: str | None,
    sdk_identifier: str,
    retry: RetryConfig | None = None,
) -> httpx.AsyncClient:
    """Asynchronous twin of :func:`build_httpx_client`."""
    return httpx.AsyncClient(
        base_url=normalise_base_url(api_base_url),
        headers=auth_headers(token, sdk_identifier),
        timeout=_API_TIMEOUT,
        transport=AsyncRetryTransport(config=retry or RetryConfig()),
    )


def _target_request(
    target: dict[str, Any],
    *,
    file_path: Path,
    body: bytes,
    content_type: str,
) -> tuple[str, dict[str, Any]]:
    """Translate an upload target into ``(url, httpx request kwargs)``.

    Handles both target shapes the API declares:

    * ``postUrl`` + ``fields``: a presigned/proxied POST — the file is sent as
      ``multipart/form-data`` with the policy fields appended *before* the
      ``file`` part. This is the shape prod returns (backend-proxied uploads,
      ``S3_SUPPORTS_PRESIGNED_POST=false`` because Backblaze B2 answers 501 to
      S3 presigned POST), and also plain S3 presigned POST.
    * ``putUrl``: the deprecated presigned PUT — raw bytes with a
      ``Content-Type`` header.

    Raises:
        SnapvisorUploadError: If the target declares neither shape.
    """
    if "postUrl" in target:
        fields = target.get("fields") or {}
        # Policy fields must precede the file part or the storage backend
        # rejects the POST.
        return str(target["postUrl"]), {
            "method": "POST",
            "data": {str(k): str(v) for k, v in fields.items()},
            "files": {"file": (file_path.name, body, content_type)},
        }
    if "putUrl" in target:
        return str(target["putUrl"]), {
            "method": "PUT",
            "content": body,
            "headers": {"Content-Type": content_type},
        }
    raise SnapvisorUploadError(
        f"Upload target for key {target.get('key')!r} has neither "
        f"'postUrl' nor 'putUrl'; cannot upload {file_path}"
    )


def _read_bytes(file_path: Path) -> bytes:
    try:
        return file_path.read_bytes()
    except OSError as error:
        raise SnapvisorUploadError(f"Failed to read file {file_path}: {error}") from error


def _check_upload(response: httpx.Response, file_path: Path, url: str) -> None:
    if not response.is_success:
        raise SnapvisorUploadError(
            f"Failed to upload {file_path} to {url}: HTTP "
            f"{response.status_code} — {_extract_storage_error(response)}"
        )


def _check_json(response: httpx.Response, method: str) -> dict[str, Any]:
    if not response.is_success:
        raise error_from_response(response)
    data = response.json()
    if not isinstance(data, dict):
        raise SnapvisorAPIError(
            status_code=response.status_code,
            method=method,
            url=str(response.request.url),
            message=f"Expected a JSON object, got {type(data).__name__}",
        )
    return data


class SnapvisorClient:
    """Authenticated client for the Snapvisor REST API and its storage targets.

    Thread-safe: the underlying :class:`httpx.Client` is, which is what lets
    :func:`snapvisor.upload` upload screenshots concurrently.
    """

    def __init__(
        self,
        *,
        token: str,
        api_base_url: str | None = None,
        sdk_identifier: str,
        retry: RetryConfig | None = None,
    ) -> None:
        self._http = build_httpx_client(
            token=token,
            api_base_url=api_base_url,
            sdk_identifier=sdk_identifier,
            retry=retry,
        )

    def __enter__(self) -> SnapvisorClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send an API request and return the decoded JSON object.

        Raises:
            SnapvisorAPIError: If the response status is not 2xx. The concrete
                subclass reflects the status (401 → ``SnapvisorAuthError`` and
                so on).
        """
        response = self._http.request(method, path.lstrip("/"), json=json_body, params=params)
        return _check_json(response, method)

    def upload_target(
        self,
        target: dict[str, Any],
        *,
        file_path: Path,
        content_type: str,
    ) -> None:
        """Upload one file to a storage target returned by ``createBuild``.

        Raises:
            SnapvisorUploadError: If the file cannot be read or the upload is rejected.
        """
        body = _read_bytes(file_path)
        url, kwargs = _target_request(
            target, file_path=file_path, body=body, content_type=content_type
        )
        method = kwargs.pop("method")
        response = self._http.request(method, url, timeout=_UPLOAD_TIMEOUT, **kwargs)
        _check_upload(response, file_path, url)


class AsyncSnapvisorClient:
    """Asynchronous twin of :class:`SnapvisorClient`, with identical semantics."""

    def __init__(
        self,
        *,
        token: str,
        api_base_url: str | None = None,
        sdk_identifier: str,
        retry: RetryConfig | None = None,
    ) -> None:
        self._http = build_async_httpx_client(
            token=token,
            api_base_url=api_base_url,
            sdk_identifier=sdk_identifier,
            retry=retry,
        )

    async def __aenter__(self) -> AsyncSnapvisorClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Asynchronous twin of :meth:`SnapvisorClient.request_json`."""
        response = await self._http.request(method, path.lstrip("/"), json=json_body, params=params)
        return _check_json(response, method)

    async def upload_target(
        self,
        target: dict[str, Any],
        *,
        file_path: Path,
        content_type: str,
    ) -> None:
        """Asynchronous twin of :meth:`SnapvisorClient.upload_target`."""
        body = _read_bytes(file_path)
        url, kwargs = _target_request(
            target, file_path=file_path, body=body, content_type=content_type
        )
        method = kwargs.pop("method")
        response = await self._http.request(method, url, timeout=_UPLOAD_TIMEOUT, **kwargs)
        _check_upload(response, file_path, url)


def _extract_storage_error(response: httpx.Response) -> str:
    """Best-effort human-readable reason from a storage-backend error response."""
    text = response.text.strip()
    return text[:500] if text else response.reason_phrase or "unknown error"
