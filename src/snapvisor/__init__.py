"""Snapvisor — the official Python SDK for the Snapvisor visual regression platform.

Wire-compatible with the Argos upload protocol (Snapvisor is a fork of Argos).
The public surface is intentionally small: :func:`upload` and :class:`UploadResult`.
"""

from __future__ import annotations

from snapvisor.errors import SnapvisorAPIError, SnapvisorError
from snapvisor.upload import ParallelConfig, UploadResult, upload

__all__ = [
    "upload",
    "UploadResult",
    "ParallelConfig",
    "SnapvisorError",
    "SnapvisorAPIError",
    "__version__",
]

__version__ = "0.1.0"
