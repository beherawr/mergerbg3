"""Subprocess wrapper around LSLib's ``divine.exe``.

The merger doesn't reimplement Larian's binary formats: it shells out to
``divine.exe`` for every LSF↔LSX conversion. ``divine.exe`` is the CLI
front-end to LSLib (the de-facto BG3 tool library); the user supplies its
path once in app settings, and we cache it.

What we use divine for:
- Convert ``.lsf`` / ``.lsfx`` binary → ``.lsx`` text so we can parse/merge
- Convert merged ``.lsx`` → ``.lsf`` / ``.lsfx`` binary for output
- (Optional, later) Pack the merged project into a ``.pak`` for distribution
- (Optional, later) Convert ``.loca`` ↔ ``.loca.xml``

Divine's CLI surface:

    divine.exe -g bg3 -a convert-resource -i lsf -o lsx \\
               -s INPUT.lsf -d OUTPUT.lsx

    divine.exe -g bg3 -a convert-resource -i lsx -o lsf \\
               -s INPUT.lsx -d OUTPUT.lsf

    divine.exe -g bg3 -a convert-resources -i lsf -o lsx \\
               -s INPUT_DIR -d OUTPUT_DIR   (batch)

This module:
- never crashes if divine.exe is absent: it raises a typed exception that
  the GUI translates into "please point at divine.exe in Settings"
- locates the executable from an explicit path passed at construction time
- runs each conversion in a temp file when needed
- handles stdout/stderr capture and surfaces divine's own error messages
  (LSLib has good error reporting; we want it to reach the user verbatim)

Testing note: this module CANNOT be exercised on Linux without divine.exe
being available. Tests run the executable detection paths and verify the
error handling; integration tests against actual conversion run only on
Windows (or under Wine).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class DivineNotFoundError(FileNotFoundError):
    """Raised when divine.exe cannot be located.

    The GUI catches this and routes the user to the Settings screen to
    pick the executable. Carries the attempted paths so the user can see
    where we looked.
    """
    def __init__(self, tried_paths: list[Path]):
        self.tried_paths = tried_paths
        attempted = "\n".join(f"  - {p}" for p in tried_paths)
        super().__init__(
            "divine.exe not found. Searched:\n"
            f"{attempted}\n"
            "Set the path explicitly in app Settings, or place divine.exe on PATH."
        )


class DivineError(RuntimeError):
    """Raised when divine.exe runs but reports an error.

    ``stderr`` carries the original divine error message: we pass it
    through verbatim because LSLib's messages are usually actionable.
    """
    def __init__(self, command: list[str], returncode: int,
                 stdout: str, stderr: str):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        summary = stderr.strip() or stdout.strip() or "(no output)"
        super().__init__(
            f"divine.exe failed (exit {returncode}): {summary}\n"
            f"Command was: {' '.join(command)}"
        )


class DotNetMissingError(RuntimeError):
    """Raised when divine.exe cannot start because .NET 8 isn't installed.

    LSLib v1.20+ requires the .NET 8 Desktop Runtime. When it's missing,
    Windows reports the failure in several different ways depending on
    OS version: stderr contains "framework" / "runtime not found" text,
    OR subprocess raises OSError WinError 1114/216, OR divine exits
    with code 0x80008096 (-2147450746). We catch all three.

    The user-facing fix is always the same: install .NET 8 Desktop
    Runtime from Microsoft. We surface a direct download link.
    """

    DOWNLOAD_URL = (
        "https://dotnet.microsoft.com/en-us/download/dotnet/8.0/"
        "runtime"
    )

    def __init__(self, detail: str = ""):
        msg = (
            ".NET 8 Desktop Runtime isn't installed, so divine.exe "
            "(the bundled LSLib tool used for binary LSF conversion) "
            "couldn't start.\n\n"
            "Install it from Microsoft (free, ~55MB):\n"
            f"   {self.DOWNLOAD_URL}\n\n"
            "Pick 'Windows x64 Desktop Runtime 8.x.x', install, then "
            "retry the merge or icon-add."
        )
        if detail:
            msg += f"\n\n(Diagnostic: {detail})"
        super().__init__(msg)


# Patterns that indicate .NET runtime is missing. Different Windows
# versions and divine builds report this differently; we look for any
# of these in subprocess output to be robust to LSLib version updates.
# Keep these short and lowercase: we match against the full output
# also lowercased, so case mismatches don't matter.
_DOTNET_MISSING_PATTERNS = (
    "you must install or update .net",
    "framework was not found",
    "framework version",  # "no compatible framework version"
    ".net runtime was not found",
    "no .net runtimes were found",
    "to install a missing version of .net",
    "microsoft.netcore.app",
    "microsoft.windowsdesktop.app",
    "framework: 'microsoft",  # variant wording on some Windows builds
)


def _looks_like_dotnet_missing(stdout: str, stderr: str, returncode: int) -> bool:
    """Heuristic: did divine fail to start because .NET 8 is missing?"""
    haystack = (stdout + "\n" + stderr).lower()
    if any(p in haystack for p in _DOTNET_MISSING_PATTERNS):
        return True
    # Windows error 0x80008096 (-2147450746): coreclr couldn't load.
    if returncode in (-2147450746, 2147516950):
        return True
    return False


# --- Locating divine.exe ---------------------------------------------------


# Common install locations to probe when the user hasn't given us an
# explicit path. We include the LSLib release layout and the Modders
# Multitool bundled copy. Not exhaustive: we never *require* a hit here,
# just try to be helpful out of the box.
DEFAULT_SEARCH_PATHS: list[str] = [
    r"C:\Program Files\LSLib\divine.exe",
    r"C:\Program Files (x86)\LSLib\divine.exe",
    r"C:\Tools\LSLib\divine.exe",
    r"C:\BG3MM\LSLib\divine.exe",
    r"C:\BG3 Modding\LSLib\divine.exe",
]


def _bundled_divine_path() -> Path | None:
    """Return the path to the LSLib copy bundled with this app, if any.

    The app ships LSLib next to the entrypoint under ``tools/lslib/``.
    Two layouts to support:

      - PyInstaller one-file mode: the executable extracts bundled data
        to a temp dir at startup and exposes the path via
        ``sys._MEIPASS``. Our LSLib lives at ``<_MEIPASS>/tools/lslib/``.
      - PyInstaller one-folder mode (or running from source): the app
        runs from a directory that contains both the entrypoint and a
        sibling ``tools/lslib/`` folder.

    Returns the Path if the bundled divine.exe exists and is a file;
    None otherwise. Never raises - callers fall back to PATH/heuristics
    when nothing's bundled.
    """
    import sys
    candidates: list[Path] = []
    # One-file PyInstaller mode: sys._MEIPASS points at the unpacked
    # temp dir. The attribute only exists when frozen, so guard with
    # hasattr to keep this code import-clean when running from source.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "tools" / "lslib" / "divine.exe")
    # One-folder mode (or running from a checkout): walk up from the
    # entrypoint until we find the marker. Most installs have the layout
    # <install>/bg3_mod_merger.exe + <install>/tools/lslib/divine.exe,
    # so the parent of sys.argv[0] is the right place to look first.
    try:
        entry = Path(sys.argv[0]).resolve().parent
        candidates.append(entry / "tools" / "lslib" / "divine.exe")
        # Also check the repo-root layout for developers running from
        # source: <repo>/vendor/lslib/divine.exe.
        candidates.append(entry / "vendor" / "lslib" / "divine.exe")
        # And one parent up, since the dev entrypoint might be
        # <repo>/gui/__main__.py, making argv[0] live in <repo>/gui/.
        candidates.append(entry.parent / "vendor" / "lslib" / "divine.exe")
    except (OSError, ValueError):
        # Pathological argv[0] (empty, weird shell, ...) - just skip.
        pass
    for c in candidates:
        if c.is_file():
            return c
    return None


def find_divine(explicit_path: Path | str | None = None) -> Path:
    """Locate divine.exe, raising DivineNotFoundError if not found.

    Resolution order:
    1. ``explicit_path`` if given (must exist; raised if it doesn't).
       An explicit path always wins so a power user can point at their
       own newer LSLib build and override the bundled copy.
    2. Bundled LSLib next to the app (``<install>/tools/lslib/divine.exe``).
       This is the common case: the user did nothing and our shipped
       copy just works.
    3. ``shutil.which("divine.exe")``: on PATH
    4. ``shutil.which("divine")``: for Linux/Wine setups
    5. Each entry in DEFAULT_SEARCH_PATHS that happens to exist

    The first hit wins. We don't validate that the executable actually
    is divine: caller code will discover that on first use.

    ``explicit_path`` is normalized before checking: surrounding
    whitespace and surrounding quotes are stripped. Windows users who
    use File Explorer's "Copy as path" get paths wrapped in double
    quotes (e.g. ``"C:\\Tools\\divine.exe"``), and pasted paths often
    end up with stray whitespace. Both used to silently fail here.
    """
    tried: list[Path] = []

    if explicit_path is not None:
        raw = str(explicit_path).strip()
        # Strip a single pair of surrounding quotes (Windows
        # "Copy as path" produces these). Don't touch internal quotes.
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
            raw = raw[1:-1].strip()
        # An empty string after normalizing means "not actually configured"  - 
        # fall through to bundled/PATH lookup rather than raising on
        # the empty path.
        if raw:
            p = Path(raw)
            tried.append(p)
            if p.is_file():
                return p
            raise DivineNotFoundError(tried)

    # Bundled copy: no user config required.
    bundled = _bundled_divine_path()
    if bundled is not None:
        return bundled

    for name in ("divine.exe", "divine"):
        found = shutil.which(name)
        if found:
            return Path(found)
        tried.append(Path(name))

    for candidate in DEFAULT_SEARCH_PATHS:
        p = Path(candidate)
        tried.append(p)
        if p.is_file():
            return p

    raise DivineNotFoundError(tried)


# --- Conversion wrapper ----------------------------------------------------


@dataclass
class Divine:
    """Bound divine.exe wrapper. Construct once, call methods many times.

    Holds the path to divine.exe and the target game. ``game="bg3"`` is the
    only setting we use; LSLib supports older Divinity titles too but
    they're irrelevant here.

    The wrapper is intentionally thin: it doesn't try to be a high-level
    binary-LSF-merge engine. The pipeline is always:

        binary LSF on disk → lsf_to_lsx(...) → LSX text → core.lsx.parse_*
        ... merge happens in memory in our model ...
        core.lsx.serialize → LSX text → lsx_to_lsf(...) → binary LSF on disk
    """
    exe_path: Path
    game: str = "bg3"

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        cmd = [str(self.exe_path), "-g", self.game, *args]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,  # we handle the return code below
            )
        except OSError as e:
            # WinError 1114 ("DLL initialization failed") and 216 ("not
            # compatible") are the typical signatures of "tried to run
            # a .NET app without the .NET runtime". Surface the
            # actionable error rather than the cryptic OS code.
            winerror = getattr(e, "winerror", None)
            if winerror in (216, 1114) or "0x800700d8" in str(e).lower():
                raise DotNetMissingError(
                    detail=f"OSError winerror={winerror}: {e}"
                ) from e
            raise
        if result.returncode != 0:
            if _looks_like_dotnet_missing(
                result.stdout, result.stderr, result.returncode
            ):
                raise DotNetMissingError(
                    detail=(result.stderr or result.stdout).strip()[:300]
                )
            raise DivineError(
                command=cmd,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return result

    def lsf_to_lsx(self, src: Path | str, dst: Path | str) -> None:
        """Convert a binary LSF (or LSFX) to LSX text on disk.

        Both .lsf and .lsfx use the same LSOF container, so divine handles
        them identically: the extension on output is whatever we ask for.
        """
        self._run([
            "-a", "convert-resource",
            "-i", "lsf",
            "-o", "lsx",
            "-s", str(src),
            "-d", str(dst),
        ])

    def lsx_to_lsf(self, src: Path | str, dst: Path | str) -> None:
        """Convert LSX text to binary LSF on disk."""
        self._run([
            "-a", "convert-resource",
            "-i", "lsx",
            "-o", "lsf",
            "-s", str(src),
            "-d", str(dst),
        ])

    def loca_to_xml(self, src: Path | str, dst: Path | str) -> None:
        """Convert a packed .loca binary to .loca.xml.

        Used when ingesting an already-packed mod that we want to merge
        with a Toolkit project. The two example projects we have today
        don't need this (their localization is already in .xml form), but
        the merger should support it eventually.
        """
        self._run([
            "-a", "convert-loca",
            "-i", "loca",
            "-o", "xml",
            "-s", str(src),
            "-d", str(dst),
        ])

    def xml_to_loca(self, src: Path | str, dst: Path | str) -> None:
        """Convert .loca.xml back to packed .loca binary."""
        self._run([
            "-a", "convert-loca",
            "-i", "xml",
            "-o", "loca",
            "-s", str(src),
            "-d", str(dst),
        ])

    def list_pak(self, pak_path: Path | str) -> str:
        """List a .pak's contents and return divine's stdout verbatim."""
        result = self._run([
            "-a", "list-package",
            "-s", str(pak_path),
        ])
        return result.stdout
