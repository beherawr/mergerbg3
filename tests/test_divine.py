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
    """We deliberately call .lsfx through lsf_to_lsx: both formats share
    the LSOF container, divine treats them identically when given i=lsf."""
    # This test is documentation-as-code; the wrapper has no separate
    # method, and that's intentional.
    assert hasattr(divine.Divine, "lsf_to_lsx")
    assert not hasattr(divine.Divine, "lsfx_to_lsx")  # would be misleading


# ---------------------------------------------------------------------------
# find_divine path-normalization (issue: users got "divine not configured"
# fallback warnings even though Settings had a valid path, because the
# stored string had stray whitespace or surrounding quotes from Windows
# "Copy as path".)
# ---------------------------------------------------------------------------


def test_find_divine_strips_surrounding_quotes(tmp_path):
    """Windows 'Copy as path' wraps paths in double quotes. The raw
    string `"C:\\Tools\\divine.exe"` (with literal quotes) used to fail
    silently because Path() treats the quotes as part of the filename."""
    fake = tmp_path / "divine.exe"
    fake.touch()
    quoted = f'"{fake}"'
    result = divine.find_divine(quoted)
    assert result == fake


def test_find_divine_strips_surrounding_single_quotes(tmp_path):
    fake = tmp_path / "divine.exe"
    fake.touch()
    result = divine.find_divine(f"'{fake}'")
    assert result == fake


def test_find_divine_strips_surrounding_whitespace(tmp_path):
    """Pasted paths often have stray whitespace at the end."""
    fake = tmp_path / "divine.exe"
    fake.touch()
    result = divine.find_divine(f"  {fake}  ")
    assert result == fake


def test_find_divine_empty_string_falls_through_to_path_lookup(tmp_path, monkeypatch):
    """An empty/whitespace-only string isn't a real path; it should fall
    through to PATH lookup rather than raising an error about a missing '.'
    file."""
    monkeypatch.setattr("core.divine.shutil.which", lambda name: None)
    monkeypatch.setattr("core.divine.DEFAULT_SEARCH_PATHS", [])
    with pytest.raises(divine.DivineNotFoundError):
        divine.find_divine("")
    with pytest.raises(divine.DivineNotFoundError):
        divine.find_divine("   ")


def test_find_divine_quoted_path_to_nonexistent_file_still_fails(tmp_path):
    """Normalization shouldn't hide actual file-doesn't-exist errors."""
    missing = tmp_path / "does_not_exist.exe"
    with pytest.raises(divine.DivineNotFoundError):
        divine.find_divine(f'"{missing}"')


def test_find_divine_internal_quotes_preserved(tmp_path):
    """Only surrounding quotes are stripped. A path with internal quotes
    (unusual but valid on Unix) should be left alone."""
    # We can't easily make a real file with " in the name on all OSes,
    # so just verify a half-quoted string raises (rather than silently
    # stripping one side).
    with pytest.raises(divine.DivineNotFoundError):
        divine.find_divine('"missing.exe')  # one quote, not surrounding


# --- Bundled LSLib discovery ----------------------------------------------


def test_bundled_divine_returns_none_when_not_bundled(monkeypatch, tmp_path):
    """In dev mode with no bundled LSLib, _bundled_divine_path returns
    None and find_divine falls through to PATH/heuristics. We isolate
    by pointing sys.argv[0] at a clean tmp dir and clearing _MEIPASS."""
    import sys
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "fake_entry.py")])
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert divine._bundled_divine_path() is None


def test_bundled_divine_finds_meipass_layout(monkeypatch, tmp_path):
    """PyInstaller one-file mode: _MEIPASS is set and divine.exe lives
    at <_MEIPASS>/tools/lslib/divine.exe."""
    import sys
    bundled = tmp_path / "tools" / "lslib" / "divine.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert divine._bundled_divine_path() == bundled


def test_bundled_divine_finds_one_folder_layout(monkeypatch, tmp_path):
    """PyInstaller one-folder mode (or installed exe): divine sits next
    to the entrypoint under tools/lslib/."""
    import sys
    entry_dir = tmp_path / "app"
    entry_dir.mkdir()
    bundled = entry_dir / "tools" / "lslib" / "divine.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"")
    monkeypatch.setattr(sys, "argv", [str(entry_dir / "bg3_mod_merger.exe")])
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert divine._bundled_divine_path() == bundled


def test_find_divine_prefers_bundled_when_no_explicit_path(monkeypatch, tmp_path):
    """With no explicit path and a bundled copy available, find_divine
    returns the bundled copy — even if divine.exe is on PATH. The user
    shouldn't get a stale PATH version when we shipped a known-good one."""
    import sys
    bundled = tmp_path / "tools" / "lslib" / "divine.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    # Put a different "divine.exe" on PATH that we should NOT pick.
    other = tmp_path / "PATH_dir" / "divine.exe"
    other.parent.mkdir()
    other.write_bytes(b"")
    monkeypatch.setenv("PATH", str(other.parent))

    found = divine.find_divine()
    assert found == bundled


def test_find_divine_explicit_path_overrides_bundled(monkeypatch, tmp_path):
    """Power-user override: when the user gives an explicit path, we use
    theirs even if we have a bundled copy. They may have a newer LSLib
    build they want to test against."""
    import sys
    bundled = tmp_path / "tools" / "lslib" / "divine.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"BUNDLED")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    user_choice = tmp_path / "my_divine" / "divine.exe"
    user_choice.parent.mkdir()
    user_choice.write_bytes(b"USERS")

    found = divine.find_divine(user_choice)
    assert found == user_choice


# --- .NET 8 missing detection ------------------------------------------------


def test_dotnet_missing_detected_in_stderr():
    """Common .NET-missing error strings should be recognized."""
    msg = (
        "It was not possible to find any compatible framework version\n"
        "The specified framework 'Microsoft.WindowsDesktop.App', "
        "version '8.0.0' was not found."
    )
    assert divine._looks_like_dotnet_missing("", msg, 0x80008096 - 0x100000000)


def test_dotnet_missing_detected_via_returncode():
    """Even with empty output, the specific coreclr failure code
    is a signal that .NET isn't installed."""
    assert divine._looks_like_dotnet_missing("", "", -2147450746)


def test_dotnet_missing_not_falsely_triggered_by_other_errors():
    """A real LSLib parsing error mustn't be misclassified as 'install .NET'."""
    msg = "Resource type LSF v1 is not supported"
    assert not divine._looks_like_dotnet_missing("", msg, 1)


def test_dotnet_missing_error_has_actionable_message():
    """The error message must include a download URL and clear next steps."""
    err = divine.DotNetMissingError("test detail")
    text = str(err)
    assert "8" in text  # mentions .NET 8
    assert ".microsoft.com" in text  # links to Microsoft's download page
    assert "test detail" in text  # preserves diagnostic info
