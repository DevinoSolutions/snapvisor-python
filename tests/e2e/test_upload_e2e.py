"""Real end-to-end upload against the Snapvisor production API.

This test uploads deterministic PNGs — carrying screenshot metadata, a
per-screenshot threshold, and a Playwright trace — to the prod project
``devino/snapvisor-js-e2e``, then reads the build back through the public API to
prove the platform really received all of it. The assertions are on the
*server's* view, not on the request body: a request body that the backend
ignores is not coverage.

It is gated on ``SNAPVISOR_TOKEN``/``ARGOS_TOKEN`` being present. When unset the
test SKIPS LOUDLY rather than passing silently — a skipped e2e is not a green e2e.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import pytest

from snapvisor import BuildOptions, Snapvisor, upload
from snapvisor.models import Build
from tests.helpers import write_solid_png

TOKEN = os.environ.get("SNAPVISOR_TOKEN") or os.environ.get("ARGOS_TOKEN")

pytestmark = pytest.mark.skipif(
    not TOKEN,
    reason=(
        "LOUD SKIP: neither SNAPVISOR_TOKEN nor ARGOS_TOKEN is set, so the real "
        "prod e2e upload cannot run. Set a project token for "
        "devino/snapvisor-js-e2e to exercise the full "
        "create->upload->finalize->read-back flow against prod."
    ),
)

# Prod project the e2e uploads into (owner/project slug).
E2E_OWNER = "devino"
E2E_PROJECT = "snapvisor-js-e2e"
API_BASE_URL = (
    os.environ.get("SNAPVISOR_API_BASE_URL")
    or os.environ.get("ARGOS_API_BASE_URL")
    or "https://api.snapvisor.io/v2/"
)

# A real 40-hex commit SHA (DevinoSolutions/snapvisor-js HEAD at authoring time);
# overridable so CI can pass its own real commit.
DEFAULT_COMMIT = "f330839717055feb123e6bd3283ead84f43c0a34"

VIEWPORT = {"width": 1280, "height": 720}


def _commit() -> str:
    return os.environ.get("SNAPVISOR_COMMIT") or os.environ.get("ARGOS_COMMIT") or DEFAULT_COMMIT


def test_upload_creates_a_real_prod_build_carrying_metadata_and_a_trace(tmp_path):
    # Deterministic screenshots, made unique per run so every run creates a fresh
    # build rather than colliding with a previous one.
    run_id = uuid.uuid4().hex[:8]
    home = write_solid_png(tmp_path / f"home-{run_id}.png", width=32, height=24, rgb=(220, 40, 40))
    write_solid_png(tmp_path / f"about-{run_id}.png", width=32, height=24, rgb=(40, 80, 220))

    # Sidecars: metadata (with a transient threshold) and a Playwright trace.
    home.with_name(home.name + ".argos.json").write_text(
        json.dumps(
            {
                "url": "https://snapvisor.io/",
                "viewport": VIEWPORT,
                "colorScheme": "dark",
                "test": {"title": f"home {run_id}", "titlePath": ["python-sdk-e2e", "home"]},
                "transient": {"threshold": 0.2},
            }
        ),
        encoding="utf-8",
    )
    home.with_name(home.name + ".pw-trace.zip").write_bytes(
        b"PK\x05\x06" + b"\x00" * 18  # a valid empty zip
    )

    result = upload(
        tmp_path,
        token=TOKEN,
        branch="python-sdk-e2e",
        commit=_commit(),
        build_name=f"python-sdk-e2e-{run_id}",
        api_base_url=API_BASE_URL,
        build=BuildOptions(metadata={"testReport": {"status": "passed"}}),
    )

    assert result.build_url.startswith("http")
    assert result.build_id
    assert result.build_number > 0

    # Read the build back through the typed API surface — the same generated
    # client end users get — rather than a hand-rolled httpx call.
    with Snapvisor(token=TOKEN, api_base_url=API_BASE_URL) as sv:
        build = sv.builds.get_build(
            owner=E2E_OWNER, project=E2E_PROJECT, build_number=str(result.build_number)
        )
        assert isinstance(build, Build)
        assert build.number == result.build_number
        assert isinstance(build.status, str) and build.status

        diffs = _wait_for_diffs(sv, result.build_number)

    if not diffs:
        pytest.fail(
            "LOUD FAILURE: the build was created but the API returned no screenshot "
            "diffs within the polling window, so the metadata round-trip could not "
            f"be verified. Build: {result.build_url}"
        )

    names = {getattr(diff, "name", None) for diff in diffs}
    assert f"home-{run_id}" in names, f"uploaded screenshot missing from the build: {names}"

    home_diff = next(diff for diff in diffs if getattr(diff, "name", None) == f"home-{run_id}")
    head = getattr(home_diff, "head", None)
    assert head is not None, "the head screenshot must be present on a first build"
    metadata = getattr(head, "metadata", None)
    assert metadata is not None, "screenshot metadata did not round-trip through the platform"
    viewport = getattr(metadata, "viewport", None)
    assert viewport is not None
    assert (int(viewport.width), int(viewport.height)) == (VIEWPORT["width"], VIEWPORT["height"])


def _wait_for_diffs(sv: Snapvisor, build_number: int, *, timeout: float = 120.0) -> list:
    """Poll ``listBuildDiffs`` until the backend has processed the screenshots."""
    deadline = time.monotonic() + timeout
    diffs: list = []
    while time.monotonic() < deadline:
        diffs = list(
            sv.builds.auto_paginate(
                "listBuildDiffs",
                owner=E2E_OWNER,
                project=E2E_PROJECT,
                build_number=str(build_number),
                per_page=100,
            )
        )
        if diffs:
            return diffs
        time.sleep(5)
    return diffs


def test_whoami_style_read_operations_reject_a_project_token():
    """29 of the 37 operations need a PAT; the SDK must say so unambiguously."""
    from snapvisor.errors import SnapvisorAPIError

    with Snapvisor(token=TOKEN, api_base_url=API_BASE_URL) as sv:
        try:
            sv.users.get_me()
        except SnapvisorAPIError as error:
            assert error.status_code in (401, 403), (
                "a project token on a PAT-only operation must fail with 401/403, "
                f"got {error.status_code}"
            )
        else:
            pytest.skip(
                "LOUD SKIP: the configured token was accepted by /me, so it is a "
                "personal access token rather than a project token; this assertion "
                "only applies to project tokens."
            )
