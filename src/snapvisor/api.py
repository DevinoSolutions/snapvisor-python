"""The :class:`Snapvisor` API facade — every operation the platform publishes.

0.1.0 could reach 2 of the API's 37 operations, and both only partially. This
module closes that gap *structurally* rather than by hand: resource namespaces
are built at import time from :mod:`snapvisor._operations`, which
``scripts/regen.py`` derives from the OpenAPI document. Adding a backend
operation makes it callable here by regenerating — there is no hand-maintained
list of call sites to fall behind.

    >>> from snapvisor import Snapvisor
    >>> sv = Snapvisor(token="snapvisor_pat_…")
    >>> me = sv.users.get_me()
    >>> for build in sv.builds.auto_paginate("listBuilds", owner="acme", project="web"):
    ...     print(build.number, build.status)

**Auth matters here.** Only 8 of the 37 operations accept a *project token*; the
other 29 need a *personal access token* (or an OAuth 2.1 access token). Passing
a project token to, say, ``listComments`` returns 401 —
:class:`~snapvisor.errors.SnapvisorAuthError` says so explicitly. Use
:meth:`Snapvisor.from_env`, which prefers ``SNAPVISOR_PAT`` for exactly this
reason.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from types import ModuleType, TracebackType
from typing import Any

import httpx

from snapvisor._generated import AuthenticatedClient
from snapvisor._operations import OPERATIONS, OperationInfo
from snapvisor.client import (
    DEFAULT_API_BASE_URL,
    auth_headers,
    normalise_base_url,
)
from snapvisor.config import env
from snapvisor.errors import SnapvisorConfigError, error_from_response
from snapvisor.pagination import Page, aauto_paginate, aiter_pages, auto_paginate, iter_pages
from snapvisor.transport import AsyncRetryTransport, RetryConfig, RetryTransport

_API_TIMEOUT = 30.0

# operationId -> OperationInfo, plus python_name -> OperationInfo, so callers can
# use either the spec's name or the Pythonic one.
_BY_ANY_NAME: dict[str, OperationInfo] = {}
for _info in OPERATIONS.values():
    _BY_ANY_NAME[_info.operation_id] = _info
    _BY_ANY_NAME[_info.python_name] = _info


def _load(info: OperationInfo) -> ModuleType:
    return importlib.import_module(info.module)


def _raise_for_api_error(response: httpx.Response) -> None:
    """httpx response hook: turn any non-2xx API response into a typed exception.

    Installed on the API client only. The uploader's storage-target requests go
    to S3/B2 through a separate client and keep raising
    :class:`~snapvisor.errors.SnapvisorUploadError`.
    """
    if response.is_success:
        return
    response.read()
    raise error_from_response(response)


def _resolve_token(token: str | None) -> str:
    resolved = token or env("PAT") or env("TOKEN")
    if not resolved:
        raise SnapvisorConfigError(
            "Missing Snapvisor token. Pass token=... or set SNAPVISOR_PAT "
            "(personal access token, required by 29 of the 37 operations) or "
            "SNAPVISOR_TOKEN / ARGOS_TOKEN (project token)."
        )
    return resolved


class Resource:
    """One tag's worth of operations, dispatched by name.

    Attribute access resolves against the generated operation registry, so
    ``sv.builds.list_builds(...)`` calls
    ``snapvisor._generated.api.builds.list_builds.sync_detailed`` with the
    facade's client injected and the response unwrapped to its parsed model.
    Both the OpenAPI ``operationId`` (``listBuilds``) and the Python name
    (``list_builds``) resolve.
    """

    def __init__(self, namespace: str, client: _BaseSnapvisor) -> None:
        mine = [info for info in OPERATIONS.values() if info.namespace == namespace]
        self._namespace = namespace
        self._client = client
        self.operations: dict[str, OperationInfo] = {info.python_name: info for info in mine}
        # Both naming conventions resolve: the spec's operationId and the Python name.
        self._aliases: dict[str, OperationInfo] = {
            **self.operations,
            **{info.operation_id: info for info in mine},
        }

    def __dir__(self) -> list[str]:
        return sorted({*super().__dir__(), *self._aliases})

    def __repr__(self) -> str:
        return f"<Resource {self._namespace} ({len(self.operations)} operations)>"

    def __getattr__(self, name: str) -> Callable[..., Any]:
        # Only reached when normal attribute lookup fails, so `_aliases` may be
        # absent during construction/unpickling — never recurse into it.
        aliases = self.__dict__.get("_aliases")
        if aliases is None or name.startswith("__"):
            raise AttributeError(name)
        info = aliases.get(name)
        if info is None:
            raise AttributeError(
                f"{self.__dict__.get('_namespace')!r} has no operation {name!r}. "
                f"Available: {', '.join(sorted(self.__dict__.get('operations', {})))}"
            )
        return self._client._bind(info)

    def _fetcher(self, operation: str) -> Callable[..., Any]:
        info = self._aliases.get(operation)
        if info is None:
            raise AttributeError(f"{self._namespace!r} has no operation {operation!r}")
        return self._client._bind(info)


class SyncResource(Resource):
    """A resource on the synchronous facade, with pagination helpers."""

    def iter_pages(self, operation: str, **kwargs: Any) -> Any:
        """Yield :class:`~snapvisor.pagination.Page` objects for a list operation."""
        return iter_pages(self._fetcher(operation), **kwargs)

    def auto_paginate(self, operation: str, **kwargs: Any) -> Any:
        """Yield every item of a list operation, walking pages transparently."""
        return auto_paginate(self._fetcher(operation), **kwargs)

    def page(self, operation: str, **kwargs: Any) -> Page[Any]:
        """Fetch a single page of a list operation as a :class:`Page`."""
        from snapvisor.pagination import to_page

        return to_page(self._fetcher(operation)(**kwargs))


class AsyncResource(Resource):
    """A resource on the asynchronous facade, with pagination helpers."""

    def iter_pages(self, operation: str, **kwargs: Any) -> Any:
        """Asynchronously yield :class:`~snapvisor.pagination.Page` objects."""
        return aiter_pages(self._fetcher(operation), **kwargs)

    def auto_paginate(self, operation: str, **kwargs: Any) -> Any:
        """Asynchronously yield every item of a list operation."""
        return aauto_paginate(self._fetcher(operation), **kwargs)

    async def page(self, operation: str, **kwargs: Any) -> Page[Any]:
        """Fetch a single page of a list operation as a :class:`Page`."""
        from snapvisor.pagination import to_page

        return to_page(await self._fetcher(operation)(**kwargs))


class _BaseSnapvisor:
    """Shared construction and namespace wiring for both facades."""

    _resource_class: type[Resource] = Resource

    def __init__(self, sdk_identifier: str | None = None) -> None:
        from snapvisor import __version__

        self.sdk_identifier = sdk_identifier or f"snapvisor-python/{__version__}"
        self._namespaces: dict[str, Resource] = {}

    def _wire_namespaces(self) -> None:
        for info in OPERATIONS.values():
            if info.namespace not in self._namespaces:
                self._namespaces[info.namespace] = self._resource_class(info.namespace, self)

    def __getattr__(self, name: str) -> Resource:
        namespaces = self.__dict__.get("_namespaces", {})
        if name in namespaces:
            return namespaces[name]
        raise AttributeError(
            f"{type(self).__name__} has no attribute {name!r}. "
            f"Namespaces: {', '.join(sorted(namespaces))}"
        )

    def __dir__(self) -> list[str]:
        return sorted({*super().__dir__(), *self.__dict__.get("_namespaces", {})})

    @property
    def namespaces(self) -> tuple[str, ...]:
        """The resource namespaces this facade exposes."""
        return tuple(sorted(self._namespaces))

    def _bind(self, info: OperationInfo) -> Callable[..., Any]:  # pragma: no cover - overridden
        raise NotImplementedError


class Snapvisor(_BaseSnapvisor):
    """Synchronous client for the whole Snapvisor API.

    Args:
        token: A personal access token (preferred — 29 of 37 operations require
            one) or a project token. Falls back to ``SNAPVISOR_PAT``, then
            ``SNAPVISOR_TOKEN``/``ARGOS_TOKEN``.
        api_base_url: API base URL. Falls back to ``SNAPVISOR_API_BASE_URL`` /
            ``ARGOS_API_BASE_URL``, then the public API.
        retry: Retry, backoff, and rate-limit policy. Defaults to 3 retries with
            draft-8 ``RateLimit`` header handling.
        sdk_identifier: Value sent as ``User-Agent``.

    Raises:
        SnapvisorConfigError: If no token can be resolved.
    """

    _resource_class = SyncResource

    def __init__(
        self,
        token: str | None = None,
        *,
        api_base_url: str | None = None,
        retry: RetryConfig | None = None,
        sdk_identifier: str | None = None,
    ) -> None:
        super().__init__(sdk_identifier)
        resolved_token = _resolve_token(token)
        base_url = normalise_base_url(api_base_url or env("API_BASE_URL") or DEFAULT_API_BASE_URL)
        self._http = httpx.Client(
            base_url=base_url,
            headers=auth_headers(resolved_token, self.sdk_identifier),
            timeout=_API_TIMEOUT,
            transport=RetryTransport(config=retry or RetryConfig()),
            event_hooks={"response": [_raise_for_api_error]},
        )
        self._generated = AuthenticatedClient(
            base_url=base_url.rstrip("/"),
            token=resolved_token,
            raise_on_unexpected_status=True,
        ).set_httpx_client(self._http)
        self._wire_namespaces()

    @classmethod
    def from_env(cls, **kwargs: Any) -> Snapvisor:
        """Build a client from ``SNAPVISOR_PAT`` / ``SNAPVISOR_TOKEN`` / ``ARGOS_TOKEN``."""
        return cls(**kwargs)

    @property
    def raw(self) -> AuthenticatedClient:
        """The underlying generated client, for calling operation modules directly.

        Use it when you want the generated function's full static signature::

            from snapvisor.operations import list_builds
            response = list_builds.sync_detailed(owner="acme", project="web", client=sv.raw)
        """
        return self._generated

    @property
    def http(self) -> httpx.Client:
        """The underlying :class:`httpx.Client`, retry transport included."""
        return self._http

    def _bind(self, info: OperationInfo) -> Callable[..., Any]:
        module = _load(info)

        def call(*args: Any, **kwargs: Any) -> Any:
            response = module.sync_detailed(*args, client=self._generated, **kwargs)
            return response.parsed

        call.__name__ = info.python_name
        call.__qualname__ = f"Snapvisor.{info.namespace}.{info.python_name}"
        call.__doc__ = (
            f"{info.operation_id} — {info.method} {info.path}.\n\n"
            f"Generated wrapper around {info.module}. Returns the parsed response "
            f"model; non-2xx responses raise a SnapvisorAPIError subclass."
        )
        return call

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Snapvisor:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class AsyncSnapvisor(_BaseSnapvisor):
    """Asynchronous twin of :class:`Snapvisor`.

    Every operation returns a coroutine; the retry, rate-limit, and error
    semantics are identical.

        >>> async with AsyncSnapvisor() as sv:
        ...     me = await sv.users.get_me()
    """

    _resource_class = AsyncResource

    def __init__(
        self,
        token: str | None = None,
        *,
        api_base_url: str | None = None,
        retry: RetryConfig | None = None,
        sdk_identifier: str | None = None,
    ) -> None:
        super().__init__(sdk_identifier)
        resolved_token = _resolve_token(token)
        base_url = normalise_base_url(api_base_url or env("API_BASE_URL") or DEFAULT_API_BASE_URL)
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers=auth_headers(resolved_token, self.sdk_identifier),
            timeout=_API_TIMEOUT,
            transport=AsyncRetryTransport(config=retry or RetryConfig()),
            event_hooks={"response": [_araise_for_api_error]},
        )
        self._generated = AuthenticatedClient(
            base_url=base_url.rstrip("/"),
            token=resolved_token,
            raise_on_unexpected_status=True,
        ).set_async_httpx_client(self._http)
        self._wire_namespaces()

    @classmethod
    def from_env(cls, **kwargs: Any) -> AsyncSnapvisor:
        """Build a client from ``SNAPVISOR_PAT`` / ``SNAPVISOR_TOKEN`` / ``ARGOS_TOKEN``."""
        return cls(**kwargs)

    @property
    def raw(self) -> AuthenticatedClient:
        """The underlying generated client, for calling operation modules directly."""
        return self._generated

    @property
    def http(self) -> httpx.AsyncClient:
        """The underlying :class:`httpx.AsyncClient`, retry transport included."""
        return self._http

    def _bind(self, info: OperationInfo) -> Callable[..., Awaitable[Any]]:
        module = _load(info)

        async def call(*args: Any, **kwargs: Any) -> Any:
            response = await module.asyncio_detailed(*args, client=self._generated, **kwargs)
            return response.parsed

        call.__name__ = info.python_name
        call.__qualname__ = f"AsyncSnapvisor.{info.namespace}.{info.python_name}"
        call.__doc__ = (
            f"{info.operation_id} — {info.method} {info.path}.\n\n"
            f"Generated async wrapper around {info.module}. Returns the parsed "
            f"response model; non-2xx responses raise a SnapvisorAPIError subclass."
        )
        return call

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncSnapvisor:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


async def _araise_for_api_error(response: httpx.Response) -> None:
    """Async twin of :func:`_raise_for_api_error`."""
    if response.is_success:
        return
    await response.aread()
    raise error_from_response(response)
