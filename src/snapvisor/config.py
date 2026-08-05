"""Environment-variable resolution.

Snapvisor is a fork of Argos and its API, CI recipes, and existing pipelines all
speak ``ARGOS_*``. 0.2.0 adds ``SNAPVISOR_*`` aliases without breaking any of
that: every lookup prefers ``SNAPVISOR_<NAME>`` and falls back to
``ARGOS_<NAME>``, so a pipeline already wired for Argos keeps working untouched
and a new one can use the product's own name.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

SNAPVISOR_PREFIX = "SNAPVISOR_"
ARGOS_PREFIX = "ARGOS_"


def env(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Read ``SNAPVISOR_<name>``, falling back to ``ARGOS_<name>``.

    Args:
        name: The unprefixed variable name, e.g. ``"TOKEN"``.
        environ: Environment to read from; defaults to :data:`os.environ`.

    Returns:
        The first non-empty value found, or ``None``.
    """
    source = os.environ if environ is None else environ
    for prefix in (SNAPVISOR_PREFIX, ARGOS_PREFIX):
        value = source.get(prefix + name)
        if value:
            return value
    return None


def env_bool(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """Read a boolean-ish env var (``1``/``true``/``yes``, case-insensitive)."""
    value = env(name, environ)
    return bool(value) and value.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, environ: Mapping[str, str] | None = None) -> int | None:
    """Read an integer env var, returning ``None`` when unset or unparseable."""
    value = env(name, environ)
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None
