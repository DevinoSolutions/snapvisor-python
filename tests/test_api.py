"""The Snapvisor facade against a mocked API: dispatch, typing, errors, pagination."""

from __future__ import annotations

import httpx
import pytest
import respx

from snapvisor import AsyncSnapvisor, Snapvisor
from snapvisor.errors import (
    REQUEST_ID_HEADER,
    SnapvisorAuthError,
    SnapvisorConfigError,
    SnapvisorForbiddenError,
    SnapvisorNotFoundError,
)
from snapvisor.models import Build, Me
from snapvisor.transport import RetryConfig

API = "https://api.snapvisor.io/v2"

# Minimal payloads that satisfy the generated models' required fields — the
# point of the typed layer is that an incomplete payload fails loudly here
# rather than silently producing a half-populated dict.
BUILD = {
    "id": "build-1",
    "number": 42,
    "status": "accepted",
    "url": "https://app.snapvisor.io/acme/web/builds/42",
    "head": {"sha": "a" * 40, "branch": "main"},
    "base": None,
    "conclusion": None,
    "stats": None,
    "metadata": None,
    "notification": None,
}

ME = {
    "user": {"id": "u1", "name": "Aladdin", "email": "aladdin@devino.ca"},
    "accounts": [],
    "github": {"connected": True, "login": "aladdin"},
}


def _page(page: int, per_page: int, total: int) -> dict:
    start = (page - 1) * per_page
    return {
        "results": [
            {**BUILD, "id": f"build-{index}", "number": index}
            for index in range(start, min(start + per_page, total))
        ],
        "pageInfo": {"total": total, "page": page, "perPage": per_page},
    }


@pytest.fixture
def client():
    sv = Snapvisor(token="pat", retry=RetryConfig(max_retries=0))
    yield sv
    sv.close()


@respx.mock
def test_get_build_returns_a_typed_model(client):
    respx.get(f"{API}/projects/acme/web/builds/42").mock(
        return_value=httpx.Response(200, json=BUILD)
    )
    build = client.builds.get_build(owner="acme", project="web", build_number="42")

    assert isinstance(build, Build), "0.1.0 returned dict[str, Any] with no autocomplete"
    assert build.number == 42
    assert build.url == BUILD["url"]


@respx.mock
def test_operation_id_and_python_name_dispatch_to_the_same_endpoint(client):
    route = respx.get(f"{API}/projects/acme/web/builds/42").mock(
        return_value=httpx.Response(200, json=BUILD)
    )
    client.builds.get_build(owner="acme", project="web", build_number="42")
    client.builds.getBuild(owner="acme", project="web", build_number="42")
    assert route.call_count == 2


@respx.mock
def test_get_me_reaches_the_users_namespace(client):
    respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json=ME))
    me = client.users.get_me()
    assert isinstance(me, Me)
    assert (me.user.id, me.user.email) == ("u1", "aladdin@devino.ca")
    assert me.github.login == "aladdin"


@respx.mock
def test_auto_paginate_walks_every_page(client):
    route = respx.get(f"{API}/projects/acme/web/builds").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=_page(
                int(request.url.params.get("page", "1")),
                int(request.url.params.get("perPage", "30")),
                7,
            ),
        )
    )
    builds = list(
        client.builds.auto_paginate("listBuilds", owner="acme", project="web", per_page=3)
    )
    assert len(builds) == 7
    assert route.call_count == 3
    assert all(isinstance(build, Build) for build in builds)


@respx.mock
def test_page_returns_one_page_with_its_metadata(client):
    respx.get(f"{API}/projects/acme/web/builds").mock(
        return_value=httpx.Response(200, json=_page(1, 2, 5))
    )
    page = client.builds.page("listBuilds", owner="acme", project="web", per_page=2)
    assert (page.page, page.per_page, page.total) == (1, 2, 5)
    assert page.has_more is True


@respx.mock
def test_api_errors_are_raised_as_typed_exceptions(client):
    respx.get(f"{API}/projects/acme/web/builds/42").mock(
        return_value=httpx.Response(404, json={"error": "Build not found"})
    )
    with pytest.raises(SnapvisorNotFoundError) as exc:
        client.builds.get_build(owner="acme", project="web", build_number="42")
    assert exc.value.status_code == 404
    assert "Build not found" in str(exc.value)


@respx.mock
def test_a_project_token_on_a_pat_only_operation_raises_an_auth_error(client):
    # Only 8 of the 37 operations accept a project token; the SDK must say so
    # with a distinct exception rather than a generic API error.
    respx.get(f"{API}/projects/acme/web/builds/42/comments").mock(
        return_value=httpx.Response(401, json={"error": "Personal access token required"})
    )
    with pytest.raises(SnapvisorAuthError):
        client.comments.list_comments(owner="acme", project="web", build_number="42")


@respx.mock
def test_every_request_carries_auth_and_correlation_headers(client):
    route = respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json=ME))
    client.users.get_me()
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer pat"
    assert request.headers["user-agent"].startswith("snapvisor-python/")
    assert request.headers[REQUEST_ID_HEADER]


@respx.mock
def test_retries_apply_to_generated_operations_too(client):
    sv = Snapvisor(token="pat", retry=RetryConfig(max_retries=2, backoff_factor=0, jitter=False))
    route = respx.get(f"{API}/projects/acme/web/builds/42").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json=BUILD),
        ]
    )
    build = sv.builds.get_build(owner="acme", project="web", build_number="42")
    assert build.number == 42
    assert route.call_count == 2
    sv.close()


def test_missing_token_raises_a_config_error_naming_the_pat_variable():
    with pytest.raises(SnapvisorConfigError, match="SNAPVISOR_PAT"):
        Snapvisor()


def test_token_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SNAPVISOR_PAT", "from-env")
    sv = Snapvisor()
    assert sv.http.headers["authorization"] == "Bearer from-env"
    sv.close()


def test_argos_token_still_works(monkeypatch):
    monkeypatch.setenv("ARGOS_TOKEN", "legacy")
    sv = Snapvisor()
    assert sv.http.headers["authorization"] == "Bearer legacy"
    sv.close()


@respx.mock
async def test_async_facade_returns_the_same_models():
    respx.get(f"{API}/projects/acme/web/builds/42").mock(
        return_value=httpx.Response(200, json=BUILD)
    )
    async with AsyncSnapvisor(token="pat") as sv:
        build = await sv.builds.get_build(owner="acme", project="web", build_number="42")
    assert isinstance(build, Build)
    assert build.number == 42


@respx.mock
async def test_async_facade_paginates():
    respx.get(f"{API}/projects/acme/web/builds").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=_page(
                int(request.url.params.get("page", "1")),
                int(request.url.params.get("perPage", "30")),
                5,
            ),
        )
    )
    async with AsyncSnapvisor(token="pat") as sv:
        builds = [
            build
            async for build in sv.builds.auto_paginate(
                "listBuilds", owner="acme", project="web", per_page=2
            )
        ]
    assert len(builds) == 5


@respx.mock
async def test_async_facade_raises_typed_errors():
    respx.get(f"{API}/me").mock(return_value=httpx.Response(403, json={"error": "no scope"}))
    async with AsyncSnapvisor(token="pat") as sv:
        with pytest.raises(SnapvisorForbiddenError) as exc:
            await sv.users.get_me()
    assert exc.value.status_code == 403
