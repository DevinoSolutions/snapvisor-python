"""End-to-end upload flow against a mocked API and storage backend (respx)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from snapvisor import ParallelConfig, SnapvisorAPIError, upload
from snapvisor.errors import SnapvisorConfigError
from snapvisor.hashing import hash_file
from tests.helpers import write_solid_png

API = "https://api.snapvisor.io/v2"
STORAGE = "https://storage.example.test/upload"

BUILD = {
    "id": "build-1",
    "number": 42,
    "url": "https://app.snapvisor.io/devino/demo/builds/42",
}


def _make_screenshots(tmp_path):
    a = write_solid_png(tmp_path / "home.png", rgb=(255, 0, 0))
    b = write_solid_png(tmp_path / "nested" / "about.png", rgb=(0, 0, 255))
    return hash_file(a), hash_file(b)


def _post_target(key: str) -> dict:
    return {"key": key, "postUrl": STORAGE, "fields": {"key": f"uploads/{key}"}}


@respx.mock
def test_upload_happy_path_creates_and_finalizes_build(tmp_path):
    key_a, key_b = _make_screenshots(tmp_path)

    create = respx.post(f"{API}/builds").mock(
        return_value=httpx.Response(
            201,
            json={
                "build": BUILD,
                "screenshots": [_post_target(key_a), _post_target(key_b)],
                "pwTraces": [],
            },
        )
    )
    storage = respx.post(STORAGE).mock(return_value=httpx.Response(204))
    finalize = respx.put(f"{API}/builds/build-1").mock(
        return_value=httpx.Response(200, json={"build": BUILD})
    )

    result = upload(tmp_path, token="tok", branch="main", commit="a" * 40, build_name="demo")

    assert result.build_url == BUILD["url"]
    assert result.build_id == "build-1"
    assert result.build_number == 42

    # createBuild body carries commit/branch/name and both screenshot keys.
    body = json.loads(create.calls.last.request.content)
    assert body["commit"] == "a" * 40
    assert body["branch"] == "main"
    assert body["name"] == "demo"
    assert body["argosSdk"].startswith("snapvisor-python/")
    assert {s["key"] for s in body["screenshots"]} == {key_a, key_b}
    assert all(s["contentType"] == "image/png" for s in body["screenshots"])

    # One storage upload per missing screenshot.
    assert storage.call_count == 2

    # finalize sends the full list with names and final=true.
    final_body = json.loads(finalize.calls.last.request.content)
    assert final_body["final"] is True
    assert {s["name"] for s in final_body["screenshots"]} == {"home", "nested/about"}


@respx.mock
def test_only_missing_screenshots_are_uploaded(tmp_path):
    key_a, key_b = _make_screenshots(tmp_path)
    # Server already has key_b, so it only returns an upload target for key_a.
    respx.post(f"{API}/builds").mock(
        return_value=httpx.Response(
            201,
            json={"build": BUILD, "screenshots": [_post_target(key_a)], "pwTraces": []},
        )
    )
    storage = respx.post(STORAGE).mock(return_value=httpx.Response(204))
    respx.put(f"{API}/builds/build-1").mock(return_value=httpx.Response(200, json={"build": BUILD}))

    upload(tmp_path, token="tok", branch="main", commit="a" * 40)
    assert storage.call_count == 1


@respx.mock
def test_proxied_post_target_sends_fields_and_file(tmp_path):
    # A single-screenshot directory so exactly one target is uploaded.
    write_solid_png(tmp_path / "one.png", rgb=(1, 2, 3))
    key = hash_file(tmp_path / "one.png")
    respx.post(f"{API}/builds").mock(
        return_value=httpx.Response(
            201,
            json={"build": BUILD, "screenshots": [_post_target(key)], "pwTraces": []},
        )
    )
    storage = respx.post(STORAGE).mock(return_value=httpx.Response(201))
    respx.put(f"{API}/builds/build-1").mock(return_value=httpx.Response(200, json={"build": BUILD}))

    upload(tmp_path, token="tok", branch="main", commit="a" * 40)

    sent = storage.calls.last.request
    body = sent.content
    assert b'name="key"' in body  # policy field appended
    assert b'name="file"' in body  # file part present
    assert b"multipart/form-data" in sent.headers["content-type"].encode()


@respx.mock
def test_put_target_shape_is_supported(tmp_path):
    write_solid_png(tmp_path / "one.png", rgb=(9, 9, 9))
    key = hash_file(tmp_path / "one.png")
    respx.post(f"{API}/builds").mock(
        return_value=httpx.Response(
            201,
            json={
                "build": BUILD,
                "screenshots": [{"key": key, "putUrl": f"{STORAGE}/{key}"}],
                "pwTraces": [],
            },
        )
    )
    put_storage = respx.put(f"{STORAGE}/{key}").mock(return_value=httpx.Response(200))
    respx.put(f"{API}/builds/build-1").mock(return_value=httpx.Response(200, json={"build": BUILD}))

    upload(tmp_path, token="tok", branch="main", commit="a" * 40)
    assert put_storage.called
    assert put_storage.calls.last.request.headers["content-type"] == "image/png"


@respx.mock
def test_parallel_config_marks_create_and_finalize(tmp_path):
    key, _ = _make_screenshots(tmp_path)
    create = respx.post(f"{API}/builds").mock(
        return_value=httpx.Response(201, json={"build": BUILD, "screenshots": [], "pwTraces": []})
    )
    finalize = respx.put(f"{API}/builds/build-1").mock(
        return_value=httpx.Response(200, json={"build": BUILD})
    )

    upload(
        tmp_path,
        token="tok",
        branch="main",
        commit="a" * 40,
        parallel=ParallelConfig(nonce="n1", total=3, index=1),
    )
    create_body = json.loads(create.calls.last.request.content)
    assert create_body["parallel"] is True
    assert create_body["parallelNonce"] == "n1"
    final_body = json.loads(finalize.calls.last.request.content)
    assert final_body["parallelTotal"] == 3
    assert final_body["parallelIndex"] == 1


@respx.mock
def test_api_error_raises_snapvisor_api_error(tmp_path):
    _make_screenshots(tmp_path)
    respx.post(f"{API}/builds").mock(
        return_value=httpx.Response(401, json={"error": "Invalid token"})
    )
    with pytest.raises(SnapvisorAPIError) as exc:
        upload(tmp_path, token="bad", branch="main", commit="a" * 40)
    assert exc.value.status_code == 401
    assert "Invalid token" in str(exc.value)


def test_missing_token_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("ARGOS_TOKEN", raising=False)
    write_solid_png(tmp_path / "one.png")
    with pytest.raises(SnapvisorConfigError, match="token"):
        upload(tmp_path, branch="main", commit="a" * 40)


def test_empty_directory_raises_config_error(tmp_path):
    with pytest.raises(SnapvisorConfigError, match="No screenshots"):
        upload(tmp_path, token="tok", branch="main", commit="a" * 40)


def test_missing_directory_raises_config_error(tmp_path):
    with pytest.raises(SnapvisorConfigError, match="does not exist"):
        upload(tmp_path / "nope", token="tok", branch="main", commit="a" * 40)
