"""CI detection, driven from captured environment dictionaries — no CI required."""

from __future__ import annotations

import json

import pytest

from snapvisor.ci import detect_ci

GITHUB_PUSH = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_SHA": "a" * 40,
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_REF_NAME": "main",
    "GITHUB_RUN_ID": "1234567890",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_JOB": "unit-test",
}

GITHUB_PULL_REQUEST = {
    **GITHUB_PUSH,
    "GITHUB_REF": "refs/pull/77/merge",
    "GITHUB_HEAD_REF": "feature/thing",
}


def test_github_actions_push():
    ci = detect_ci(GITHUB_PUSH)
    assert ci is not None
    assert (ci.provider, ci.branch, ci.commit) == ("github-actions", "main", "a" * 40)
    assert (ci.run_id, ci.run_attempt, ci.job_id) == ("1234567890", 2, "unit-test")
    assert ci.nonce == "1234567890-2", "shards of the same run attempt must share a nonce"
    assert ci.pr_number is None


def test_github_actions_pull_request_prefers_the_head_ref_and_reads_the_pr_number():
    ci = detect_ci(GITHUB_PULL_REQUEST)
    assert ci is not None
    assert ci.branch == "feature/thing"
    assert ci.pr_number == 77


def test_github_actions_reads_the_head_sha_from_the_event_payload(tmp_path):
    # On `pull_request` events GITHUB_SHA is a synthetic merge commit that exists
    # in no branch; the build must be attributed to the PR head instead.
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"pull_request": {"number": 99, "head": {"sha": "b" * 40}}}), encoding="utf-8"
    )
    ci = detect_ci({**GITHUB_PULL_REQUEST, "GITHUB_EVENT_PATH": str(event)})
    assert ci is not None
    assert ci.pr_number == 99
    assert ci.pr_head_commit == "b" * 40
    assert ci.commit == "b" * 40


def test_github_actions_survives_an_unreadable_event_payload(tmp_path):
    ci = detect_ci({**GITHUB_PUSH, "GITHUB_EVENT_PATH": str(tmp_path / "missing.json")})
    assert ci is not None
    assert ci.commit == "a" * 40


def test_gitlab_ci_merge_request():
    ci = detect_ci(
        {
            "GITLAB_CI": "true",
            "CI_COMMIT_SHA": "c" * 40,
            "CI_COMMIT_REF_NAME": "detached",
            "CI_MERGE_REQUEST_IID": "12",
            "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "feature/x",
            "CI_MERGE_REQUEST_SOURCE_BRANCH_SHA": "d" * 40,
            "CI_PIPELINE_ID": "555",
            "CI_JOB_ID": "666",
        }
    )
    assert ci is not None
    assert (ci.provider, ci.branch, ci.commit) == ("gitlab-ci", "feature/x", "d" * 40)
    assert (ci.pr_number, ci.run_id, ci.nonce) == (12, "555", "555")


def test_circleci_extracts_the_pr_number_from_the_pull_request_url():
    ci = detect_ci(
        {
            "CIRCLECI": "true",
            "CIRCLE_SHA1": "e" * 40,
            "CIRCLE_BRANCH": "main",
            "CIRCLE_PULL_REQUEST": "https://github.com/acme/web/pull/321",
            "CIRCLE_WORKFLOW_ID": "wf-1",
            "CIRCLE_BUILD_NUM": "7",
        }
    )
    assert ci is not None
    assert (ci.provider, ci.pr_number, ci.run_id, ci.job_id) == ("circleci", 321, "wf-1", "7")


def test_buildkite_turns_the_retry_count_into_a_run_attempt():
    ci = detect_ci(
        {
            "BUILDKITE": "true",
            "BUILDKITE_COMMIT": "f" * 40,
            "BUILDKITE_BRANCH": "main",
            "BUILDKITE_PULL_REQUEST": "false",
            "BUILDKITE_BUILD_ID": "bk-1",
            "BUILDKITE_RETRY_COUNT": "1",
            "BUILDKITE_JOB_ID": "job-1",
        }
    )
    assert ci is not None
    assert ci.provider == "buildkite"
    assert ci.pr_number is None, "'false' is Buildkite's way of saying 'not a pull request'"
    assert ci.run_attempt == 2


def test_travis_prefers_the_pull_request_sha():
    ci = detect_ci(
        {
            "TRAVIS": "true",
            "TRAVIS_COMMIT": "1" * 40,
            "TRAVIS_PULL_REQUEST_SHA": "2" * 40,
            "TRAVIS_BRANCH": "main",
            "TRAVIS_PULL_REQUEST_BRANCH": "feature/y",
            "TRAVIS_PULL_REQUEST": "5",
            "TRAVIS_BUILD_ID": "t-1",
        }
    )
    assert ci is not None
    assert (ci.provider, ci.commit, ci.branch, ci.pr_number) == (
        "travis",
        "2" * 40,
        "feature/y",
        5,
    )


def test_azure_pipelines():
    ci = detect_ci(
        {
            "TF_BUILD": "True",
            "BUILD_SOURCEVERSION": "3" * 40,
            "BUILD_SOURCEBRANCHNAME": "main",
            "SYSTEM_PULLREQUEST_PULLREQUESTNUMBER": "8",
            "SYSTEM_PULLREQUEST_SOURCECOMMITID": "4" * 40,
            "BUILD_BUILDID": "az-1",
            "SYSTEM_JOBATTEMPT": "3",
        }
    )
    assert ci is not None
    assert (ci.provider, ci.commit, ci.pr_number, ci.run_attempt) == (
        "azure-pipelines",
        "4" * 40,
        8,
        3,
    )


def test_jenkins_strips_the_origin_prefix_from_the_branch():
    ci = detect_ci(
        {
            "JENKINS_URL": "https://ci.example.test/",
            "GIT_COMMIT": "5" * 40,
            "GIT_BRANCH": "origin/main",
            "CHANGE_ID": "42",
            "BUILD_ID": "j-1",
            "BUILD_NUMBER": "17",
        }
    )
    assert ci is not None
    assert (ci.provider, ci.branch, ci.pr_number) == ("jenkins", "main", 42)


def test_no_ci_returns_none():
    assert detect_ci({"PATH": "/usr/bin"}) is None


@pytest.mark.parametrize("value", ["", "0", "false", "-1"])
def test_non_positive_pr_numbers_are_ignored(value: str):
    ci = detect_ci({**GITHUB_PUSH, "GITHUB_REF": "refs/heads/main", "CHANGE_ID": value})
    assert ci is not None
    assert ci.pr_number is None
