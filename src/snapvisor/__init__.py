"""Snapvisor — the official Python SDK for the Snapvisor visual regression platform.

Two entry points, one for each job:

* :func:`upload` — the screenshot upload protocol, unchanged since 0.1.0 and
  wire-compatible with ``@snapvisor/core``::

      import snapvisor
      result = snapvisor.upload("screenshots/", token="…")
      print(result.build_url)

* :class:`Snapvisor` — every other operation the platform publishes (builds,
  projects, comments, reviews, changes, deployments, analytics, auth), generated
  from the live OpenAPI document so coverage cannot fall behind the API::

      from snapvisor import Snapvisor
      sv = Snapvisor(token="snapvisor_pat_…")
      for build in sv.builds.auto_paginate("listBuilds", owner="acme", project="web"):
          print(build.number, build.status)

Both share one transport policy: retries with exponential backoff, IETF draft-8
rate-limit handling on 429, request-id correlation, and a typed error hierarchy.

Environment variables are read as ``SNAPVISOR_*`` first and ``ARGOS_*`` second,
so pipelines wired for Argos keep working unchanged.
"""

from __future__ import annotations

from snapvisor.api import AsyncSnapvisor, Resource, Snapvisor
from snapvisor.ci import CiEnvironment, detect_ci
from snapvisor.discovery import Snapshot, discover_snapshots
from snapvisor.errors import (
    SnapvisorAPIError,
    SnapvisorAuthError,
    SnapvisorConfigError,
    SnapvisorConflictError,
    SnapvisorError,
    SnapvisorForbiddenError,
    SnapvisorNotFoundError,
    SnapvisorRateLimitError,
    SnapvisorServerError,
    SnapvisorUploadError,
)
from snapvisor.pagination import Page, auto_paginate, iter_pages
from snapvisor.transport import RetryConfig
from snapvisor.upload import (
    BuildOptions,
    ParallelConfig,
    UploadResult,
    aupload,
    finalize_builds,
    find_baseline,
    skip_build,
    upload,
)

__all__ = [
    # Upload protocol (0.1.0 surface, unchanged)
    "upload",
    "UploadResult",
    "ParallelConfig",
    "SnapvisorError",
    "SnapvisorAPIError",
    # Upload protocol (0.2.0 additions)
    "aupload",
    "BuildOptions",
    "skip_build",
    "finalize_builds",
    "find_baseline",
    "Snapshot",
    "discover_snapshots",
    # Full API surface
    "Snapvisor",
    "AsyncSnapvisor",
    "Resource",
    # Transport, pagination, CI
    "RetryConfig",
    "Page",
    "auto_paginate",
    "iter_pages",
    "CiEnvironment",
    "detect_ci",
    # Error taxonomy
    "SnapvisorConfigError",
    "SnapvisorUploadError",
    "SnapvisorAuthError",
    "SnapvisorForbiddenError",
    "SnapvisorNotFoundError",
    "SnapvisorConflictError",
    "SnapvisorRateLimitError",
    "SnapvisorServerError",
    "__version__",
]

__version__ = "0.2.0"
