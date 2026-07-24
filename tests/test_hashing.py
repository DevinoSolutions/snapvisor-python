"""Hashing is the SHA-256 hex of raw file bytes, matching the API key pattern."""

from __future__ import annotations

import hashlib
import re

from snapvisor.hashing import hash_file
from tests.helpers import write_solid_png

_KEY_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")


def test_hash_file_matches_sha256_of_bytes(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(b"the quick brown fox")
    expected = hashlib.sha256(b"the quick brown fox").hexdigest()
    assert hash_file(path) == expected


def test_hash_file_returns_valid_screenshot_key(tmp_path):
    png = write_solid_png(tmp_path / "red.png", rgb=(255, 0, 0))
    key = hash_file(png)
    assert _KEY_PATTERN.match(key)


def test_identical_pngs_hash_identically(tmp_path):
    a = write_solid_png(tmp_path / "a.png", rgb=(0, 128, 255))
    b = write_solid_png(tmp_path / "b.png", rgb=(0, 128, 255))
    assert hash_file(a) == hash_file(b)


def test_different_pngs_hash_differently(tmp_path):
    a = write_solid_png(tmp_path / "a.png", rgb=(0, 0, 0))
    b = write_solid_png(tmp_path / "b.png", rgb=(255, 255, 255))
    assert hash_file(a) != hash_file(b)
