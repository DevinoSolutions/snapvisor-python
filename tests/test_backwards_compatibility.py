"""The 0.1.0 public surface is frozen — it is live on PyPI.

0.2.0 is additive. A script written against `snapvisor` 0.1.0 must run against
0.2.0 untouched, so the names, signatures, defaults, and exception hierarchy
below are transcribed from the 0.1.0 release (commit `a18e0a2`) and asserted
here rather than trusted to review.
"""

from __future__ import annotations

import inspect

import httpx
import pytest
import respx

import snapvisor
from tests.helpers import write_solid_png

# `snapvisor.__all__` at 0.1.0.
V0_1_0_EXPORTS = (
    "upload",
    "UploadResult",
    "ParallelConfig",
    "SnapvisorError",
    "SnapvisorAPIError",
    "__version__",
)

# `upload()`'s keyword-only parameters at 0.1.0, all defaulting to None.
V0_1_0_UPLOAD_KEYWORDS = (
    "token",
    "build_name",
    "branch",
    "commit",
    "reference_branch",
    "parallel",
    "api_base_url",
)


@pytest.mark.parametrize("name", V0_1_0_EXPORTS)
def test_every_0_1_0_export_still_exists(name: str):
    assert name in snapvisor.__all__
    assert hasattr(snapvisor, name)


def test_upload_still_accepts_every_0_1_0_argument():
    parameters = inspect.signature(snapvisor.upload).parameters

    first = next(iter(parameters.values()))
    assert first.name == "directory"
    assert first.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD

    for name in V0_1_0_UPLOAD_KEYWORDS:
        assert name in parameters, f"0.1.0 callers pass {name}=..."
        parameter = parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None, f"{name} must keep its 0.1.0 default"


def test_every_new_upload_argument_is_optional():
    # An argument added without a default would break every existing call site.
    for parameter in inspect.signature(snapvisor.upload).parameters.values():
        if parameter.name == "directory":
            continue
        assert parameter.default is not inspect.Parameter.empty


def test_upload_result_fields_are_unchanged():
    result = snapvisor.UploadResult(build_url="u", build_id="i", build_number=1)
    assert (result.build_url, result.build_id, result.build_number) == ("u", "i", 1)


def test_parallel_config_fields_are_unchanged():
    assert snapvisor.ParallelConfig(nonce="n", total=3) == snapvisor.ParallelConfig(
        nonce="n", total=3, index=None
    )


def test_the_new_exceptions_are_still_caught_by_the_0_1_0_handlers():
    # `except SnapvisorAPIError` and `except SnapvisorError` were the documented
    # 0.1.0 handlers; every new class must remain catchable by them.
    api_errors = (
        snapvisor.SnapvisorAuthError,
        snapvisor.SnapvisorForbiddenError,
        snapvisor.SnapvisorNotFoundError,
        snapvisor.SnapvisorConflictError,
        snapvisor.SnapvisorRateLimitError,
        snapvisor.SnapvisorServerError,
    )
    for cls in api_errors:
        assert issubclass(cls, snapvisor.SnapvisorAPIError)
        assert issubclass(cls, snapvisor.SnapvisorError)
    assert issubclass(snapvisor.SnapvisorUploadError, snapvisor.SnapvisorError)
    assert issubclass(snapvisor.SnapvisorConfigError, snapvisor.SnapvisorError)


def test_snapvisor_api_error_can_still_be_constructed_the_0_1_0_way():
    error = snapvisor.SnapvisorAPIError(
        status_code=500, method="POST", url="https://api.test/builds", message="boom"
    )
    assert error.status_code == 500
    assert error.method == "POST"
    assert error.url == "https://api.test/builds"
    assert error.message == "boom"


@respx.mock
def test_a_0_1_0_era_script_runs_unchanged(tmp_path):
    """The literal example from the 0.1.0 README, against a mocked API."""
    write_solid_png(tmp_path / "home.png")
    build = {"id": "b1", "number": 1, "url": "https://app.snapvisor.io/a/b/builds/1"}
    respx.post("https://api.snapvisor.io/v2/builds").mock(
        return_value=httpx.Response(201, json={"build": build, "screenshots": [], "pwTraces": []})
    )
    respx.put("https://api.snapvisor.io/v2/builds/b1").mock(
        return_value=httpx.Response(200, json={"build": build})
    )

    from snapvisor import upload

    result = upload(
        str(tmp_path),
        token="tok",
        build_name="my-suite",
        branch="main",
        commit="a" * 40,
        reference_branch="main",
    )

    assert result.build_url == build["url"]
    assert result.build_id == "b1"
    assert result.build_number == 1
