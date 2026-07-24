"""Screenshot discovery and content-type resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from snapvisor.errors import SnapvisorConfigError

# Extensions Snapvisor accepts as screenshots, mapped to their content type.
_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


@dataclass(frozen=True)
class Snapshot:
    """A discovered screenshot file.

    Attributes:
        path: Absolute path to the file on disk.
        name: The screenshot name reported to Snapvisor — the path relative to
            the upload directory, using forward slashes, without extension.
        content_type: The MIME type inferred from the file extension.
    """

    path: Path
    name: str
    content_type: str


def content_type_for(path: Path) -> str | None:
    """Return the Snapvisor content type for ``path``, or ``None`` if unsupported."""
    return _CONTENT_TYPES.get(path.suffix.lower())


def discover_snapshots(directory: str | Path) -> list[Snapshot]:
    """Recursively find every supported screenshot under ``directory``.

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
        snapshots.append(
            Snapshot(
                path=file_path.resolve(),
                name=name,
                content_type=content_type,
            )
        )
    return snapshots
