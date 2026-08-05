"""A minimal on-disk credential store for the CLI.

Deliberately dependency-free: the SDK's only runtime dependencies are ``httpx``
and ``attrs``, and a keyring backend is not worth breaking that for. The token
file is created with owner-only permissions on POSIX; on Windows it inherits the
user profile's ACL, which is the same protection ``gh`` and ``npm`` rely on.

Location, in precedence order:

1. ``$SNAPVISOR_CONFIG_DIR/credentials.json``
2. ``$XDG_CONFIG_HOME/snapvisor/credentials.json``
3. ``%APPDATA%\\snapvisor\\credentials.json`` (Windows)
4. ``~/.config/snapvisor/credentials.json``
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

FILE_NAME = "credentials.json"


def config_dir() -> Path:
    """The directory the credential file lives in."""
    explicit = os.environ.get("SNAPVISOR_CONFIG_DIR")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "snapvisor"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "snapvisor"
    return Path.home() / ".config" / "snapvisor"


def credentials_path() -> Path:
    """The full path to the credential file (which may not exist)."""
    return config_dir() / FILE_NAME


@dataclass(frozen=True)
class Credentials:
    """A stored token and the API it belongs to.

    Attributes:
        token: The personal access token.
        api_base_url: The API the token authenticates against.
    """

    token: str
    api_base_url: str | None = None


def load() -> Credentials | None:
    """Read stored credentials, or ``None`` when nothing is stored.

    Returns ``None`` rather than raising on a corrupt file: a broken credential
    cache must never be the reason a CLI command cannot run with an env-var token.
    """
    path = credentials_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        return None
    api_base_url = payload.get("apiBaseUrl")
    return Credentials(
        token=token, api_base_url=api_base_url if isinstance(api_base_url, str) else None
    )


def save(credentials: Credentials) -> Path:
    """Write credentials to disk with owner-only permissions.

    Returns:
        The path written.
    """
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"token": credentials.token}
    if credentials.api_base_url:
        payload["apiBaseUrl"] = credentials.api_base_url
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # Best effort: some filesystems (FAT, some network mounts) reject chmod.
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def clear() -> bool:
    """Delete stored credentials.

    Returns:
        ``True`` if a file was removed, ``False`` if there was nothing to remove.
    """
    path = credentials_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True
