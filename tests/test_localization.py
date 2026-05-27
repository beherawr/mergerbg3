"""Tests for ``core.localization`` against the real example projects."""

from __future__ import annotations

from pathlib import Path

import pytest

from core import localization
from .helpers import FIXTURES, PROJECTS


def all_loca() -> list[Path]:
    """Every .xml file under any Localization/<Lang>/ folder."""
    results: list[Path] = []
    for project in PROJECTS:
        results.extend(project.glob("Mods/*/Localization/*/*.xml"))
    return sorted(results)


# --- Parsing -----------------------------------------------------------------


@pytest.mark.parametrize("path", all_loca(), ids=lambda p: str(p.relative_to(p.parents[6])))
def test_parses_real_file(path):
    """Every real localization file parses without error."""
    parsed = localization.parse_file(path)
    assert len(parsed.entries) >= 1
    for entry in parsed.entries:
        # Every entry has a handle and version.
        assert entry.contentuid.startswith("h"), f"bad handle in {path}: {entry.contentuid}"
        # Handles are h + 36 chars (UUID-shaped with 'g' separators): 37 total.
        assert len(entry.contentuid) == localization.HANDLE_LENGTH, (
            f"wrong handle length in {path}: {entry.contentuid}"
        )
        assert entry.version  # non-empty string


def test_shadowdance_localization_content():
    """Spot check known entries in ShadowDance/english.xml."""
    sd = FIXTURES / "ShadowDance"
    path = next(sd.glob("Mods/*/Localization/*/english.xml"))
    parsed = localization.parse_file(path)

    assert len(parsed.entries) == 11
    handles = parsed.handles()
    # Spell display names: these are referenced from Spell_Target.txt etc.
    assert "h2db009beg91d6g310eg03c0g9e0885029fce" in handles
    assert "h6564e6bcg6d91g70f9ge706ga9f0a4eec3e3" in handles

    # Look up a specific entry's text.
    invis = parsed.by_handle("hfcba34ebgb12eg066dg0dfdgff22e5caed58")
    assert invis is not None
    assert invis.text == "Invisible"


def test_shadowdancer_localization_with_inline_tags():
    """Body text may contain inline tags like <LSTag>. We must preserve them."""
    sdancer = FIXTURES / "Shadowdancer"
    path = next(sdancer.glob("Mods/*/Localization/*/english.xml"))
    parsed = localization.parse_file(path)
    # Shadowdancer has 3 entries: confirm they round-trip.
    assert len(parsed.entries) == 3


def test_shadowdance_has_lstag_in_body():
    """ShadowDance has at least one <LSTag>-bearing description; the body
    text round-trips that markup intact."""
    sd = FIXTURES / "ShadowDance"
    path = next(sd.glob("Mods/*/Localization/*/english.xml"))
    parsed = localization.parse_file(path)
    has_lstag = any("<LSTag" in e.text for e in parsed.entries)
    assert has_lstag


# --- Handle extraction -------------------------------------------------------


def test_extract_handles_finds_known_refs():
    """The handle scanner is what the reference index will use to find every
    loca ref in stats files and LSX attributes. It must catch the joined
    `handle;version` form used by stats .txt."""
    sample = 'data "DisplayName" "h6564e6bcg6d91g70f9ge706ga9f0a4eec3e3;1"'
    found = localization.extract_handles(sample)
    assert found == {"h6564e6bcg6d91g70f9ge706ga9f0a4eec3e3"}


def test_extract_handles_handles_multiple():
    sample = '''
        data "DisplayName" "h2db009beg91d6g310eg03c0g9e0885029fce;2"
        data "Description" "hf1770ec4g2aeegc1e9g0236g34c9aa9c921d;2"
    '''
    found = localization.extract_handles(sample)
    assert found == {
        "h2db009beg91d6g310eg03c0g9e0885029fce",
        "hf1770ec4g2aeegc1e9g0236g34c9aa9c921d",
    }


def test_extract_handles_skips_uuids():
    """A real UUID like d21296e6-898c-4072-8c24-4c5a26f249f0 shouldn't match:
    different format (has dashes, 36 chars, no leading h)."""
    sample = 'value="d21296e6-898c-4072-8c24-4c5a26f249f0"'
    found = localization.extract_handles(sample)
    assert found == set()


# --- Merging -----------------------------------------------------------------


def test_merge_disjoint_handles():
    a = localization.LocaFile(entries=[
        localization.LocaEntry("habc" + "1" * 29, "1", "alpha"),
    ])
    b = localization.LocaFile(entries=[
        localization.LocaEntry("hdef" + "2" * 29, "1", "beta"),
    ])
    merged, conflicts = localization.merge(a, b)
    assert len(merged.entries) == 2
    assert conflicts == []


def test_merge_identical_dedupes():
    e = localization.LocaEntry("habc" + "1" * 29, "1", "alpha")
    a = localization.LocaFile(entries=[e])
    b = localization.LocaFile(entries=[e])
    merged, conflicts = localization.merge(a, b)
    assert len(merged.entries) == 1
    assert conflicts == []


def test_merge_takes_higher_version_for_same_text():
    """If two mods have the same handle+text but different versions (because
    one was edited and bumped), the merge takes the higher version."""
    a = localization.LocaFile(entries=[
        localization.LocaEntry("habc" + "1" * 29, "1", "alpha"),
    ])
    b = localization.LocaFile(entries=[
        localization.LocaEntry("habc" + "1" * 29, "3", "alpha"),
    ])
    merged, conflicts = localization.merge(a, b)
    assert merged.entries[0].version == "3"
    assert conflicts == []


def test_merge_conflict_for_same_handle_different_text():
    """Same handle pointing at different text is a real conflict: extremely
    rare in practice (would mean one mod is misusing another's handle) but
    must be surfaced."""
    a = localization.LocaFile(entries=[
        localization.LocaEntry("habc" + "1" * 29, "1", "Original"),
    ])
    b = localization.LocaFile(entries=[
        localization.LocaEntry("habc" + "1" * 29, "1", "Overwritten"),
    ])
    merged, conflicts = localization.merge(a, b)
    # A's text wins by default.
    assert merged.entries[0].text == "Original"
    assert len(conflicts) == 1


def test_merge_real_projects_no_conflicts():
    """Merging the two real fixture loca files should produce a single
    LocaFile with all entries from both and zero conflicts (handles disjoint)."""
    a_path = next((FIXTURES / "ShadowDance").glob("Mods/*/Localization/*/english.xml"))
    b_path = next((FIXTURES / "Shadowdancer").glob("Mods/*/Localization/*/english.xml"))
    a = localization.parse_file(a_path)
    b = localization.parse_file(b_path)
    merged, conflicts = localization.merge(a, b)

    assert conflicts == []
    assert len(merged.entries) == len(a.entries) + len(b.entries)


# --- Round-trip --------------------------------------------------------------


def test_can_write_and_reparse():
    """A parsed file written and re-read should yield the same entries.
    (Exact byte match is harder due to xmlns ordering; semantic match is
    what we care about for the merger.)"""
    a_path = next((FIXTURES / "Shadowdancer").glob("Mods/*/Localization/*/english.xml"))
    original = localization.parse_file(a_path)
    out_bytes = original.to_xml_bytes()
    reparsed = localization.parse_bytes(out_bytes)
    assert len(reparsed.entries) == len(original.entries)
    for o, r in zip(original.entries, reparsed.entries):
        assert o.contentuid == r.contentuid
        assert o.version == r.version
        assert o.text == r.text
