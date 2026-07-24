"""The public ``upload`` entry point and its supporting config resolution.

Implements the Snapvisor upload protocol (wire-compatible with ``@snapvisor/core``):

1. ``POST /builds`` with ``{key, contentType}`` for every unique screenshot →
   returns the build plus signed upload targets for the screenshots the server
   does not already have.
2. Upload each missing screenshot to its target (proxied/presigned POST, or the
   deprecated PUT).
3. ``PUT /builds/{buildId}`` with the full screenshot list and ``final=true`` →
   returns the finalized build with its ``url``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snapvisor.client import SnapvisorClient
from snapvisor.discovery import Snapshot, discover_snapshots
from snapvisor.errors import SnapvisorConfigError, SnapvisorError
from snapvisor.hashing import hash_file


@dataclass(frozen=True)
class ParallelConfig:
    """Configuration for a build assembled from parallel test shards.

    Attributes:
        nonce: A unique identifier shared by every shard of the same build.
        total: Total number of shards; ``-1`` finalizes the build manually.
        index: 1-based index of this shard, or ``None`` for manual finalization.
    """

    nonce: str
    total: int
    index: int | None = None


@dataclass(frozen=True)
class UploadResult:
    """The outcome of a successful :func:`upload`.

    Attributes:
        build_url: The URL of the build on the Snapvisor app.
        build_id: The build's unique identifier.
        build_number: The build's sequential number within the project.
    """

    build_url: str
    build_id: str
    build_number: int


@dataclass(frozen=True)
class _HashedSnapshot:
    snapshot: Snapshot
    key: str


def upload(
    directory: str | Path,
    *,
    token: str | None = None,
    build_name: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    reference_branch: str | None = None,
    parallel: ParallelConfig | None = None,
    api_base_url: str | None = None,
) -> UploadResult:
    """Upload every screenshot under ``directory`` to Snapvisor as one build.

    Args:
        directory: Directory to search recursively for ``.png``/``.jpg``/``.jpeg`` files.
        token: Snapvisor project token. Falls back to ``ARGOS_TOKEN``.
        build_name: Optional build name (for multi-build setups). Falls back to
            ``ARGOS_BUILD_NAME``.
        branch: Git branch. Falls back to ``ARGOS_BRANCH`` then ``git branch --show-current``.
        commit: Git commit SHA. Falls back to ``ARGOS_COMMIT`` then ``git rev-parse HEAD``.
        reference_branch: Branch to use as the comparison baseline.
        parallel: Parallel-shard configuration, if this build is assembled from
            several jobs. Falls back to the ``ARGOS_PARALLEL*`` env vars.
        api_base_url: API base URL. Falls back to ``ARGOS_API_BASE_URL`` then the
            public default.

    Returns:
        An :class:`UploadResult` describing the created build.

    Raises:
        SnapvisorConfigError: If the token, branch, or commit cannot be resolved,
            or the directory is missing/empty.
        SnapvisorAPIError: If any API request fails.
        SnapvisorUploadError: If a screenshot upload fails.
    """
    root = Path(directory)

    resolved_token = token or os.environ.get("ARGOS_TOKEN")
    if not resolved_token:
        raise SnapvisorConfigError(
            "Missing Snapvisor project token. Pass token=... or set ARGOS_TOKEN."
        )

    resolved_branch = _resolve_branch(branch, root)
    resolved_commit = _resolve_commit(commit, root)
    resolved_build_name = build_name or os.environ.get("ARGOS_BUILD_NAME")
    resolved_parallel = parallel if parallel is not None else _parallel_from_env()

    snapshots = discover_snapshots(root)
    if not snapshots:
        raise SnapvisorConfigError(f"No screenshots (.png/.jpg/.jpeg) found under {root}")

    hashed = _hash_snapshots(snapshots)

    from snapvisor import __version__

    sdk_identifier = f"snapvisor-python/{__version__}"

    with SnapvisorClient(
        token=resolved_token,
        api_base_url=api_base_url or os.environ.get("ARGOS_API_BASE_URL"),
        sdk_identifier=sdk_identifier,
    ) as client:
        build = _run_upload(
            client,
            hashed=hashed,
            branch=resolved_branch,
            commit=resolved_commit,
            build_name=resolved_build_name,
            reference_branch=reference_branch,
            parallel=resolved_parallel,
            sdk_identifier=sdk_identifier,
        )

    return _build_to_result(build)


def _run_upload(
    client: SnapvisorClient,
    *,
    hashed: list[_HashedSnapshot],
    branch: str,
    commit: str,
    build_name: str | None,
    reference_branch: str | None,
    parallel: ParallelConfig | None,
    sdk_identifier: str,
) -> dict[str, Any]:
    # De-duplicate the screenshot upload list by key: two identical images share
    # one upload target.
    unique_screenshots: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for item in hashed:
        if item.key in seen_keys:
            continue
        seen_keys.add(item.key)
        unique_screenshots.append({"key": item.key, "contentType": item.snapshot.content_type})

    create_body: dict[str, Any] = {
        "commit": commit,
        "branch": branch,
        "name": build_name,
        "screenshots": unique_screenshots,
        "referenceBranch": reference_branch,
        "argosSdk": sdk_identifier,
    }
    if parallel is not None:
        create_body["parallel"] = True
        create_body["parallelNonce"] = parallel.nonce

    create_response = client.request_json("POST", "/builds", json_body=create_body)
    build_info = create_response["build"]
    build_id = build_info["id"]

    # Upload only the screenshots the server does not already have.
    targets = create_response.get("screenshots") or []
    by_key = {item.key: item for item in hashed}
    for target in targets:
        key = target.get("key")
        item = by_key.get(key)
        if item is None:
            raise SnapvisorError(f"Server requested upload for unknown screenshot key {key!r}")
        client.upload_target(
            target,
            file_path=item.snapshot.path,
            content_type=item.snapshot.content_type,
        )

    # Finalize: send the full screenshot list with names and metadata.
    screenshot_inputs = [
        {
            "key": item.key,
            "name": item.snapshot.name,
            "contentType": item.snapshot.content_type,
            "metadata": None,
            "baseName": None,
            "parentName": None,
            "threshold": None,
            "pwTraceKey": None,
        }
        for item in hashed
    ]

    update_body: dict[str, Any] = {
        "screenshots": screenshot_inputs,
        "final": True,
    }
    if parallel is not None:
        update_body["parallel"] = True
        update_body["parallelTotal"] = parallel.total
        update_body["parallelIndex"] = parallel.index
        # A parallel shard is finalized by the server once every shard reported;
        # only its own last request within the shard is final.
        update_body["final"] = True

    update_response = client.request_json("PUT", f"/builds/{build_id}", json_body=update_body)
    return update_response["build"]


def _hash_snapshots(snapshots: list[Snapshot]) -> list[_HashedSnapshot]:
    return [
        _HashedSnapshot(snapshot=snapshot, key=hash_file(snapshot.path)) for snapshot in snapshots
    ]


def _build_to_result(build: dict[str, Any]) -> UploadResult:
    try:
        return UploadResult(
            build_url=str(build["url"]),
            build_id=str(build["id"]),
            build_number=int(build["number"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SnapvisorError(f"Unexpected build payload from Snapvisor: {build!r}") from error


def _resolve_branch(explicit: str | None, root: Path) -> str:
    branch = explicit or os.environ.get("ARGOS_BRANCH") or _git(["branch", "--show-current"], root)
    if not branch:
        raise SnapvisorConfigError(
            "Could not resolve the git branch. Pass branch=... or set ARGOS_BRANCH."
        )
    return branch


def _resolve_commit(explicit: str | None, root: Path) -> str:
    commit = explicit or os.environ.get("ARGOS_COMMIT") or _git(["rev-parse", "HEAD"], root)
    if not commit:
        raise SnapvisorConfigError(
            "Could not resolve the git commit. Pass commit=... or set ARGOS_COMMIT."
        )
    return commit


def _parallel_from_env() -> ParallelConfig | None:
    if os.environ.get("ARGOS_PARALLEL", "").lower() not in ("1", "true", "yes"):
        return None
    nonce = os.environ.get("ARGOS_PARALLEL_NONCE")
    total_raw = os.environ.get("ARGOS_PARALLEL_TOTAL")
    if not nonce or not total_raw:
        raise SnapvisorConfigError(
            "ARGOS_PARALLEL is set but ARGOS_PARALLEL_NONCE and "
            "ARGOS_PARALLEL_TOTAL are required for parallel builds."
        )
    index_raw = os.environ.get("ARGOS_PARALLEL_INDEX")
    return ParallelConfig(
        nonce=nonce,
        total=int(total_raw),
        index=int(index_raw) if index_raw else None,
    )


def _git(args: list[str], cwd: Path) -> str | None:
    """Run a read-only git command, returning stripped stdout or ``None`` on failure."""
    directory = cwd if cwd.is_dir() else Path.cwd()
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=directory,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
