"""Real end-to-end upload against the Snapvisor production API.

This test uploads two deterministic PNGs to the prod project
``devino/snapvisor-js-e2e`` and then reads the build back through the public API
to prove the build was actually created.

It is gated on ``ARGOS_TOKEN`` being present. When it is not set the test SKIPS
LOUDLY rather than passing silently — a skipped e2e is not a green e2e.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from snapvisor import upload
from tests.helpers import write_solid_png

ARGOS_TOKEN = os.environ.get("ARGOS_TOKEN")

pytestmark = pytest.mark.skipif(
    not ARGOS_TOKEN,
    reason=(
        "LOUD SKIP: ARGOS_TOKEN is not set, so the real prod e2e upload cannot "
        "run. Set ARGOS_TOKEN (project token for devino/snapvisor-js-e2e) to "
        "exercise the full create->upload->finalize->read-back flow against prod."
    ),
)

# Prod project the e2e uploads into (owner/project slug).
E2E_OWNER = "devino"
E2E_PROJECT = "snapvisor-js-e2e"
API_BASE_URL = os.environ.get("ARGOS_API_BASE_URL", "https://api.snapvisor.io/v2/")

# A real 40-hex commit SHA (DevinoSolutions/snapvisor-js HEAD at authoring time);
# overridable so CI can pass its own real commit.
DEFAULT_COMMIT = "f330839717055feb123e6bd3283ead84f43c0a34"


def test_upload_creates_a_real_prod_build(tmp_path):
    # Two deterministic screenshots, made unique per run so every run creates a
    # fresh build rather than colliding with a previous one.
    run_id = uuid.uuid4().hex[:8]
    write_solid_png(tmp_path / f"home-{run_id}.png", width=32, height=24, rgb=(220, 40, 40))
    write_solid_png(tmp_path / f"about-{run_id}.png", width=32, height=24, rgb=(40, 80, 220))

    commit = os.environ.get("ARGOS_COMMIT") or DEFAULT_COMMIT

    result = upload(
        tmp_path,
        token=ARGOS_TOKEN,
        branch="python-sdk-e2e",
        commit=commit,
        build_name=f"python-sdk-e2e-{run_id}",
        api_base_url=API_BASE_URL,
    )

    assert result.build_url.startswith("http")
    assert result.build_id
    assert result.build_number > 0

    # Read the build back through the public API to confirm it really exists and
    # has a valid status (pending/progress/... are all acceptable — the point is
    # a real, retrievable build with our two screenshots).
    base = API_BASE_URL.rstrip("/") + "/"
    with httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {ARGOS_TOKEN}"},
        timeout=30.0,
    ) as client:
        response = client.get(f"projects/{E2E_OWNER}/{E2E_PROJECT}/builds/{result.build_number}")
    assert response.status_code == 200, response.text
    build = response.json()
    assert build["number"] == result.build_number
    assert isinstance(build["status"], str) and build["status"]
