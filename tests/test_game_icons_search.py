"""Tests for ``gui.game_icons_search``.

We exercise the bundled icon set: the index loads, search returns
sensible results, image loading produces a usable PIL Image. If the
bundled assets aren't present (e.g. on a checkout where the build
step hasn't been run), these tests skip with a clear reason.
"""
from __future__ import annotations

import pytest
from PIL import Image

from gui import game_icons_search


def _ensure_bundle_present():
    """Skip a test if the bundled assets aren't there. Avoids spurious
    CI failures on minimal checkouts."""
    all_icons = game_icons_search.list_all()
    if not all_icons:
        pytest.skip(
            "Bundled game-icons assets not present in this checkout. "
            "Run the bundle build step or use the full release."
        )
    return all_icons


def test_list_all_returns_thousands_of_icons():
    """Sanity check: the bundle is supposed to ship game-icons.net's
    full set, which is ~4180 icons. If the count is way off, something
    went wrong with the bundle generation."""
    icons = _ensure_bundle_present()
    assert len(icons) > 3000, \
        f"Expected ~4180 bundled icons, got {len(icons)}"


def test_each_entry_has_required_fields():
    """Each GameIcon dataclass needs name, filename, and author. The
    JSON index uses short keys (n/f/a) but the loader expands them."""
    icons = _ensure_bundle_present()
    sample = icons[:50]
    for entry in sample:
        assert entry.name, f"Empty name for {entry}"
        assert entry.filename.endswith(".png"), \
            f"Filename should be a PNG: {entry.filename}"
        assert entry.author, f"Empty author for {entry}"


def test_search_empty_query_returns_first_batch():
    """An empty query is the dialog's initial state - we show the
    first N icons so the user has something to scroll through."""
    _ensure_bundle_present()
    hits = game_icons_search.search("", limit=20)
    assert len(hits) == 20


def test_search_known_term_finds_matches():
    """Common BG3 spell-art terms should all return hits. If any of
    these return zero, the search heuristic or the index is broken."""
    _ensure_bundle_present()
    for query in ("skull", "lightning", "magic", "dragon", "fire", "shield"):
        hits = game_icons_search.search(query, limit=5)
        assert len(hits) > 0, f"Expected hits for {query!r}, got none"
        # Verify the result names actually contain the query word.
        for entry in hits:
            assert query.lower() in entry.name.lower(), \
                f"Search for {query!r} returned {entry.name!r} which doesn't contain it"


def test_search_is_case_insensitive():
    """Capitalization shouldn't matter."""
    _ensure_bundle_present()
    lower = game_icons_search.search("skull", limit=10)
    upper = game_icons_search.search("SKULL", limit=10)
    mixed = game_icons_search.search("Skull", limit=10)
    assert {h.filename for h in lower} == {h.filename for h in upper}
    assert {h.filename for h in lower} == {h.filename for h in mixed}


def test_search_garbage_returns_empty():
    """A query that won't match anything should return an empty list,
    not crash or return everything. We don't want a typo to swamp
    the user with irrelevant icons."""
    _ensure_bundle_present()
    hits = game_icons_search.search("xqzqzqz nonsense gibberish", limit=20)
    assert hits == []


def test_search_respects_limit():
    """The limit caps the result count even when more matches exist.
    Prevents the UI from being flooded with 500 cards on a common
    search term like "magic"."""
    _ensure_bundle_present()
    hits = game_icons_search.search("a", limit=5)
    assert len(hits) <= 5


def test_load_image_returns_pil_image():
    """Loading a bundled icon as a PIL image should give us a 256x256
    grayscale (L-mode) image ready to feed into the forge stylizer.
    """
    icons = _ensure_bundle_present()
    entry = icons[0]
    img = game_icons_search.load_image(entry)
    assert isinstance(img, Image.Image)
    # Bundled PNGs are 256x256 1-bit, loaded as L-mode by load_image.
    assert img.size == (256, 256)
    assert img.mode == "L"
    # Some pixels should be white (the icon foreground), some black
    # (the background). If it's all one value, the bundle generation
    # failed for this icon.
    a_min, a_max = img.getextrema()
    assert a_min < 100 and a_max > 155, \
        f"Icon {entry.name} has degenerate value range ({a_min}-{a_max})"
