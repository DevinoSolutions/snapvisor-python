"""Shared fixtures.

Unit tests must behave identically on a laptop and inside GitHub Actions, so
every variable the SDK reads from the environment — its own ``SNAPVISOR_*`` /
``ARGOS_*`` config *and* the CI-provider variables :mod:`snapvisor.ci` sniffs —
is cleared before each test. A test that wants one sets it explicitly.
"""

from __future__ import annotations

import pytest

_SDK_PREFIXES = ("SNAPVISOR_", "ARGOS_")

_CI_VARIABLES = (
    "GITHUB_ACTIONS",
    "GITHUB_EVENT_PATH",
    "GITHUB_REF",
    "GITHUB_REF_NAME",
    "GITHUB_HEAD_REF",
    "GITHUB_SHA",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_JOB",
    "GITLAB_CI",
    "CIRCLECI",
    "BUILDKITE",
    "JENKINS_URL",
    "TRAVIS",
    "TF_BUILD",
    "CI",
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove SDK and CI-provider environment variables for the duration of a test."""
    import os

    for name in list(os.environ):
        if name.startswith(_SDK_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    for name in _CI_VARIABLES:
        monkeypatch.delenv(name, raising=False)
