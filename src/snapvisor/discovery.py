"""Screenshot discovery, sidecar resolution, and content-type resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from snapvisor.errors import SnapvisorConfigError

# Extensions Snapvisor accepts as screenshots, mapped to their content type.
# Reconciled with the API's `SnapshotContentTypeSchema`, which accepts
# `image/png` and `image/jpeg` for screenshots and `application/zip` for
# Playwright traces (traces are discovered as sidecars, never as screenshots).
_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

#: Content type the API expects for a Playwright trace upload.
TRACE_CONTENT_TYPE = "application/zip"

#: Sidecar suffixes, matching `@snapvisor/util`'s `getMetadataPath` and
#: `getPlaywrightTracePath` so the Python and TypeScript SDKs read the artifacts
#: any test-runner integration writes.
METADATA_SUFFIX = ".argos.json"
TRACE_SUFFIX = ".pw-trace.zip"


@dataclass(frozen=True)
class Snapshot:
    """A discovered screenshot file and its sidecars.

    Attributes:
        path: Absolute path to the file on disk.
        name: The screenshot name reported to Snapvisor — the path relative to
            the upload directory, using forward slashes, without extension.
        content_type: The MIME type inferred from the file extension.
        metadata_path: The ``<screenshot>.argos.json`` sidecar, when present.
        trace_path: The ``<screenshot>.pw-trace.zip`` Playwright trace, when present.
    """

    path: Path
    name: str
    content_type: str
    metadata_path: Path | None = None
    trace_path: Path | None = None


def content_type_for(path: Path) -> str | None:
    """Return the Snapvisor content type for ``path``, or ``None`` if unsupported."""
    return _CONTENT_TYPES.get(path.suffix.lower())


def metadata_path_for(screenshot: Path) -> Path:
    """The metadata sidecar path for a screenshot (may not exist)."""
    return screenshot.with_name(screenshot.name + METADATA_SUFFIX)


def trace_path_for(screenshot: Path) -> Path:
    """The Playwright trace sidecar path for a screenshot (may not exist)."""
    return screenshot.with_name(screenshot.name + TRACE_SUFFIX)


def discover_snapshots(directory: str | Path) -> list[Snapshot]:
    """Recursively find every supported screenshot under ``directory``.

    Sidecar metadata and Playwright traces next to a screenshot are attached to
    it; they are never returned as screenshots in their own right.

    Raises:
        SnapvisorConfigError: If ``directory`` does not exist or is not a directory.
    """
    root = Path(directory)
    if not root.exists():
        raise SnapvisorConfigError(f"Upload directory does not exist: {root}")
    if not root.is_dir():
        raise SnapvisorConfigError(f"Upload path is not a directory: {root}")

    snapshots: list[Snapshot] = []
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        content_type = content_type_for(file_path)
        if content_type is None:
            continue
        relative = file_path.relative_to(root).with_suffix("")
        name = relative.as_posix()
        metadata_path = metadata_path_for(file_path)
        trace_path = trace_path_for(file_path)
        snapshots.append(
            Snapshot(
                path=file_path.resolve(),
                name=name,
                content_type=content_type,
                metadata_path=metadata_path.resolve() if metadata_path.is_file() else None,
                trace_path=trace_path.resolve() if trace_path.is_file() else None,
            )
        )
    return snapshots
