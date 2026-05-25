"""Tests for ``core.divine``.

Without divine.exe present (we're on Linux for CI), we can only test:
- The location-resolution logic
- The exception types
- That the wrapper builds the right command line shape

Actual conversion is integration-tested separately on Windows.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from core import divine


# --- find_divine -------------------------------------------------------------


def test_find_divine_explicit_path_exists(tmp_path):
    """When given an explicit existing path, find_divine returns it."""
    fake = tmp_path / "divine.exe"
    fake.write_bytes(b"")
    found = divine.find_divine(fake)
    assert found == fake


def test_find_divine_explicit_path_missing(tmp_path):
    """An explicit path that doesn't exist raises with the path in the error."""
    fake = tmp_path / "does-not-exist.exe"
    with pytest.raises(divine.DivineNotFoundError) as ei:
        divine.find_divine(fake)
    assert fake in ei.value.tried_paths


def test_find_divine_no_path_raises_helpful_error():
    """With no explicit path and no divine on PATH, raise with the list of
    places we tried so the user can see why we failed."""
    # We can't trust that divine isn't somehow on PATH in CI. Use a sentinel
    # exe name that won't collide; pass it explicitly.
    fake = Path("/definitely/does/not/exist/divine.exe")
    with pytest.raises(divine.DivineNotFoundError) as ei:
        divine.find_divine(fake)
    err = str(ei.value)
    assert "Searched:" in err
    assert "divine.exe" in err


# --- Divine class structural tests ------------------------------------------


def test_divine_wrapper_uses_bg3_game_arg(tmp_path, monkeypatch):
    """The wrapper always passes ``-g bg3``. We patch subprocess.run to
    capture the command line and verify."""
    fake_exe = tmp_path / "divine.exe"
    fake_exe.write_bytes(b"")

    captured: dict = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # Return a fake successful CompletedProcess.
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr("core.divine.subprocess.run", fake_run)

    d = divine.Divine(exe_path=fake_exe)
    d.lsf_to_lsx("input.lsf", "output.lsx")

    cmd = captured["cmd"]
    assert str(fake_exe) in cmd
    assert "-g" in cmd and "bg3" in cmd
    assert "-a" in cmd and "convert-resource" in cmd
    assert "-i" in cmd and "lsf" in cmd
    assert "-o" in cmd and "lsx" in cmd
    assert "-s" in cmd and "input.lsf" in cmd
    assert "-d" in cmd and "output.lsx" in cmd


def test_divine_raises_on_nonzero_returncode(tmp_path, monkeypatch):
    """A non-zero exit translates to DivineError carrying stderr."""
    fake_exe = tmp_path / "divine.exe"
    fake_exe.write_bytes(b"")

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = "LSLib: file not found: foo.lsf"
        return R()

    monkeypatch.setattr("core.divine.subprocess.run", fake_run)

    d = divine.Divine(exe_path=fake_exe)
    with pytest.raises(divine.DivineError) as ei:
        d.lsf_to_lsx("foo.lsf", "foo.lsx")

    err = ei.value
    assert err.returncode == 1
    assert "LSLib: file not found" in str(err)


def test_loca_conversions_use_correct_action(tmp_path, monkeypatch):
    """convert-loca, not convert-resource, for .loca files."""
    fake_exe = tmp_path / "divine.exe"
    fake_exe.write_bytes(b"")

    captured: dict = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr("core.divine.subprocess.run", fake_run)
    d = divine.Divine(exe_path=fake_exe)

    d.loca_to_xml("a.loca", "a.xml")
    assert "convert-loca" in captured["cmd"]
    assert "loca" in captured["cmd"] and "xml" in captured["cmd"]

    d.xml_to_loca("a.xml", "a.loca")
    assert "convert-loca" in captured["cmd"]


def test_lsfx_uses_same_path_as_lsf():
    """We deliberately call .lsfx through lsf_to_lsx — both formats share
    the LSOF container, divine treats them identically when given i=lsf."""
    # This test is documentation-as-code; the wrapper has no separate
    # method, and that's intentional.
    assert hasattr(divine.Divine, "lsf_to_lsx")
    assert not hasattr(divine.Divine, "lsfx_to_lsx")  # would be misleading
