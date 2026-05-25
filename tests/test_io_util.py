"""Tests for the long-path I/O helpers.

The actual ``\\\\?\\`` prefix only matters on Windows; on Linux/macOS the
helpers are no-ops. We verify both the prefix-application logic (across
platforms by checking what the helper would produce) and end-to-end
writes (cross-platform since regular paths just work).

The merger's reproduction case — a >260-char absolute path — is tested
on every platform by constructing such a path under tmp_path and
verifying io_util.write_bytes_safe doesn't choke on it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from core import io_util


def test_to_long_path_noop_on_non_windows():
    """On Linux/macOS the helper just returns the original string."""
    with patch.object(sys, "platform", "linux"):
        result = io_util.to_long_path("/home/me/foo/bar.txt")
        assert result == "/home/me/foo/bar.txt"


def test_to_long_path_prefixes_drive_letter_path_on_windows():
    with patch.object(sys, "platform", "win32"):
        result = io_util.to_long_path(r"C:\Users\me\Documents\foo.txt")
        assert result == r"\\?\C:\Users\me\Documents\foo.txt"


def test_to_long_path_prefixes_unc_path_on_windows():
    with patch.object(sys, "platform", "win32"):
        result = io_util.to_long_path(r"\\server\share\folder\file.txt")
        assert result == r"\\?\UNC\server\share\folder\file.txt"


def test_to_long_path_skips_relative_paths():
    """\\\\?\\ only works with absolute paths; relative paths are returned
    unchanged. (Pathlib will error before reaching CopyFile2 anyway.)"""
    with patch.object(sys, "platform", "win32"):
        result = io_util.to_long_path(r"relative\path\file.txt")
        # Relative paths don't get the prefix
        assert not result.startswith("\\\\?\\")


def test_to_long_path_idempotent():
    """Applying the prefix to an already-prefixed path is a no-op."""
    with patch.object(sys, "platform", "win32"):
        prefixed = r"\\?\C:\already\prefixed\file.txt"
        assert io_util.to_long_path(prefixed) == prefixed


def test_to_long_path_normalizes_forward_slashes_on_windows():
    """The \\\\?\\ prefix only works with backslashes."""
    with patch.object(sys, "platform", "win32"):
        result = io_util.to_long_path("C:/Users/me/Documents/foo.txt")
        assert "/" not in result
        assert result == r"\\?\C:\Users\me\Documents\foo.txt"


def test_write_bytes_safe_round_trips_normal_path(tmp_path):
    """Cross-platform sanity: short paths work the same as Path.write_bytes."""
    target = tmp_path / "ordinary_name.bin"
    io_util.write_bytes_safe(target, b"some data")
    assert target.read_bytes() == b"some data"


def test_write_bytes_safe_handles_long_path_on_linux(tmp_path):
    """A path with >260 chars works fine on Linux (no MAX_PATH limit).
    This test exercises the helper's non-Windows branch."""
    # Build a >260-char absolute path under tmp_path.
    deep = tmp_path
    while len(str(deep)) < 260:
        deep = deep / "deep_subdirectory_with_some_length"
    deep.mkdir(parents=True, exist_ok=True)
    target = deep / "long_path_target.bin"
    assert len(str(target)) > 260
    io_util.write_bytes_safe(target, b"payload")
    assert target.read_bytes() == b"payload"


def test_write_text_safe_round_trips(tmp_path):
    target = tmp_path / "ordinary.txt"
    io_util.write_text_safe(target, "hello world\n")
    assert target.read_text() == "hello world\n"
