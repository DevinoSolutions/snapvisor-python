"""Screenshot discovery: recursive, extension-filtered, relative POSIX names."""

from __future__ import annotations

import pytest

from snapvisor.discovery import discover_snapshots
from snapvisor.errors import SnapvisorConfigError
from tests.helpers import write_solid_png


def test_discovers_nested_pngs_with_relative_names(tmp_path):
    write_solid_png(tmp_path / "home.png")
    write_solid_png(tmp_path / "nested" / "about.png")
    (tmp_path / "notes.txt").write_text("ignore me")

    snapshots = discover_snapshots(tmp_path)
    names = {s.name for s in snapshots}
    assert names == {"home", "nested/about"}
    assert all(s.content_type == "image/png" for s in snapshots)


def test_jpeg_extensions_map_to_jpeg_content_type(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff")
    (tmp_path / "b.jpeg").write_bytes(b"\xff\xd8\xff")
    snapshots = discover_snapshots(tmp_path)
    assert {s.content_type for s in snapshots} == {"image/jpeg"}


def test_missing_directory_raises_config_error(tmp_path):
    with pytest.raises(SnapvisorConfigError, match="does not exist"):
        discover_snapshots(tmp_path / "nope")


def test_file_path_instead_of_directory_raises(tmp_path):
    file_path = tmp_path / "file.png"
    write_solid_png(file_path)
    with pytest.raises(SnapvisorConfigError, match="not a directory"):
        discover_snapshots(file_path)


# --- 0.2.0: sidecar discovery ---------------------------------------------


def test_metadata_and_trace_sidecars_are_attached_to_their_screenshot(tmp_path):
    from tests.helpers import write_solid_png

    screenshot = write_solid_png(tmp_path / "home.png")
    screenshot.with_name("home.png.argos.json").write_text("{}", encoding="utf-8")
    screenshot.with_name("home.png.pw-trace.zip").write_bytes(b"PK")

    snapshots = discover_snapshots(tmp_path)
    assert len(snapshots) == 1, "sidecars must never be discovered as screenshots"
    assert snapshots[0].metadata_path is not None
    assert snapshots[0].trace_path is not None
    assert snapshots[0].name == "home"


def test_absent_sidecars_leave_the_fields_none(tmp_path):
    from tests.helpers import write_solid_png

    write_solid_png(tmp_path / "home.png")
    snapshot = discover_snapshots(tmp_path)[0]
    assert snapshot.metadata_path is None
    assert snapshot.trace_path is None
