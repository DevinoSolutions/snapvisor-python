"""CI-environment detection.

The API's build model is much richer than "a commit on a branch": ``prNumber``,
``prHeadCommit``, ``ciProvider``, ``runId``, and ``runAttempt`` are what let
Snapvisor attach a build to a pull request, supersede an earlier attempt of the
same run, and report status back to the forge. 0.1.0 shelled out to ``git`` and
sent none of it, so every Python-uploaded build looked like a bare branch push.

This is a port of ``@snapvisor/core``'s ``ci-environment`` detectors. Each
detector is a pure function of an environment mapping, which is exactly how the
tests drive them — captured env dicts per provider, no CI required.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CiEnvironment:
    """What the SDK could learn about the CI run it is executing inside.

    Attributes:
        name: Human-readable provider name.
        provider: The identifier sent to the API as ``ciProvider``.
        commit: The commit under test — the PR *head* commit where the provider
            checks out a synthetic merge commit.
        branch: The branch under test (the source branch for a pull request).
        pr_number: The pull/merge request number, when running for one.
        pr_head_commit: The head commit of the pull request, when it differs
            from the checked-out commit.
        run_id: The provider's identifier for this run, used to group parallel shards.
        run_attempt: 1-based attempt number for re-run support.
        job_id: The provider's identifier for this job within the run.
        nonce: A stable value shared by every shard of the same run, used as the
            default parallel nonce.
    """

    name: str
    provider: str
    commit: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    pr_head_commit: str | None = None
    run_id: str | None = None
    run_attempt: int | None = None
    job_id: str | None = None
    nonce: str | None = None


def _int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _github_event(environ: Mapping[str, str]) -> dict:
    """Read the GitHub Actions event payload, or ``{}`` when unavailable."""
    path = environ.get("GITHUB_EVENT_PATH")
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _sub_object(payload: dict, key: str) -> dict:
    """Return ``payload[key]`` when it is an object, else an empty one."""
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _detect_github_actions(environ: Mapping[str, str]) -> CiEnvironment | None:
    if environ.get("GITHUB_ACTIONS") != "true":
        return None
    event = _github_event(environ)
    pull_request = _sub_object(event, "pull_request")
    head = _sub_object(pull_request, "head")

    raw_number = pull_request.get("number")
    pr_number = _int(str(raw_number)) if raw_number is not None else None
    if pr_number is None:
        # `refs/pull/123/merge` is present even without an event payload.
        match = re.match(r"^refs/pull/(\d+)/", environ.get("GITHUB_REF", ""))
        pr_number = _int(match.group(1)) if match else None

    # On `pull_request` events GITHUB_SHA is a synthetic merge commit that exists
    # nowhere in the repository; the head SHA is the one users can check out.
    raw_sha = head.get("sha")
    pr_head_commit = raw_sha if isinstance(raw_sha, str) else None
    commit = pr_head_commit or environ.get("GITHUB_SHA")
    branch = environ.get("GITHUB_HEAD_REF") or environ.get("GITHUB_REF_NAME")
    run_id = environ.get("GITHUB_RUN_ID")
    run_attempt = _int(environ.get("GITHUB_RUN_ATTEMPT"))

    return CiEnvironment(
        name="GitHub Actions",
        provider="github-actions",
        commit=commit,
        branch=branch,
        pr_number=pr_number,
        pr_head_commit=pr_head_commit,
        run_id=run_id,
        run_attempt=run_attempt,
        job_id=environ.get("GITHUB_JOB"),
        nonce=f"{run_id}-{run_attempt or 1}" if run_id else None,
    )


def _detect_gitlab_ci(environ: Mapping[str, str]) -> CiEnvironment | None:
    if environ.get("GITLAB_CI") != "true":
        return None
    run_id = environ.get("CI_PIPELINE_ID")
    return CiEnvironment(
        name="GitLab CI",
        provider="gitlab-ci",
        commit=environ.get("CI_MERGE_REQUEST_SOURCE_BRANCH_SHA") or environ.get("CI_COMMIT_SHA"),
        branch=environ.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME")
        or environ.get("CI_COMMIT_REF_NAME"),
        pr_number=_int(environ.get("CI_MERGE_REQUEST_IID")),
        pr_head_commit=environ.get("CI_MERGE_REQUEST_SOURCE_BRANCH_SHA"),
        run_id=run_id,
        job_id=environ.get("CI_JOB_ID"),
        nonce=run_id,
    )


def _detect_circleci(environ: Mapping[str, str]) -> CiEnvironment | None:
    if environ.get("CIRCLECI") != "true":
        return None
    # CIRCLE_PULL_REQUEST is a URL ending in the PR number.
    match = re.search(r"/(\d+)/?$", environ.get("CIRCLE_PULL_REQUEST", ""))
    run_id = environ.get("CIRCLE_WORKFLOW_ID") or environ.get("CIRCLE_BUILD_NUM")
    return CiEnvironment(
        name="CircleCI",
        provider="circleci",
        commit=environ.get("CIRCLE_SHA1"),
        branch=environ.get("CIRCLE_BRANCH"),
        pr_number=_int(match.group(1)) if match else None,
        run_id=run_id,
        job_id=environ.get("CIRCLE_BUILD_NUM"),
        nonce=run_id,
    )


def _detect_buildkite(environ: Mapping[str, str]) -> CiEnvironment | None:
    if environ.get("BUILDKITE") != "true":
        return None
    run_id = environ.get("BUILDKITE_BUILD_ID")
    retries = _int(environ.get("BUILDKITE_RETRY_COUNT"))
    return CiEnvironment(
        name="Buildkite",
        provider="buildkite",
        commit=environ.get("BUILDKITE_COMMIT"),
        branch=environ.get("BUILDKITE_BRANCH"),
        # Buildkite sets the literal string "false" when not a pull request.
        pr_number=_int(environ.get("BUILDKITE_PULL_REQUEST")),
        run_id=run_id,
        run_attempt=(retries + 1) if retries is not None else None,
        job_id=environ.get("BUILDKITE_JOB_ID"),
        nonce=run_id,
    )


def _detect_jenkins(environ: Mapping[str, str]) -> CiEnvironment | None:
    if not environ.get("JENKINS_URL"):
        return None
    run_id = environ.get("BUILD_ID") or environ.get("BUILD_NUMBER")
    branch = environ.get("CHANGE_BRANCH") or environ.get("BRANCH_NAME") or environ.get("GIT_BRANCH")
    if branch and branch.startswith("origin/"):
        branch = branch[len("origin/") :]
    return CiEnvironment(
        name="Jenkins",
        provider="jenkins",
        commit=environ.get("GIT_COMMIT"),
        branch=branch,
        pr_number=_int(environ.get("CHANGE_ID")),
        run_id=run_id,
        job_id=environ.get("BUILD_NUMBER"),
        nonce=run_id,
    )


def _detect_travis(environ: Mapping[str, str]) -> CiEnvironment | None:
    if environ.get("TRAVIS") != "true":
        return None
    pr_head_commit = environ.get("TRAVIS_PULL_REQUEST_SHA") or None
    run_id = environ.get("TRAVIS_BUILD_ID")
    return CiEnvironment(
        name="Travis CI",
        provider="travis",
        commit=pr_head_commit or environ.get("TRAVIS_COMMIT"),
        branch=environ.get("TRAVIS_PULL_REQUEST_BRANCH") or environ.get("TRAVIS_BRANCH"),
        pr_number=_int(environ.get("TRAVIS_PULL_REQUEST")),
        pr_head_commit=pr_head_commit,
        run_id=run_id,
        job_id=environ.get("TRAVIS_JOB_ID"),
        nonce=run_id,
    )


def _detect_azure_pipelines(environ: Mapping[str, str]) -> CiEnvironment | None:
    if environ.get("TF_BUILD") not in ("True", "true"):
        return None
    pr_head_commit = environ.get("SYSTEM_PULLREQUEST_SOURCECOMMITID") or None
    run_id = environ.get("BUILD_BUILDID")
    return CiEnvironment(
        name="Azure Pipelines",
        provider="azure-pipelines",
        commit=pr_head_commit or environ.get("BUILD_SOURCEVERSION"),
        branch=environ.get("SYSTEM_PULLREQUEST_SOURCEBRANCH")
        or environ.get("BUILD_SOURCEBRANCHNAME"),
        pr_number=_int(environ.get("SYSTEM_PULLREQUEST_PULLREQUESTNUMBER")),
        pr_head_commit=pr_head_commit,
        run_id=run_id,
        run_attempt=_int(environ.get("SYSTEM_JOBATTEMPT")),
        job_id=environ.get("SYSTEM_JOBID"),
        nonce=run_id,
    )


# Order matters only in that a more specific provider must precede a generic one;
# these are all mutually exclusive in practice.
DETECTORS = (
    _detect_github_actions,
    _detect_gitlab_ci,
    _detect_circleci,
    _detect_buildkite,
    _detect_travis,
    _detect_azure_pipelines,
    _detect_jenkins,
)


def detect_ci(environ: Mapping[str, str] | None = None) -> CiEnvironment | None:
    """Identify the CI provider running this process, if any.

    Args:
        environ: Environment to inspect; defaults to :data:`os.environ`.

    Returns:
        The detected :class:`CiEnvironment`, or ``None`` when not running in a
        supported CI provider (a local run, for instance).
    """
    source = os.environ if environ is None else environ
    for detector in DETECTORS:
        detected = detector(source)
        if detected is not None:
            return detected
    return None
