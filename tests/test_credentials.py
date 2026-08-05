"""The CLI's credential store."""

from __future__ import annotations

import json

from snapvisor import credentials
from snapvisor.credentials import Credentials


def test_save_then_load_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAPVISOR_CONFIG_DIR", str(tmp_path))
    path = credentials.save(Credentials(token="pat-1", api_base_url="https://api.test/v2"))

    assert path == tmp_path / "credentials.json"
    loaded = credentials.load()
    assert loaded == Credentials(token="pat-1", api_base_url="https://api.test/v2")


def test_load_returns_none_when_nothing_is_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAPVISOR_CONFIG_DIR", str(tmp_path))
    assert credentials.load() is None


def test_a_corrupt_file_never_blocks_a_command(tmp_path, monkeypatch):
    # A broken cache must degrade to "no stored token", not to a crash: the user
    # can still pass --token or set the env var.
    monkeypatch.setenv("SNAPVISOR_CONFIG_DIR", str(tmp_path))
    (tmp_path / "credentials.json").write_text("{not json", encoding="utf-8")
    assert credentials.load() is None


def test_an_empty_token_is_treated_as_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAPVISOR_CONFIG_DIR", str(tmp_path))
    (tmp_path / "credentials.json").write_text(json.dumps({"token": ""}), encoding="utf-8")
    assert credentials.load() is None


def test_clear_removes_the_file_and_reports_whether_it_existed(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAPVISOR_CONFIG_DIR", str(tmp_path))
    credentials.save(Credentials(token="pat-1"))
    assert credentials.clear() is True
    assert credentials.clear() is False
    assert credentials.load() is None


def test_xdg_config_home_is_honoured(tmp_path, monkeypatch):
    monkeypatch.delenv("SNAPVISOR_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert credentials.credentials_path() == tmp_path / "snapvisor" / "credentials.json"


def test_cli_login_and_logout(tmp_path, monkeypatch, capsys):
    from snapvisor import cli

    monkeypatch.setenv("SNAPVISOR_CONFIG_DIR", str(tmp_path))
    assert cli.main(["login", "--token", "pat-42"]) == 0
    assert credentials.load() == Credentials(token="pat-42")
    assert "stored" in capsys.readouterr().out

    assert cli.main(["logout"]) == 0
    assert credentials.load() is None


def test_cli_login_without_a_token_is_a_config_error(tmp_path, monkeypatch):
    from snapvisor import cli

    monkeypatch.setenv("SNAPVISOR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin", _EmptyStdin())
    assert cli.main(["login"]) == cli.EXIT_CONFIG_ERROR


class _EmptyStdin:
    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        return "\n"
