"""CLI wiring: argument parsing, output format, and error handling."""

from __future__ import annotations

import pytest

from snapvisor import cli
from snapvisor.errors import SnapvisorConfigError
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


def test_snapvisor_error_exits_nonzero(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise SnapvisorConfigError("missing token")

    monkeypatch.setattr(cli, "upload", boom)
    code = cli.main(["upload", "shots"])
    assert code == 1
    assert "missing token" in capsys.readouterr().err


def test_missing_subcommand_exits_with_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
