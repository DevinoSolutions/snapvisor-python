"""The public ``upload`` entry point and its supporting config resolution.

Implements the Snapvisor upload protocol (wire-compatible with ``@snapvisor/core``):

1. ``POST /builds`` with ``{key, contentType}`` for every unique screenshot →
   returns the build plus signed upload targets for the screenshots (and
   Playwright traces) the server does not already have.
2. Upload each missing file to its target (proxied/presigned POST, or the
   deprecated PUT) — concurrently, with a bounded worker pool.
3. ``PUT /builds/{buildId}`` with the full screenshot list and ``final=true`` →
   returns the finalized build with its ``url``.

This choreography stays handwritten: the storage leg does not touch the Snapvisor
API at all (it hits S3/Backblaze with a policy payload), and the ``createBuild``
body is an ``allOf`` over a ``oneOf``, which a generated client renders as a
union the caller has to unwrap. Everything *else* the API offers is reachable
through :class:`snapvisor.Snapvisor`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snapvisor.ci import CiEnvironment, detect_ci
from snapvisor.client import AsyncSnapvisorClient, SnapvisorClient
from snapvisor.config import env, env_bool, env_int
from snapvisor.discovery import TRACE_CONTENT_TYPE, Snapshot, discover_snapshots
from snapvisor.errors import SnapvisorConfigError, SnapvisorError
from snapvisor.hashing import hash_file
from snapvisor.transport import RetryConfig

#: Default number of screenshot uploads in flight at once. A build may carry up
#: to 5000 screenshots (`MAX_SCREENSHOTS_PER_BUILD`); uploading them one at a
#: time — as 0.1.0 did — is 5000 sequential round trips.
DEFAULT_CONCURRENCY = 8

#: Build modes the API accepts.
BUILD_MODES = ("ci", "monitoring")


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
    """A screenshot with everything the API needs to know about it."""

    snapshot: Snapshot
    key: str
    metadata: dict[str, Any] | None = None
    threshold: float | None = None
    base_name: str | None = None
    parent_name: str | None = None
    trace_key: str | None = None


@dataclass(frozen=True)
class BuildOptions:
    """The ``createBuild`` fields beyond commit/branch/screenshots.

    0.1.0 sent 7 of the API's 20 ``createBuild`` body fields, so builds uploaded
    from Python could not be attached to a pull request, could not declare their
    CI provider or run, could not be marked as a merge-queue or subset build, and
    could not carry a reference commit. Every one of them is reachable here.

    Attributes:
        pr_number: The pull request this build belongs to.
        pr_head_commit: The head commit of that pull request.
        reference_commit: Commit to compare against, overriding branch matching.
        reference_branch: Branch to compare against.
        parent_commits: Candidate parent commits for baseline resolution.
        mode: ``"ci"`` (default server-side) or ``"monitoring"``.
        ci_provider: The CI provider identifier, e.g. ``"github-actions"``.
        run_id: The CI run this build belongs to.
        run_attempt: 1-based attempt number of that run.
        skipped: Mark the build as skipped — it compares nothing and always succeeds.
        merge_queue: Whether the build was produced inside a merge queue.
        merge_queue_pr_numbers: PRs aggregated by the merge-queue build.
        subset: Whether the build carries only a subset of the suite's screenshots.
        metadata: Build-level metadata (``BuildMetadata``), sent with the final
            ``updateBuild`` request.
    """

    pr_number: int | None = None
    pr_head_commit: str | None = None
    reference_commit: str | None = None
    reference_branch: str | None = None
    parent_commits: Sequence[str] | None = None
    mode: str | None = None
    ci_provider: str | None = None
    run_id: str | None = None
    run_attempt: int | None = None
    skipped: bool | None = None
    merge_queue: bool | None = None
    merge_queue_pr_numbers: Sequence[int] | None = None
    subset: bool | None = None
    metadata: Mapping[str, Any] | None = None

    def merged_with_ci(self, ci: CiEnvironment | None) -> BuildOptions:
        """Fill unset fields from a detected CI environment (explicit values win)."""
        if ci is None:
            return self
        return BuildOptions(
            pr_number=self.pr_number if self.pr_number is not None else ci.pr_number,
            pr_head_commit=self.pr_head_commit or ci.pr_head_commit,
            reference_commit=self.reference_commit,
            reference_branch=self.reference_branch,
            parent_commits=self.parent_commits,
            mode=self.mode,
            ci_provider=self.ci_provider or ci.provider,
            run_id=self.run_id or ci.run_id,
            run_attempt=self.run_attempt if self.run_attempt is not None else ci.run_attempt,
            skipped=self.skipped,
            merge_queue=self.merge_queue,
            merge_queue_pr_numbers=self.merge_queue_pr_numbers,
            subset=self.subset,
            metadata=self.metadata,
        )


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
    build: BuildOptions | None = None,
    threshold: float | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    detect_ci_environment: bool = True,
    retry: RetryConfig | None = None,
) -> UploadResult:
    """Upload every screenshot under ``directory`` to Snapvisor as one build.

    Args:
        directory: Directory to search recursively for ``.png``/``.jpg``/``.jpeg`` files.
            A ``<screenshot>.argos.json`` sidecar supplies that screenshot's metadata
            (and, via its ``transient`` block, its ``threshold``/``baseName``/
            ``parentName``); a ``<screenshot>.pw-trace.zip`` sidecar is uploaded
            as its Playwright trace.
        token: Snapvisor project token. Falls back to ``SNAPVISOR_TOKEN`` then ``ARGOS_TOKEN``.
        build_name: Optional build name (for multi-build setups). Falls back to
            ``SNAPVISOR_BUILD_NAME``/``ARGOS_BUILD_NAME``.
        branch: Git branch. Falls back to the env vars, the detected CI
            environment, then ``git branch --show-current``.
        commit: Git commit SHA. Falls back to the env vars, the detected CI
            environment, then ``git rev-parse HEAD``.
        reference_branch: Branch to use as the comparison baseline.
        parallel: Parallel-shard configuration, if this build is assembled from
            several jobs. Falls back to the ``SNAPVISOR_PARALLEL*``/``ARGOS_PARALLEL*``
            env vars.
        api_base_url: API base URL. Falls back to ``SNAPVISOR_API_BASE_URL``/
            ``ARGOS_API_BASE_URL`` then the public default.
        build: The rest of the ``createBuild`` surface — pull-request context, CI
            run identity, merge-queue and subset flags, build metadata.
        threshold: Default per-screenshot diff threshold, applied to screenshots
            whose sidecar metadata does not set one.
        concurrency: Maximum file uploads in flight. ``1`` restores 0.1.0's
            serial behaviour.
        detect_ci_environment: Auto-detect the CI provider and fill in
            ``ciProvider``/``runId``/``runAttempt``/``prNumber``/``prHeadCommit``
            (and branch/commit) when not given explicitly.
        retry: Retry, backoff, and rate-limit policy. Defaults to 3 retries with
            draft-8 ``RateLimit`` handling.

    Returns:
        An :class:`UploadResult` describing the created build.

    Raises:
        SnapvisorConfigError: If the token, branch, or commit cannot be resolved,
            or the directory is missing/empty.
        SnapvisorAPIError: If any API request fails.
        SnapvisorUploadError: If a screenshot upload fails.
    """
    plan = _plan_upload(
        directory,
        token=token,
        build_name=build_name,
        branch=branch,
        commit=commit,
        reference_branch=reference_branch,
        parallel=parallel,
        build=build,
        threshold=threshold,
        detect_ci_environment=detect_ci_environment,
    )

    with SnapvisorClient(
        token=plan.token,
        api_base_url=api_base_url or env("API_BASE_URL"),
        sdk_identifier=plan.sdk_identifier,
        retry=retry,
    ) as client:
        create_response = client.request_json("POST", "/builds", json_body=plan.create_body)
        build_info = create_response["build"]
        targets = _resolve_targets(create_response, plan)

        if targets:
            _upload_concurrently(client, targets, concurrency=concurrency)

        update_response = client.request_json(
            "PUT", f"/builds/{build_info['id']}", json_body=plan.update_body
        )

    return _build_to_result(update_response["build"])


async def aupload(
    directory: str | Path,
    *,
    token: str | None = None,
    build_name: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    reference_branch: str | None = None,
    parallel: ParallelConfig | None = None,
    api_base_url: str | None = None,
    build: BuildOptions | None = None,
    threshold: float | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    detect_ci_environment: bool = True,
    retry: RetryConfig | None = None,
) -> UploadResult:
    """Asynchronous twin of :func:`upload`, with identical arguments and semantics."""
    plan = _plan_upload(
        directory,
        token=token,
        build_name=build_name,
        branch=branch,
        commit=commit,
        reference_branch=reference_branch,
        parallel=parallel,
        build=build,
        threshold=threshold,
        detect_ci_environment=detect_ci_environment,
    )

    async with AsyncSnapvisorClient(
        token=plan.token,
        api_base_url=api_base_url or env("API_BASE_URL"),
        sdk_identifier=plan.sdk_identifier,
        retry=retry,
    ) as client:
        create_response = await client.request_json("POST", "/builds", json_body=plan.create_body)
        build_info = create_response["build"]
        targets = _resolve_targets(create_response, plan)

        if targets:
            await _aupload_concurrently(client, targets, concurrency=concurrency)

        update_response = await client.request_json(
            "PUT", f"/builds/{build_info['id']}", json_body=plan.update_body
        )

    return _build_to_result(update_response["build"])


def skip_build(
    *,
    token: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    build_name: str | None = None,
    api_base_url: str | None = None,
    parallel: ParallelConfig | None = None,
    retry: RetryConfig | None = None,
) -> dict[str, Any]:
    """Create a build marked ``skipped`` — it compares nothing and always succeeds.

    Used to unblock required status checks on branches where the visual suite
    does not run.

    Returns:
        The created build payload.
    """
    resolved_token = _resolve_token(token)
    root = Path.cwd()
    ci = detect_ci()
    body: dict[str, Any] = {
        "commit": _resolve_commit(commit, root, ci),
        "branch": _resolve_branch(branch, root, ci),
        "name": build_name or env("BUILD_NAME"),
        "screenshots": [],
        "skipped": True,
        "argosSdk": _sdk_identifier(),
    }
    if ci is not None:
        body["ciProvider"] = ci.provider
        body["runId"] = ci.run_id
    if parallel is not None:
        body["parallel"] = True
        body["parallelNonce"] = parallel.nonce
    with SnapvisorClient(
        token=resolved_token,
        api_base_url=api_base_url or env("API_BASE_URL"),
        sdk_identifier=_sdk_identifier(),
        retry=retry,
    ) as client:
        return client.request_json("POST", "/builds", json_body=body)["build"]


def finalize_builds(
    *,
    parallel_nonce: str,
    token: str | None = None,
    api_base_url: str | None = None,
    retry: RetryConfig | None = None,
) -> dict[str, Any]:
    """Finalize every parallel build sharing ``parallel_nonce`` (``POST /builds/finalize``).

    Call this once all shards have uploaded when the shard count is not known up
    front (``ParallelConfig.total == -1``).

    Returns:
        The API's finalize payload, listing the builds it finalized.
    """
    with SnapvisorClient(
        token=_resolve_token(token),
        api_base_url=api_base_url or env("API_BASE_URL"),
        sdk_identifier=_sdk_identifier(),
        retry=retry,
    ) as client:
        return client.request_json(
            "POST", "/builds/finalize", json_body={"parallelNonce": parallel_nonce}
        )


def find_baseline(
    *,
    commits: Sequence[str],
    token: str | None = None,
    api_base_url: str | None = None,
    retry: RetryConfig | None = None,
) -> dict[str, Any]:
    """Ask the API which of ``commits`` can serve as a baseline (``POST /baseline``).

    Returns:
        The API's baseline payload.
    """
    with SnapvisorClient(
        token=_resolve_token(token),
        api_base_url=api_base_url or env("API_BASE_URL"),
        sdk_identifier=_sdk_identifier(),
        retry=retry,
    ) as client:
        return client.request_json("POST", "/baseline", json_body={"commits": list(commits)})


@dataclass(frozen=True)
class _UploadPlan:
    """Everything resolved before the first request goes out."""

    token: str
    sdk_identifier: str
    hashed: list[_HashedSnapshot]
    create_body: dict[str, Any]
    update_body: dict[str, Any]


@dataclass(frozen=True)
class _UploadTask:
    target: dict[str, Any]
    file_path: Path
    content_type: str


def _sdk_identifier() -> str:
    from snapvisor import __version__

    return f"snapvisor-python/{__version__}"


def _resolve_token(token: str | None) -> str:
    resolved = token or env("TOKEN")
    if not resolved:
        raise SnapvisorConfigError(
            "Missing Snapvisor project token. Pass token=... or set "
            "SNAPVISOR_TOKEN (or ARGOS_TOKEN)."
        )
    return resolved


def _plan_upload(
    directory: str | Path,
    *,
    token: str | None,
    build_name: str | None,
    branch: str | None,
    commit: str | None,
    reference_branch: str | None,
    parallel: ParallelConfig | None,
    build: BuildOptions | None,
    threshold: float | None,
    detect_ci_environment: bool,
) -> _UploadPlan:
    root = Path(directory)
    resolved_token = _resolve_token(token)
    ci = detect_ci() if detect_ci_environment else None

    options = (build or BuildOptions()).merged_with_ci(ci)
    if reference_branch is not None:
        options = _with_reference_branch(options, reference_branch)
    if options.mode is not None and options.mode not in BUILD_MODES:
        raise SnapvisorConfigError(
            f"Invalid build mode {options.mode!r}; expected one of {', '.join(BUILD_MODES)}."
        )

    resolved_branch = _resolve_branch(branch, root, ci)
    resolved_commit = _resolve_commit(commit, root, ci)
    resolved_build_name = build_name or env("BUILD_NAME")
    resolved_parallel = parallel if parallel is not None else _parallel_from_env(ci)

    snapshots = discover_snapshots(root)
    if not snapshots:
        raise SnapvisorConfigError(f"No screenshots (.png/.jpg/.jpeg) found under {root}")

    hashed = _hash_snapshots(snapshots, default_threshold=threshold)
    sdk_identifier = _sdk_identifier()

    return _UploadPlan(
        token=resolved_token,
        sdk_identifier=sdk_identifier,
        hashed=hashed,
        create_body=_create_body(
            hashed=hashed,
            branch=resolved_branch,
            commit=resolved_commit,
            build_name=resolved_build_name,
            parallel=resolved_parallel,
            options=options,
            sdk_identifier=sdk_identifier,
        ),
        update_body=_update_body(hashed=hashed, parallel=resolved_parallel, options=options),
    )


def _with_reference_branch(options: BuildOptions, reference_branch: str) -> BuildOptions:
    return BuildOptions(
        pr_number=options.pr_number,
        pr_head_commit=options.pr_head_commit,
        reference_commit=options.reference_commit,
        reference_branch=reference_branch,
        parent_commits=options.parent_commits,
        mode=options.mode,
        ci_provider=options.ci_provider,
        run_id=options.run_id,
        run_attempt=options.run_attempt,
        skipped=options.skipped,
        merge_queue=options.merge_queue,
        merge_queue_pr_numbers=options.merge_queue_pr_numbers,
        subset=options.subset,
        metadata=options.metadata,
    )


def _create_body(
    *,
    hashed: list[_HashedSnapshot],
    branch: str,
    commit: str,
    build_name: str | None,
    parallel: ParallelConfig | None,
    options: BuildOptions,
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

    trace_keys: list[str] = []
    for item in hashed:
        if item.trace_key and item.trace_key not in trace_keys:
            trace_keys.append(item.trace_key)

    body: dict[str, Any] = {
        "commit": commit,
        "branch": branch,
        "name": build_name,
        "screenshots": unique_screenshots,
        "referenceBranch": options.reference_branch,
        "argosSdk": sdk_identifier,
    }
    if trace_keys:
        body["pwTraceKeys"] = trace_keys
    if parallel is not None:
        body["parallel"] = True
        body["parallelNonce"] = parallel.nonce

    optional: dict[str, Any] = {
        "prNumber": options.pr_number,
        "prHeadCommit": options.pr_head_commit,
        "referenceCommit": options.reference_commit,
        "parentCommits": list(options.parent_commits) if options.parent_commits else None,
        "mode": options.mode,
        "ciProvider": options.ci_provider,
        "runId": options.run_id,
        "runAttempt": options.run_attempt,
        "skipped": options.skipped,
        "mergeQueue": options.merge_queue,
        "mergeQueuePrNumbers": (
            list(options.merge_queue_pr_numbers) if options.merge_queue_pr_numbers else None
        ),
        "subset": options.subset,
    }
    body.update({key: value for key, value in optional.items() if value is not None})
    return body


def _update_body(
    *,
    hashed: list[_HashedSnapshot],
    parallel: ParallelConfig | None,
    options: BuildOptions,
) -> dict[str, Any]:
    screenshot_inputs = [
        {
            "key": item.key,
            "name": item.snapshot.name,
            "contentType": item.snapshot.content_type,
            "metadata": item.metadata,
            "baseName": item.base_name,
            "parentName": item.parent_name,
            "threshold": item.threshold,
            "pwTraceKey": item.trace_key,
        }
        for item in hashed
    ]

    body: dict[str, Any] = {"screenshots": screenshot_inputs, "final": True}
    if options.metadata is not None:
        body["metadata"] = dict(options.metadata)
    if parallel is not None:
        body["parallel"] = True
        body["parallelTotal"] = parallel.total
        body["parallelIndex"] = parallel.index
        # A parallel shard is finalized by the server once every shard reported;
        # only its own last request within the shard is final.
        body["final"] = True
    return body


def _resolve_targets(create_response: Mapping[str, Any], plan: _UploadPlan) -> list[_UploadTask]:
    """Match the server's requested upload targets to local files.

    Both ``screenshots`` and ``pwTraces`` are honoured; 0.1.0 silently discarded
    the trace targets, so Playwright traces never reached the platform.
    """
    by_key = {item.key: item for item in plan.hashed}
    by_trace_key = {item.trace_key: item for item in plan.hashed if item.trace_key}

    tasks: list[_UploadTask] = []
    for target in create_response.get("screenshots") or []:
        key = target.get("key")
        item = by_key.get(key)
        if item is None:
            raise SnapvisorError(f"Server requested upload for unknown screenshot key {key!r}")
        tasks.append(
            _UploadTask(
                target=target,
                file_path=item.snapshot.path,
                content_type=item.snapshot.content_type,
            )
        )

    for target in create_response.get("pwTraces") or []:
        key = target.get("key")
        item = by_trace_key.get(key)
        if item is None or item.snapshot.trace_path is None:
            raise SnapvisorError(f"Server requested upload for unknown trace key {key!r}")
        tasks.append(
            _UploadTask(
                target=target,
                file_path=item.snapshot.trace_path,
                content_type=TRACE_CONTENT_TYPE,
            )
        )
    return tasks


def _upload_concurrently(
    client: SnapvisorClient,
    tasks: Sequence[_UploadTask],
    *,
    concurrency: int,
) -> None:
    """Upload every task with a bounded worker pool, failing on the first error."""
    workers = max(1, min(concurrency, len(tasks)))
    if workers == 1:
        for task in tasks:
            client.upload_target(
                task.target, file_path=task.file_path, content_type=task.content_type
            )
        return

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="snapvisor-upload") as pool:
        futures = [
            pool.submit(
                client.upload_target,
                task.target,
                file_path=task.file_path,
                content_type=task.content_type,
            )
            for task in tasks
        ]
        # Surface the first failure with its original traceback, exactly as the
        # serial path did; the pool shuts down as the context manager exits.
        for future in futures:
            future.result()


async def _aupload_concurrently(
    client: AsyncSnapvisorClient,
    tasks: Sequence[_UploadTask],
    *,
    concurrency: int,
) -> None:
    """Asynchronous twin of :func:`_upload_concurrently`."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(task: _UploadTask) -> None:
        async with semaphore:
            await client.upload_target(
                task.target, file_path=task.file_path, content_type=task.content_type
            )

    await asyncio.gather(*(run(task) for task in tasks))


def _hash_snapshots(
    snapshots: Iterable[Snapshot], *, default_threshold: float | None = None
) -> list[_HashedSnapshot]:
    hashed: list[_HashedSnapshot] = []
    for snapshot in snapshots:
        metadata, threshold, base_name, parent_name = _read_metadata(snapshot)
        hashed.append(
            _HashedSnapshot(
                snapshot=snapshot,
                key=hash_file(snapshot.path),
                metadata=metadata,
                threshold=threshold if threshold is not None else default_threshold,
                base_name=base_name,
                parent_name=parent_name,
                trace_key=hash_file(snapshot.trace_path) if snapshot.trace_path else None,
            )
        )
    return hashed


def _read_metadata(
    snapshot: Snapshot,
) -> tuple[dict[str, Any] | None, float | None, str | None, str | None]:
    """Read a screenshot's sidecar metadata and split off its transient fields.

    ``transient`` carries values the API takes as *ScreenshotInput* fields rather
    than as metadata (``threshold``, ``baseName``, ``parentName``); the TypeScript
    SDK strips it before sending, and so do we.
    """
    if snapshot.metadata_path is None:
        return None, None, None, None
    try:
        payload = json.loads(snapshot.metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SnapvisorConfigError(
            f"Failed to read screenshot metadata {snapshot.metadata_path}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise SnapvisorConfigError(
            f"Screenshot metadata {snapshot.metadata_path} is not a JSON object."
        )

    transient = payload.pop("transient", None)
    threshold = base_name = parent_name = None
    if isinstance(transient, dict):
        raw_threshold = transient.get("threshold")
        threshold = float(raw_threshold) if isinstance(raw_threshold, (int, float)) else None
        base_name = (
            transient.get("baseName") if isinstance(transient.get("baseName"), str) else None
        )
        parent_name = (
            transient.get("parentName") if isinstance(transient.get("parentName"), str) else None
        )
    return (payload or None), threshold, base_name, parent_name


def _build_to_result(build: Mapping[str, Any]) -> UploadResult:
    try:
        return UploadResult(
            build_url=str(build["url"]),
            build_id=str(build["id"]),
            build_number=int(build["number"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SnapvisorError(f"Unexpected build payload from Snapvisor: {build!r}") from error


def _resolve_branch(explicit: str | None, root: Path, ci: CiEnvironment | None = None) -> str:
    branch = (
        explicit
        or env("BRANCH")
        or (ci.branch if ci else None)
        or _git(["branch", "--show-current"], root)
    )
    if not branch:
        raise SnapvisorConfigError(
            "Could not resolve the git branch. Pass branch=... or set SNAPVISOR_BRANCH."
        )
    return branch


def _resolve_commit(explicit: str | None, root: Path, ci: CiEnvironment | None = None) -> str:
    commit = (
        explicit
        or env("COMMIT")
        or (ci.commit if ci else None)
        or _git(["rev-parse", "HEAD"], root)
    )
    if not commit:
        raise SnapvisorConfigError(
            "Could not resolve the git commit. Pass commit=... or set SNAPVISOR_COMMIT."
        )
    return commit


def _parallel_from_env(ci: CiEnvironment | None = None) -> ParallelConfig | None:
    if not env_bool("PARALLEL"):
        return None
    nonce = env("PARALLEL_NONCE") or (ci.nonce if ci else None)
    total = env_int("PARALLEL_TOTAL")
    if not nonce or total is None:
        raise SnapvisorConfigError(
            "SNAPVISOR_PARALLEL is set but SNAPVISOR_PARALLEL_NONCE and "
            "SNAPVISOR_PARALLEL_TOTAL are required for parallel builds "
            "(ARGOS_* aliases are also accepted)."
        )
    return ParallelConfig(nonce=nonce, total=total, index=env_int("PARALLEL_INDEX"))


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
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
