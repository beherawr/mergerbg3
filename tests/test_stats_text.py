"""Tests for ``core.stats_text`` against the real example projects."""

from __future__ import annotations

import pytest

from core import stats_text
from .helpers import all_stats_txt


# --- Parsing -----------------------------------------------------------------


@pytest.mark.parametrize("path", all_stats_txt(), ids=lambda p: str(p.relative_to(p.parents[6])))
def test_parses_real_file(path):
    """Every real .txt file in the fixtures parses without error and yields
    at least one entry."""
    stats = stats_text.parse_file(path)
    assert len(stats.entries) >= 1
    for entry in stats.entries:
        # The line-based format always gives every entry a name and type.
        assert entry.name, f"empty name in {path}"
        assert entry.type, f"empty type in entry {entry.name!r} in {path}"


@pytest.mark.parametrize("path", all_stats_txt(), ids=lambda p: str(p.relative_to(p.parents[6])))
def test_roundtrip_real_file(path):
    """Parse → serialize round-trip should produce the same bytes as the source.

    This is the strongest correctness check we can run without a manual diff.
    If a byte differs, we either changed semantically meaningful content, or
    our formatter doesn't match Larian's. Both are bugs to fix.
    """
    original = path.read_bytes()
    # Strip BOM for comparison since the parser handles it (but
    # serialize() does not re-emit one; Larian's .txt files don't carry BOMs).
    if original.startswith(b"\xef\xbb\xbf"):
        original = original[3:]
    parsed = stats_text.parse_file(path)
    rewritten = stats_text.serialize(parsed).encode("utf-8")
    assert rewritten == original, (
        f"round-trip mismatch in {path}\n"
        f"--- original ({len(original)} bytes) ---\n{original[:200]!r}\n"
        f"--- rewritten ({len(rewritten)} bytes) ---\n{rewritten[:200]!r}\n"
    )


# --- Content spot-checks -----------------------------------------------------


def test_shadow_dance_spell_target_content():
    """Spot-check that we correctly parse a known entry's fields.

    Catches regressions where a field gets dropped or reordered.
    """
    path = next(p for p in all_stats_txt() if p.name == "Spell_Target.txt")
    stats = stats_text.parse_file(path)
    entry = stats.by_name("Target_BackstabK")
    assert entry is not None
    assert entry.type == "SpellData"
    assert entry.using is None  # this entry has no inheritance
    assert entry.data_value("SpellType") == "Target"
    assert entry.data_value("TargetRadius") == "18"
    # TranslatedString encoding stays joined in the data value.
    assert entry.data_value("DisplayName") == "h2db009beg91d6g310eg03c0g9e0885029fce;2"


def test_shadow_dance_status_boost_has_two_entries():
    """Status_BOOST.txt should yield two entries (BlockReactK + SeeInDaDarkK)."""
    path = next(p for p in all_stats_txt() if p.name == "Status_BOOST.txt")
    stats = stats_text.parse_file(path)
    assert stats.names() == ["BlockReactK", "SeeInDaDarkK"]


def test_shadowdancer_weapon_uses_dagger():
    """Weapon.txt should yield SDancerBlade inheriting from WPN_Dagger."""
    path = next(p for p in all_stats_txt() if p.name == "Weapon.txt")
    stats = stats_text.parse_file(path)
    entry = stats.by_name("SDancerBlade")
    assert entry is not None
    assert entry.type == "Weapon"
    assert entry.using == "WPN_Dagger"
    assert entry.data_value("RootTemplate") == "d21296e6-898c-4072-8c24-4c5a26f249f0"


# --- Merging -----------------------------------------------------------------


def test_merge_disjoint_files():
    """Merging two files with no overlapping names produces all entries
    in (A then B) order with zero conflicts."""
    a = stats_text.parse_text(
        'new entry "Foo"\r\ntype "Weapon"\r\n\r\n'
    )
    b = stats_text.parse_text(
        'new entry "Bar"\r\ntype "Weapon"\r\n\r\n'
    )
    merged, conflicts = stats_text.merge(a, b)
    assert merged.names() == ["Foo", "Bar"]
    assert conflicts == []


def test_merge_identical_dedupes_silently():
    """When the same entry appears identically in both files, it's deduped
    without raising a conflict."""
    body = 'new entry "Foo"\r\ntype "Weapon"\r\ndata "Rarity" "Rare"\r\n\r\n'
    a = stats_text.parse_text(body)
    b = stats_text.parse_text(body)
    merged, conflicts = stats_text.merge(a, b)
    assert merged.names() == ["Foo"]
    assert conflicts == []


def test_merge_conflict_no_prefix_omits_b_and_reports():
    """Same name, different content, no prefix policy:
    B's entry is omitted, conflict is reported. The user has to resolve."""
    a = stats_text.parse_text('new entry "Foo"\r\ntype "Weapon"\r\ndata "Rarity" "Rare"\r\n')
    b = stats_text.parse_text('new entry "Foo"\r\ntype "Weapon"\r\ndata "Rarity" "Common"\r\n')
    merged, conflicts = stats_text.merge(a, b)
    assert merged.names() == ["Foo"]
    assert merged.by_name("Foo").data_value("Rarity") == "Rare"  # A wins
    assert len(conflicts) == 1
    assert conflicts[0].name == "Foo"


def test_merge_conflict_with_prefix_renames_b():
    """Same name, different content, with prefix policy:
    B's entry is renamed (kept in output), conflict still reported."""
    a = stats_text.parse_text('new entry "Foo"\r\ntype "Weapon"\r\ndata "Rarity" "Rare"\r\n')
    b = stats_text.parse_text('new entry "Foo"\r\ntype "Weapon"\r\ndata "Rarity" "Common"\r\n')
    merged, conflicts = stats_text.merge(a, b, prefix_b_on_conflict="ModB_")
    assert merged.names() == ["Foo", "ModB_Foo"]
    assert merged.by_name("ModB_Foo").data_value("Rarity") == "Common"
    assert len(conflicts) == 1


def test_merge_shadow_dance_with_shadowdancer_no_conflicts():
    """The two real fixture projects have entirely disjoint stat names.
    Merging any pair of their .txt files (even across different stat types,
    which shouldn't happen in practice) should produce zero conflicts."""
    sd_passive = next(p for p in all_stats_txt() if p.name == "Status_BOOST.txt")
    sd_invis = next(p for p in all_stats_txt() if p.name == "Status_INVISIBLE.txt")

    a = stats_text.parse_file(sd_passive)
    b = stats_text.parse_file(sd_invis)
    # Different stat types in the same file is technically unusual but the
    # parser/merger don't care — only the reference index will warn later.
    merged, conflicts = stats_text.merge(a, b)
    assert conflicts == []
    assert len(merged.entries) == len(a.entries) + len(b.entries)


# --- Defensive parsing -------------------------------------------------------


def test_parses_with_trailing_blank_lines():
    text = 'new entry "X"\r\ntype "Y"\r\n\r\n\r\n'  # extra blank lines
    parsed = stats_text.parse_text(text)
    assert parsed.names() == ["X"]


def test_rejects_data_outside_entry():
    text = 'data "Key" "Value"\r\n'
    with pytest.raises(stats_text.StatsParseError):
        stats_text.parse_text(text)


def test_rejects_unknown_keyword():
    """Catches format drift; we'd rather see the failure than silently lose data."""
    text = 'new entry "X"\r\nunknown_keyword "Y"\r\n'
    with pytest.raises(stats_text.StatsParseError):
        stats_text.parse_text(text)


def test_rejects_embedded_quote_in_value():
    """Writing back a value containing a literal quote would corrupt the file."""
    entry = stats_text.StatsEntry(name="X", type="Y", data=[("K", 'has "quote" inside')])
    with pytest.raises(ValueError, match="embedded double-quote"):
        stats_text.serialize_entry(entry)
