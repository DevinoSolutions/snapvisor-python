"""Thin typed HTTP client over the Snapvisor v2 API.

Wraps :mod:`httpx`. Authenticates with ``Authorization: Bearer <token>`` (the
scheme the API declares for its ``projectToken`` security scheme) and turns every
non-success API response into a loud :class:`SnapvisorAPIError`.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from snapvisor.errors import SnapvisorAPIError, SnapvisorUploadError

DEFAULT_API_BASE_URL = "https://api.snapvisor.io/v2/"

# Upload targets are signed for a bounded lifetime; keep a generous per-file timeout.
_UPLOAD_TIMEOUT = 60.0
_API_TIMEOUT = 30.0


class SnapvisorClient:
    """Authenticated client for the Snapvisor REST API and its storage targets."""

    def __init__(
        self,
        *,
        token: str,
        api_base_url: str | None = None,
        sdk_identifier: str,
    ) -> None:
        base = (api_base_url or DEFAULT_API_BASE_URL).rstrip("/") + "/"
        self._http = httpx.Client(
            base_url=base,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": sdk_identifier,
            },
            timeout=_API_TIMEOUT,
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
    ) -> dict[str, Any]:
        """Send an API request and return the decoded JSON object.

        Raises:
            SnapvisorAPIError: If the response status is not 2xx.
        """
        relative = path.lstrip("/")
        response = self._http.request(method, relative, json=json_body)
        if not response.is_success:
            raise SnapvisorAPIError(
                status_code=response.status_code,
                method=method,
                url=str(response.request.url),
                message=_extract_api_error(response),
            )
        data = response.json()
        if not isinstance(data, dict):
            raise SnapvisorAPIError(
                status_code=response.status_code,
                method=method,
                url=str(response.request.url),
                message=f"Expected a JSON object, got {type(data).__name__}",
            )
        return data

    def upload_target(
        self,
        target: dict[str, Any],
        *,
        file_path: Path,
        content_type: str,
    ) -> None:
        """Upload one file to the storage target returned by ``createBuild``.

        Handles both target shapes the API declares:

        * ``postUrl`` + ``fields``: a presigned/proxied POST — the file is sent
          as ``multipart/form-data`` with the policy fields appended *before* the
          ``file`` part. This is the shape prod returns (backend-proxied uploads,
          ``S3_SUPPORTS_PRESIGNED_POST=false``), and also S3 presigned POST.
        * ``putUrl``: the deprecated presigned PUT — the raw bytes are sent with
          a ``Content-Type`` header.

        Raises:
            SnapvisorUploadError: If the file cannot be read or the upload is rejected.
        """
        try:
            body = file_path.read_bytes()
        except OSError as error:
            raise SnapvisorUploadError(f"Failed to read screenshot {file_path}: {error}") from error

        if "postUrl" in target:
            url = str(target["postUrl"])
            fields = target.get("fields") or {}
            # Policy fields must precede the file part or the storage backend
            # rejects the POST.
            data = {str(k): str(v) for k, v in fields.items()}
            files = {"file": (file_path.name, body, content_type)}
            response = self._http.post(url, data=data, files=files, timeout=_UPLOAD_TIMEOUT)
        elif "putUrl" in target:
            url = str(target["putUrl"])
            response = self._http.put(
                url,
                content=body,
                headers={"Content-Type": content_type},
                timeout=_UPLOAD_TIMEOUT,
            )
        else:
            raise SnapvisorUploadError(
                f"Upload target for key {target.get('key')!r} has neither "
                f"'postUrl' nor 'putUrl'; cannot upload {file_path}"
            )

        if not response.is_success:
            raise SnapvisorUploadError(
                f"Failed to upload {file_path} to {url}: HTTP "
                f"{response.status_code} — {_extract_storage_error(response)}"
            )


def _extract_api_error(response: httpx.Response) -> str | None:
    """Pull the ``error`` message out of a Snapvisor JSON error body."""
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str):
            return error
    return None


def _extract_storage_error(response: httpx.Response) -> str:
    """Best-effort human-readable reason from a storage-backend error response."""
    text = response.text.strip()
    return text[:500] if text else response.reason_phrase or "unknown error"
