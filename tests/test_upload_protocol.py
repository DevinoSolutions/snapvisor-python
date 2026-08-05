"""The parts of the upload protocol 0.1.0 could not reach.

0.1.0 sent 7 of ``createBuild``'s 20 body fields and hardcoded 5 of the 8
``ScreenshotInput`` fields to ``None``, so a Python-uploaded build carried no
viewport or test metadata, no per-screenshot threshold, no base/parent matching,
and no Playwright trace. It also uploaded strictly serially. These tests assert
the wire payload, because that is what the platform actually sees.
"""

from __future__ import annotations

import json
import threading

import httpx
import pytest
import respx

from snapvisor import BuildOptions, ParallelConfig, upload
from snapvisor.errors import SnapvisorConfigError
from snapvisor.hashing import hash_file
from snapvisor.upload import aupload, finalize_builds, find_baseline, skip_build
from tests.helpers import write_solid_png

API = "https://api.snapvisor.io/v2"
STORAGE = "https://storage.example.test/upload"

BUILD = {
    "id": "build-1",
    "number": 42,
    "url": "https://app.snapvisor.io/devino/demo/builds/42",
}


def _post_target(key: str) -> dict:
    return {"key": key, "postUrl": STORAGE, "fields": {"key": f"uploads/{key}"}}


def _mock_build_flow(*, screenshots: list[dict] | None = None, traces: list[dict] | None = None):
    create = respx.post(f"{API}/builds").mock(
        return_value=httpx.Response(
            201,
            json={
                "build": BUILD,
                "screenshots": screenshots or [],
                "pwTraces": traces or [],
            },
        )
    )
    finalize = respx.put(f"{API}/builds/build-1").mock(
        return_value=httpx.Response(200, json={"build": BUILD})
    )
    return create, finalize


@respx.mock
def test_create_build_sends_the_whole_documented_body(tmp_path):
    write_solid_png(tmp_path / "home.png")
    create, _ = _mock_build_flow()

    upload(
        tmp_path,
        token="tok",
        branch="main",
        commit="a" * 40,
        detect_ci_environment=False,
        build=BuildOptions(
            pr_number=77,
            pr_head_commit="b" * 40,
            reference_commit="c" * 40,
            reference_branch="develop",
            parent_commits=["d" * 40, "e" * 40],
            mode="monitoring",
            ci_provider="github-actions",
            run_id="run-1",
            run_attempt=2,
            merge_queue=True,
            merge_queue_pr_numbers=[1, 2],
            subset=True,
        ),
    )

    body = json.loads(create.calls.last.request.content)
    assert body["prNumber"] == 77
    assert body["prHeadCommit"] == "b" * 40
    assert body["referenceCommit"] == "c" * 40
    assert body["referenceBranch"] == "develop"
    assert body["parentCommits"] == ["d" * 40, "e" * 40]
    assert body["mode"] == "monitoring"
    assert body["ciProvider"] == "github-actions"
    assert body["runId"] == "run-1"
    assert body["runAttempt"] == 2
    assert body["mergeQueue"] is True
    assert body["mergeQueuePrNumbers"] == [1, 2]
    assert body["subset"] is True


@respx.mock
def test_unset_optional_fields_are_omitted_rather_than_sent_as_null(tmp_path):
    write_solid_png(tmp_path / "home.png")
    create, _ = _mock_build_flow()
    upload(tmp_path, token="tok", branch="main", commit="a" * 40, detect_ci_environment=False)

    body = json.loads(create.calls.last.request.content)
    for field in ("prNumber", "mode", "mergeQueue", "subset", "runId"):
        assert field not in body


@respx.mock
def test_an_invalid_mode_is_rejected_before_any_request(tmp_path):
    write_solid_png(tmp_path / "home.png")
    _mock_build_flow()
    with pytest.raises(SnapvisorConfigError, match="Invalid build mode"):
        upload(
            tmp_path,
            token="tok",
            branch="main",
            commit="a" * 40,
            build=BuildOptions(mode="nonsense"),
        )


@respx.mock
def test_screenshot_metadata_sidecar_reaches_the_finalize_request(tmp_path):
    screenshot = write_solid_png(tmp_path / "home.png")
    screenshot.with_name("home.png.argos.json").write_text(
        json.dumps(
            {
                "url": "https://acme.test/",
                "viewport": {"width": 1280, "height": 720},
                "colorScheme": "dark",
                "test": {"title": "home page", "titlePath": ["suite", "home page"]},
                "transient": {"threshold": 0.25, "baseName": "base.png", "parentName": "p.png"},
            }
        ),
        encoding="utf-8",
    )
    _, finalize = _mock_build_flow()

    upload(tmp_path, token="tok", branch="main", commit="a" * 40, detect_ci_environment=False)

    entry = json.loads(finalize.calls.last.request.content)["screenshots"][0]
    assert entry["metadata"]["viewport"] == {"width": 1280, "height": 720}
    assert entry["metadata"]["colorScheme"] == "dark"
    assert entry["metadata"]["test"]["title"] == "home page"
    assert "transient" not in entry["metadata"], "transient fields are lifted, not forwarded"
    assert entry["threshold"] == 0.25
    assert entry["baseName"] == "base.png"
    assert entry["parentName"] == "p.png"


@respx.mock
def test_a_default_threshold_applies_only_where_the_sidecar_sets_none(tmp_path):
    write_solid_png(tmp_path / "plain.png", rgb=(1, 1, 1))
    tuned = write_solid_png(tmp_path / "tuned.png", rgb=(2, 2, 2))
    tuned.with_name("tuned.png.argos.json").write_text(
        json.dumps({"transient": {"threshold": 0.9}}), encoding="utf-8"
    )
    _, finalize = _mock_build_flow()

    upload(
        tmp_path,
        token="tok",
        branch="main",
        commit="a" * 40,
        threshold=0.1,
        detect_ci_environment=False,
    )

    by_name = {
        entry["name"]: entry
        for entry in json.loads(finalize.calls.last.request.content)["screenshots"]
    }
    assert by_name["plain"]["threshold"] == 0.1
    assert by_name["tuned"]["threshold"] == 0.9


@respx.mock
def test_build_level_metadata_is_sent_with_the_finalize_request(tmp_path):
    write_solid_png(tmp_path / "home.png")
    _, finalize = _mock_build_flow()

    upload(
        tmp_path,
        token="tok",
        branch="main",
        commit="a" * 40,
        detect_ci_environment=False,
        build=BuildOptions(metadata={"testReport": {"status": "passed"}}),
    )

    body = json.loads(finalize.calls.last.request.content)
    assert body["metadata"] == {"testReport": {"status": "passed"}}


@respx.mock
def test_playwright_traces_are_declared_and_uploaded(tmp_path):
    screenshot = write_solid_png(tmp_path / "home.png")
    trace = screenshot.with_name("home.png.pw-trace.zip")
    trace.write_bytes(b"PK\x03\x04 fake trace")
    trace_key = hash_file(trace)
    screenshot_key = hash_file(screenshot)

    create, finalize = _mock_build_flow(
        screenshots=[_post_target(screenshot_key)],
        traces=[{"key": trace_key, "postUrl": STORAGE, "fields": {}}],
    )
    storage = respx.post(STORAGE).mock(return_value=httpx.Response(204))

    upload(tmp_path, token="tok", branch="main", commit="a" * 40, detect_ci_environment=False)

    create_body = json.loads(create.calls.last.request.content)
    assert create_body["pwTraceKeys"] == [trace_key]
    # 0.1.0 discarded the pwTraces targets entirely, so only the screenshot went up.
    assert storage.call_count == 2
    entry = json.loads(finalize.calls.last.request.content)["screenshots"][0]
    assert entry["pwTraceKey"] == trace_key


@respx.mock
def test_a_trace_is_uploaded_as_a_zip(tmp_path):
    screenshot = write_solid_png(tmp_path / "home.png")
    trace = screenshot.with_name("home.png.pw-trace.zip")
    trace.write_bytes(b"PK\x03\x04")
    _mock_build_flow(traces=[{"key": hash_file(trace), "putUrl": f"{STORAGE}/t"}])
    put = respx.put(f"{STORAGE}/t").mock(return_value=httpx.Response(200))

    upload(tmp_path, token="tok", branch="main", commit="a" * 40, detect_ci_environment=False)
    assert put.calls.last.request.headers["content-type"] == "application/zip"


@respx.mock
def test_uploads_run_concurrently(tmp_path):
    for index in range(12):
        write_solid_png(tmp_path / f"shot-{index}.png", rgb=(index, index, index))
    keys = [hash_file(tmp_path / f"shot-{index}.png") for index in range(12)]
    _mock_build_flow(screenshots=[_post_target(key) for key in keys])

    live = 0
    peak = 0
    lock = threading.Lock()
    gate = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        # Hold every worker until enough of them have arrived to prove overlap.
        if peak >= 4:
            gate.set()
        gate.wait(timeout=5)
        with lock:
            live -= 1
        return httpx.Response(204)

    respx.post(STORAGE).mock(side_effect=handler)

    upload(
        tmp_path,
        token="tok",
        branch="main",
        commit="a" * 40,
        concurrency=4,
        detect_ci_environment=False,
    )
    assert peak >= 2, "0.1.0 uploaded strictly one file at a time"
    assert peak <= 4, "the worker pool must respect its bound"


@respx.mock
def test_concurrency_of_one_preserves_the_serial_behaviour(tmp_path):
    for index in range(3):
        write_solid_png(tmp_path / f"shot-{index}.png", rgb=(index, index, index))
    keys = [hash_file(tmp_path / f"shot-{index}.png") for index in range(3)]
    _mock_build_flow(screenshots=[_post_target(key) for key in keys])
    storage = respx.post(STORAGE).mock(return_value=httpx.Response(204))

    upload(
        tmp_path,
        token="tok",
        branch="main",
        commit="a" * 40,
        concurrency=1,
        detect_ci_environment=False,
    )
    assert storage.call_count == 3


@respx.mock
def test_a_failing_upload_still_aborts_the_build(tmp_path):
    for index in range(4):
        write_solid_png(tmp_path / f"shot-{index}.png", rgb=(index, index, index))
    keys = [hash_file(tmp_path / f"shot-{index}.png") for index in range(4)]
    _mock_build_flow(screenshots=[_post_target(key) for key in keys])
    respx.post(STORAGE).mock(return_value=httpx.Response(403, text="denied"))

    from snapvisor.errors import SnapvisorUploadError

    with pytest.raises(SnapvisorUploadError, match="denied"):
        upload(tmp_path, token="tok", branch="main", commit="a" * 40, detect_ci_environment=False)


@respx.mock
def test_ci_environment_populates_the_build(tmp_path, monkeypatch):
    write_solid_png(tmp_path / "home.png")
    create, _ = _mock_build_flow()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    monkeypatch.setenv("GITHUB_REF", "refs/pull/12/merge")
    monkeypatch.setenv("GITHUB_HEAD_REF", "feature/thing")
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")

    upload(tmp_path, token="tok")

    body = json.loads(create.calls.last.request.content)
    assert body["commit"] == "f" * 40
    assert body["branch"] == "feature/thing"
    assert body["ciProvider"] == "github-actions"
    assert body["runId"] == "999"
    assert body["runAttempt"] == 3
    assert body["prNumber"] == 12


@respx.mock
def test_explicit_values_beat_the_detected_ci_environment(tmp_path, monkeypatch):
    write_solid_png(tmp_path / "home.png")
    create, _ = _mock_build_flow()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")

    upload(tmp_path, token="tok", commit="0" * 40, branch="explicit")

    body = json.loads(create.calls.last.request.content)
    assert body["commit"] == "0" * 40
    assert body["branch"] == "explicit"


@respx.mock
def test_snapvisor_env_vars_take_precedence_over_argos(tmp_path, monkeypatch):
    write_solid_png(tmp_path / "home.png")
    create, _ = _mock_build_flow()
    monkeypatch.setenv("ARGOS_TOKEN", "legacy")
    monkeypatch.setenv("SNAPVISOR_TOKEN", "modern")
    monkeypatch.setenv("ARGOS_BRANCH", "old-branch")
    monkeypatch.setenv("SNAPVISOR_BRANCH", "new-branch")

    upload(tmp_path, commit="a" * 40, detect_ci_environment=False)

    request = create.calls.last.request
    assert request.headers["authorization"] == "Bearer modern"
    assert json.loads(request.content)["branch"] == "new-branch"


@respx.mock
def test_argos_env_vars_still_work_alone(tmp_path, monkeypatch):
    write_solid_png(tmp_path / "home.png")
    create, _ = _mock_build_flow()
    monkeypatch.setenv("ARGOS_TOKEN", "legacy")
    monkeypatch.setenv("ARGOS_BRANCH", "old-branch")

    upload(tmp_path, commit="a" * 40, detect_ci_environment=False)

    request = create.calls.last.request
    assert request.headers["authorization"] == "Bearer legacy"
    assert json.loads(request.content)["branch"] == "old-branch"


@respx.mock
def test_skip_build_marks_the_build_skipped():
    route = respx.post(f"{API}/builds").mock(
        return_value=httpx.Response(201, json={"build": BUILD, "screenshots": [], "pwTraces": []})
    )
    build = skip_build(token="tok", branch="main", commit="a" * 40)

    body = json.loads(route.calls.last.request.content)
    assert body["skipped"] is True
    assert body["screenshots"] == []
    assert build["id"] == "build-1"


@respx.mock
def test_finalize_builds_posts_the_parallel_nonce():
    route = respx.post(f"{API}/builds/finalize").mock(
        return_value=httpx.Response(200, json={"builds": [BUILD]})
    )
    payload = finalize_builds(parallel_nonce="n1", token="tok")
    assert json.loads(route.calls.last.request.content) == {"parallelNonce": "n1"}
    assert payload["builds"][0]["id"] == "build-1"


@respx.mock
def test_find_baseline_posts_the_candidate_commits():
    route = respx.post(f"{API}/baseline").mock(
        return_value=httpx.Response(200, json={"commit": "a" * 40})
    )
    find_baseline(commits=["a" * 40, "b" * 40], token="tok")
    assert json.loads(route.calls.last.request.content) == {"commits": ["a" * 40, "b" * 40]}


@respx.mock
def test_parallel_nonce_defaults_to_the_ci_run(tmp_path, monkeypatch):
    write_solid_png(tmp_path / "home.png")
    create, _ = _mock_build_flow()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("SNAPVISOR_PARALLEL", "true")
    monkeypatch.setenv("SNAPVISOR_PARALLEL_TOTAL", "4")

    upload(tmp_path, token="tok")

    body = json.loads(create.calls.last.request.content)
    assert body["parallel"] is True
    assert body["parallelNonce"] == "999-1"


@respx.mock
def test_explicit_parallel_config_is_unchanged_from_0_1_0(tmp_path):
    write_solid_png(tmp_path / "home.png")
    create, finalize = _mock_build_flow()

    upload(
        tmp_path,
        token="tok",
        branch="main",
        commit="a" * 40,
        detect_ci_environment=False,
        parallel=ParallelConfig(nonce="n1", total=3, index=1),
    )
    assert json.loads(create.calls.last.request.content)["parallelNonce"] == "n1"
    final_body = json.loads(finalize.calls.last.request.content)
    assert (final_body["parallelTotal"], final_body["parallelIndex"]) == (3, 1)


@respx.mock
async def test_aupload_matches_the_sync_wire_payload(tmp_path):
    screenshot = write_solid_png(tmp_path / "home.png")
    key = hash_file(screenshot)
    create, finalize = _mock_build_flow(screenshots=[_post_target(key)])
    storage = respx.post(STORAGE).mock(return_value=httpx.Response(204))

    result = await aupload(
        tmp_path, token="tok", branch="main", commit="a" * 40, detect_ci_environment=False
    )

    assert result.build_url == BUILD["url"]
    assert storage.call_count == 1
    assert json.loads(create.calls.last.request.content)["branch"] == "main"
    assert json.loads(finalize.calls.last.request.content)["final"] is True
