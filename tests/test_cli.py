"""CLI wiring: argument parsing, output format, and error handling."""

from __future__ import annotations

import json

import pytest

from snapvisor import cli
from snapvisor.errors import (
    SnapvisorAPIError,
    SnapvisorConfigError,
    SnapvisorRateLimitError,
    SnapvisorUploadError,
)
from snapvisor.upload import ParallelConfig, UploadResult

RESULT = UploadResult(
    build_url="https://app.snapvisor.io/devino/demo/builds/7",
    build_id="b7",
    build_number=7,
)


def test_upload_prints_build_url(monkeypatch, capsys):
    captured: dict[str, object] = {}

    def fake_upload(directory, **kwargs):
        captured["directory"] = directory
        captured.update(kwargs)
        return RESULT

    monkeypatch.setattr(cli, "upload", fake_upload)
    code = cli.main(["upload", "shots", "--token", "tok", "--build-name", "demo"])

    assert code == 0
    assert capsys.readouterr().out.strip() == "Build created: " + RESULT.build_url
    assert captured["directory"] == "shots"
    assert captured["token"] == "tok"
    assert captured["build_name"] == "demo"


def test_upload_builds_parallel_config(monkeypatch):
    captured: dict[str, object] = {}

    def fake_upload(directory, **kwargs):
        captured.update(kwargs)
        return RESULT

    monkeypatch.setattr(cli, "upload", fake_upload)
    cli.main(
        [
            "upload",
            "shots",
            "--parallel-nonce",
            "n1",
            "--parallel-total",
            "4",
            "--parallel-index",
            "2",
        ]
    )
    parallel = captured["parallel"]
    assert parallel == ParallelConfig(nonce="n1", total=4, index=2)


def test_partial_parallel_flags_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "upload", lambda *a, **k: RESULT)
    code = cli.main(["upload", "shots", "--parallel-total", "4"])
    assert code == 1
    assert "parallel" in capsys.readouterr().err.lower()


def test_config_error_exits_with_the_config_code(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise SnapvisorConfigError("missing token")

    monkeypatch.setattr(cli, "upload", boom)
    code = cli.main(["upload", "shots"])
    assert code == cli.EXIT_CONFIG_ERROR
    assert "missing token" in capsys.readouterr().err


def test_missing_subcommand_exits_with_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


# --- 0.2.0 additions -------------------------------------------------------


def _raise(error: Exception):
    def boom(*args, **kwargs):
        raise error

    return boom


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SnapvisorConfigError("no token"), cli.EXIT_CONFIG_ERROR),
        (SnapvisorUploadError("storage rejected it"), cli.EXIT_UPLOAD_ERROR),
        (
            SnapvisorAPIError(status_code=500, method="POST", url="u", message="boom"),
            cli.EXIT_API_ERROR,
        ),
        (
            SnapvisorRateLimitError(status_code=429, method="POST", url="u", message="slow down"),
            cli.EXIT_RATE_LIMITED,
        ),
    ],
)
def test_each_failure_kind_gets_its_own_exit_code(monkeypatch, error, expected):
    # CI needs to tell "you configured this wrong" from "the API is having a bad
    # day" from "you are rate limited"; 0.1.0 returned 1 for all of them.
    monkeypatch.setattr(cli, "upload", _raise(error))
    assert cli.main(["upload", "shots"]) == expected


def test_upload_forwards_the_full_build_surface(monkeypatch):
    captured: dict[str, object] = {}

    def fake_upload(directory, **kwargs):
        captured.update(kwargs)
        return RESULT

    monkeypatch.setattr(cli, "upload", fake_upload)
    cli.main(
        [
            "upload",
            "shots",
            "--pr-number",
            "77",
            "--mode",
            "monitoring",
            "--subset",
            "--merge-queue",
            "--parent-commit",
            "aa",
            "--parent-commit",
            "bb",
            "--threshold",
            "0.3",
            "--concurrency",
            "16",
            "--no-ci-detect",
        ]
    )
    build = captured["build"]
    assert build.pr_number == 77
    assert build.mode == "monitoring"
    assert build.subset is True
    assert build.merge_queue is True
    assert build.parent_commits == ["aa", "bb"]
    assert captured["threshold"] == 0.3
    assert captured["concurrency"] == 16
    assert captured["detect_ci_environment"] is False


def test_upload_reads_build_metadata_from_a_file(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli, "upload", lambda directory, **kwargs: (captured.update(kwargs), RESULT)[1]
    )
    metadata = tmp_path / "meta.json"
    metadata.write_text('{"testReport": {"status": "passed"}}', encoding="utf-8")

    cli.main(["upload", "shots", "--metadata", f"@{metadata}"])
    assert captured["build"].metadata == {"testReport": {"status": "passed"}}


def test_invalid_metadata_json_is_a_config_error(monkeypatch):
    monkeypatch.setattr(cli, "upload", lambda *a, **k: RESULT)
    assert cli.main(["upload", "shots", "--metadata", "{nope"]) == cli.EXIT_CONFIG_ERROR


def test_json_output_is_machine_readable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "upload", lambda *a, **k: RESULT)
    assert cli.main(["--json", "upload", "shots"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["buildUrl"] == RESULT.build_url
    assert payload["buildNumber"] == 7


def test_finalize_forwards_the_nonce(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "finalize_builds", lambda **kwargs: captured.update(kwargs) or {})
    assert cli.main(["finalize", "--parallel-nonce", "n1"]) == 0
    assert captured["parallel_nonce"] == "n1"


def test_skip_reports_the_created_build(monkeypatch, capsys):
    monkeypatch.setattr(cli, "skip_build", lambda **kwargs: {"id": "b1", "url": "https://app/x"})
    assert cli.main(["skip"]) == 0
    assert "https://app/x" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["build", "list", "--owner", "acme", "--project", "web"],
        ["build", "get", "--owner", "acme", "--project", "web", "--number", "1"],
        ["build", "diffs", "--owner", "acme", "--project", "web", "--number", "1"],
        ["project", "list", "--account", "acme"],
        ["project", "get", "--owner", "acme", "--project", "web"],
        ["project", "create", "--account", "acme", "--name", "web"],
        ["comment", "list", "--owner", "a", "--project", "b", "--number", "1"],
        ["comment", "create", "--owner", "a", "--project", "b", "--number", "1", "--body", "hi"],
        [
            "comment",
            "delete",
            "--owner",
            "a",
            "--project",
            "b",
            "--number",
            "1",
            "--comment-id",
            "c",
        ],
        [
            "comment",
            "resolve",
            "--owner",
            "a",
            "--project",
            "b",
            "--number",
            "1",
            "--comment-id",
            "c",
        ],
        [
            "comment",
            "unresolve",
            "--owner",
            "a",
            "--project",
            "b",
            "--number",
            "1",
            "--comment-id",
            "c",
        ],
        ["review", "list", "--owner", "a", "--project", "b", "--number", "1"],
        [
            "review",
            "create",
            "--owner",
            "a",
            "--project",
            "b",
            "--number",
            "1",
            "--state",
            "approved",
        ],
        [
            "review",
            "dismiss",
            "--owner",
            "a",
            "--project",
            "b",
            "--number",
            "1",
            "--review-id",
            "r",
        ],
        ["change", "ignore", "--owner", "a", "--project", "b", "--change-id", "c"],
        ["change", "unignore", "--owner", "a", "--project", "b", "--change-id", "c"],
        ["deployment", "get", "--deployment-id", "d"],
        ["deployment", "resolve", "--domain", "example.test"],
        ["analytics", "--account", "a", "--from", "2026-01-01", "--group-by", "day"],
        ["whoami"],
        ["skip"],
        ["finalize", "--parallel-nonce", "n"],
        ["upload", "shots"],
    ],
)
def test_every_documented_command_parses(argv):
    # Parsing is where a CLI silently rots; assert the whole grammar, not a sample.
    args = cli.build_parser().parse_args(argv)
    assert callable(args.func)


def test_the_cli_covers_the_typescript_command_groups():
    parser = cli.build_parser()
    subparsers = next(
        action
        for action in parser._subparsers._group_actions
        if action.choices  # noqa: SLF001
    )
    assert {
        "upload",
        "skip",
        "finalize",
        "whoami",
        "build",
        "review",
        "comment",
        "change",
        "project",
        "analytics",
        "deployment",
        "login",
        "logout",
    } <= set(subparsers.choices)
