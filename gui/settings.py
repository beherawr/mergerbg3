"""Persistent app settings (the equivalent of QSettings, but plain JSON).

Stored under the platform-appropriate user data dir:
- Windows: ``%APPDATA%\\bg3_mod_merger\\settings.json``
- Linux:   ``~/.config/bg3_mod_merger/settings.json``
- macOS:   ``~/Library/Application Support/bg3_mod_merger/settings.json``

Why JSON instead of QSettings? Keeps the engine import-clean — no Qt
dependency creeps into ``core/``, and the settings file is trivially
inspectable / hand-editable when something goes wrong.

We store *paths the user actually browsed to*, not auto-discovered ones.
That way an explicit choice in Settings wins over any heuristic the next
session might dream up.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _user_data_dir() -> Path:
    """Return the platform-appropriate per-user config directory."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "bg3_mod_merger"
        return Path.home() / "AppData" / "Roaming" / "bg3_mod_merger"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "bg3_mod_merger"
    # Linux / Unix
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "bg3_mod_merger"
    return Path.home() / ".config" / "bg3_mod_merger"


SETTINGS_PATH = _user_data_dir() / "settings.json"


@dataclass
class Settings:
    """All app-level preferences. Anything the user might tweak goes here.

    Adding a new field: append to this dataclass with a default; existing
    JSON files missing the field will still load (we merge with the
    default-constructed instance).
    """
    # Path to divine.exe for LSF/LOCA conversion. Empty = not configured;
    # the merger doesn't currently need it for clean-union merges, so we
    # don't force the user to set this before a first run.
    divine_path: str = ""

    # Path to the user's workspace directory — typically the BG3 Toolkit
    # ``/data`` folder, which contains all their project subdirectories.
    # Empty means "not yet configured" and the wizard's first page will
    # ask for it. Once set, the picker scans here for projects to merge.
    workspace_dir: str = ""

    # Directory the user last picked an input project FROM. Used as the
    # default browse directory so they don't have to navigate from / every
    # time. We store one slot per role (input A, input B, output) so each
    # gets a useful default.
    last_input_dir: str = ""
    last_output_dir: str = ""

    # Default conflict policy when starting a new merge. Values match
    # ``merger.ConflictPolicy``: "skip", "prefix", "fail".
    default_conflict_policy: str = "skip"

    # Prefix string used with policy="prefix". Stored so users who
    # consistently use the same naming convention don't have to retype.
    default_conflict_prefix: str = "Merged_"

    # Recent project paths, most recent first. Capped at 10.
    recent_projects: list[str] = field(default_factory=list)

    # Window geometry — last position + size. Empty = use Qt defaults.
    window_geometry: str = ""


def load() -> Settings:
    """Read settings from disk. Returns defaults if the file is missing
    or unreadable — never raises."""
    if not SETTINGS_PATH.is_file():
        return Settings()
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return Settings()
        # Merge with defaults so missing keys are filled in.
        defaults = asdict(Settings())
        defaults.update({k: v for k, v in raw.items() if k in defaults})
        return Settings(**defaults)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        # Corrupt file — fall back to defaults rather than blocking startup.
        return Settings()


def save(settings: Settings) -> None:
    """Write settings to disk. Creates the directory if needed.
    Best-effort; failures are silent (the GUI will continue working with
    in-memory state)."""
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(asdict(settings), indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def add_recent_project(settings: Settings, path: str | Path) -> None:
    """Push a project path to the front of the recents list, deduplicated,
    capped at 10. Mutates settings in place."""
    s = str(Path(path).resolve())
    settings.recent_projects = [s] + [p for p in settings.recent_projects if p != s]
    settings.recent_projects = settings.recent_projects[:10]
