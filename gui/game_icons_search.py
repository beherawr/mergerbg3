"""Loader and search for the bundled game-icons.net icon set.

We ship ~4180 1-bit 256x256 PNGs at ``gui/assets/game_icons/``
generated from the game-icons.net SVG repository (CC BY 3.0). Plus
a ``_index.json`` mapping every PNG to a human-readable name and
contributor name.

This module exposes:

  list_all()             - eager-load the full index
  search(query, limit)   - return up to ``limit`` index entries whose
                           name contains every word in the query
                           (case-insensitive, simple substring match)
  load_image(entry)      - open one entry's PNG as a PIL Image, ready
                           to feed into the forge stylizer

We deliberately don't bring in any heavy text-search library (whoosh,
lunr, etc.). The corpus is 4180 short strings, so linear scan
finishes in well under a millisecond - simpler and faster than
building an index in memory.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image


@dataclass(frozen=True)
class GameIcon:
    """One entry in the bundled set."""
    name: str       # human-readable, e.g. "Lightning Storm"
    filename: str   # PNG filename in the assets dir, e.g. "lorc__lightning-storm.png"
    author: str     # contributor folder name, e.g. "lorc"

    @property
    def path(self) -> Path:
        # Resolved relative to the assets directory at runtime, so
        # this works both from source and from inside a PyInstaller
        # bundle (where _MEIPASS holds the unpacked tree).
        base = _assets_dir()
        return base / self.filename if base else Path(self.filename)


# --- Asset path discovery --------------------------------------------------


def _assets_dir() -> Path | None:
    """Return the directory containing the bundled game-icons PNGs.

    Two layouts to support:
      - Running from source: ``<repo>/gui/assets/game_icons/``
      - PyInstaller one-file: ``<sys._MEIPASS>/gui/assets/game_icons/``

    Returns ``None`` if neither exists, so callers can degrade to
    "no game icons available" rather than crashing.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "gui" / "assets" / "game_icons")
    # Dev/source: this file is gui/game_icons_search.py, so the assets
    # dir is at ../gui/assets/game_icons from this module's perspective,
    # which simplifies to gui/assets/game_icons.
    candidates.append(
        Path(__file__).resolve().parent / "assets" / "game_icons"
    )
    for c in candidates:
        if c.is_dir():
            return c
    return None


# --- Index loading ---------------------------------------------------------


_INDEX_CACHE: Optional[list[GameIcon]] = None


def _load_index() -> list[GameIcon]:
    """Read the _index.json once, cache it in memory.

    The index is small (~250KB of JSON for 4180 entries) and is the
    same for every dialog instance. We cache so we don't re-read on
    every search.
    """
    global _INDEX_CACHE
    if _INDEX_CACHE is not None:
        return _INDEX_CACHE
    base = _assets_dir()
    if base is None:
        _INDEX_CACHE = []
        return _INDEX_CACHE
    idx_file = base / "_index.json"
    if not idx_file.is_file():
        _INDEX_CACHE = []
        return _INDEX_CACHE
    try:
        raw = json.loads(idx_file.read_text(encoding="utf-8"))
    except Exception:
        _INDEX_CACHE = []
        return _INDEX_CACHE
    _INDEX_CACHE = [
        GameIcon(name=e["n"], filename=e["f"], author=e["a"])
        for e in raw
    ]
    return _INDEX_CACHE


def list_all() -> list[GameIcon]:
    """Return every available game icon as a list. Useful for the
    dialog's default view (show some icons even before the user
    searches), and for tests."""
    return list(_load_index())


# --- Searching -------------------------------------------------------------


def search(query: str, limit: int = 80) -> list[GameIcon]:
    """Return up to ``limit`` icons whose name contains every word in
    the query (case-insensitive). Words are simple whitespace-split;
    no quoting, no boolean ops, just substring AND.

    Examples:
      ""              - empty query: returns the first ``limit`` icons
                        (gives the user something to look at on tab open)
      "fire"          - matches "Fire Bolt", "Fireball", "Wildfire"
      "fire bolt"     - matches "Fire Bolt", "Crossbow Bolt Fire"
      "skull"         - matches "Skull", "Skull Ring", "Skull Crack"
      "magicwand"     - won't match "Magic Wand" because there's no
                        space; substring is matched after tokenization
    """
    index = _load_index()
    q = query.strip().lower()
    if not q:
        return index[:limit]
    tokens = q.split()
    out: list[GameIcon] = []
    for entry in index:
        name_lower = entry.name.lower()
        if all(t in name_lower for t in tokens):
            out.append(entry)
            if len(out) >= limit:
                break
    return out


# --- Image loading ---------------------------------------------------------


def load_image(entry: GameIcon) -> Image.Image:
    """Open the PNG for an entry as a PIL Image. Returns L-mode
    grayscale because the bundled PNGs are 1-bit (we converted them
    to L-mode here so callers can stylize them uniformly without
    worrying about the 1-bit/L distinction).

    The bundled image is 256x256 white-on-black. The forge stylizer
    treats it as a luminance mask and applies the glow stack, so the
    1-bit edges get smoothed by Gaussian blurs into clean glow halos.
    """
    img = Image.open(entry.path).convert("L")
    return img
