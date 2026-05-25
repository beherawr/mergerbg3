"""Windows long-path I/O helpers.

The Windows file APIs default to a 260-character path limit (``MAX_PATH``).
Real BG3 mod trees can blow through that quickly:

    F:\\SteamLibrary\\steamapps\\common\\Baldurs Gate 3\\Data\\Editor\\Mods\\
        ModName_<36-char-uuid>\\Public\\SharedDev\\Assets\\Weapons\\
        Humans\\WPN_HUM_Greatsword_Giantslayer_A\\Resources\\
        WPN_HUM_Greatsword_Giantslayer_A.GR2

That's >260 characters before we've even added the merger's staging
suffix. ``CopyFile2`` (used by ``shutil.copy2``) and the lower-level
``CreateFileW`` calls return ``ERROR_PATH_NOT_FOUND (3)`` past the limit
unless the path is given the ``\\\\?\\`` prefix, which tells Win32 to
skip path parsing and accept the full string verbatim.

Other platforms don't have this issue — these helpers are no-ops on
Linux/macOS.
"""

from __future__ import annotations

import sys
from pathlib import Path


def to_long_path(p: Path | str) -> str:
    """Return a path string usable with Windows file APIs beyond MAX_PATH.

    On Windows, prepends ``\\\\?\\`` to absolute paths (and ``\\\\?\\UNC\\``
    to UNC paths). On other platforms returns the plain string.

    The ``\\\\?\\`` prefix requires backslash separators and an absolute
    path; this helper takes care of both.

    The result is a string, not a ``Path`` — don't use it with Path
    operations (``.parent``, ``.exists()``, etc.); only feed it to the
    lower-level I/O APIs that need to bypass MAX_PATH.
    """
    s = str(p)
    if sys.platform != "win32":
        return s
    if s.startswith("\\\\?\\"):
        return s
    # Normalize to backslashes so the absolute-path check below catches
    # both ``C:/...`` and ``C:\\...`` consistently. (Path.is_absolute()
    # is OS-dependent — on a non-Windows test host it returns False for
    # ``C:\\foo``, but our code path is the platform=='win32' branch.)
    s = s.replace("/", "\\")
    # Windows absolute path = either a drive-letter path ``X:\foo`` or
    # a UNC path ``\\server\share\foo``. Anything else (bare relative
    # path) we leave alone — the prefix doesn't apply.
    is_unc = s.startswith("\\\\")
    is_drive_abs = (
        len(s) >= 3 and s[1] == ":" and s[2] == "\\"
        and s[0].isalpha()
    )
    if not (is_unc or is_drive_abs):
        return s
    if is_unc:
        # UNC: \\server\share\... → \\?\UNC\server\share\...
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def write_bytes_safe(path: Path | str, data: bytes) -> None:
    """``Path.write_bytes`` that survives long Windows paths."""
    p = Path(path)
    if sys.platform == "win32" and len(str(p)) > 240:
        with open(to_long_path(p), "wb") as f:
            f.write(data)
    else:
        p.write_bytes(data)


def write_text_safe(
    path: Path | str, text: str, encoding: str = "utf-8",
) -> None:
    """``Path.write_text`` that survives long Windows paths."""
    p = Path(path)
    if sys.platform == "win32" and len(str(p)) > 240:
        with open(to_long_path(p), "w", encoding=encoding, newline="") as f:
            f.write(text)
    else:
        p.write_text(text, encoding=encoding)
