"""File hashing.

The screenshot key is the lowercase SHA-256 hex digest of the file's raw bytes
— identical to what ``@snapvisor/core`` computes for the uploaded bytes. The API
validates keys against ``^[A-Fa-f0-9]{64}$`` and dedupes uploads by this key.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024


def hash_file(path: str | Path) -> str:
    """Return the SHA-256 hex digest of the bytes of the file at ``path``."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()
