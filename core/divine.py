"""Subprocess wrapper around LSLib's ``divine.exe``.

The merger doesn't reimplement Larian's binary formats — it shells out to
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
- never crashes if divine.exe is absent — it raises a typed exception that
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

    ``stderr`` carries the original divine error message — we pass it
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


# --- Locating divine.exe ---------------------------------------------------


# Common install locations to probe when the user hasn't given us an
# explicit path. We include the LSLib release layout and the Modders
# Multitool bundled copy. Not exhaustive — we never *require* a hit here,
# just try to be helpful out of the box.
DEFAULT_SEARCH_PATHS: list[str] = [
    r"C:\Program Files\LSLib\divine.exe",
    r"C:\Program Files (x86)\LSLib\divine.exe",
    r"C:\Tools\LSLib\divine.exe",
    r"C:\BG3MM\LSLib\divine.exe",
    r"C:\BG3 Modding\LSLib\divine.exe",
]


def find_divine(explicit_path: Path | str | None = None) -> Path:
    """Locate divine.exe, raising DivineNotFoundError if not found.

    Resolution order:
    1. ``explicit_path`` if given (must exist; raised if it doesn't)
    2. ``shutil.which("divine.exe")`` — on PATH
    3. ``shutil.which("divine")`` — for Linux/Wine setups
    4. Each entry in DEFAULT_SEARCH_PATHS that happens to exist

    The first hit wins. We don't validate that the executable actually
    is divine — caller code will discover that on first use.
    """
    tried: list[Path] = []

    if explicit_path is not None:
        p = Path(explicit_path)
        tried.append(p)
        if p.is_file():
            return p
        raise DivineNotFoundError(tried)

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

    The wrapper is intentionally thin — it doesn't try to be a high-level
    binary-LSF-merge engine. The pipeline is always:

        binary LSF on disk → lsf_to_lsx(...) → LSX text → core.lsx.parse_*
        ... merge happens in memory in our model ...
        core.lsx.serialize → LSX text → lsx_to_lsf(...) → binary LSF on disk
    """
    exe_path: Path
    game: str = "bg3"

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        cmd = [str(self.exe_path), "-g", self.game, *args]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,  # we handle the return code below
        )
        if result.returncode != 0:
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
        them identically — the extension on output is whatever we ask for.
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
