"""Tests for ``core.remap``."""

from __future__ import annotations

import pytest

from core import remap, stats_text, stats_xml, localization, lsx
from .helpers import FIXTURES


# --- Per-kind tables --------------------------------------------------------


def test_uuid_remap_basic():
    r = remap.UuidRemap()
    r.add("aaaa-bbbb", "cccc-dddd")
    assert r.apply("aaaa-bbbb") == "cccc-dddd"
    assert r.apply("not-in-table") == "not-in-table"


def test_remap_rejects_ambiguous_entry():
    r = remap.StatRemap()
    r.add("FOO", "BAR")
    with pytest.raises(ValueError, match="already maps to"):
        r.add("FOO", "BAZ")


def test_remap_allows_idempotent_re_add():
    """Re-adding the same mapping is a no-op (caller may build the table
    incrementally from overlapping sources)."""
    r = remap.StatRemap()
    r.add("FOO", "BAR")
    r.add("FOO", "BAR")  # no error


def test_remap_set_is_empty_default():
    s = remap.RemapSet()
    assert s.is_empty()


# --- Value-level rewrite ----------------------------------------------------


def test_rewrite_value_no_remaps_is_noop():
    s = remap.RemapSet()
    assert remap.rewrite_value("anything goes here", s) == "anything goes here"


def test_rewrite_value_rewrites_stat_tokens_in_functor_list():
    """The classic case: ``ApplyStatus(INVISIBLEKira, 100, 1)`` -> if we
    remap INVISIBLEKira to ModB_INVISIBLEKira the call rewrites correctly."""
    s = remap.RemapSet()
    s.stats.add("INVISIBLEKira", "ModB_INVISIBLEKira")
    out = remap.rewrite_value(
        "ApplyStatus(INVISIBLEKira,100,1);DealDamage(1d6,Necrotic)",
        s,
    )
    assert out == (
        "ApplyStatus(ModB_INVISIBLEKira,100,1);DealDamage(1d6,Necrotic)"
    )


def test_rewrite_value_doesnt_rewrite_substrings():
    """Whole-word match only: INVISIBLE shouldn't be touched if INVISIBLEKira
    is also defined."""
    s = remap.RemapSet()
    s.stats.add("INVISIBLE", "ModB_INVISIBLE")
    out = remap.rewrite_value("ApplyStatus(INVISIBLEKira,100,1)", s)
    # Boundary-aware: INVISIBLEKira stays whole.
    assert "ModB_INVISIBLE" not in out  # didn't sneak in as a prefix
    assert "INVISIBLEKira" in out


def test_rewrite_value_handles_joined_handle_version():
    """Loca handle in joined `handle;version` form gets remapped, version
    is preserved."""
    s = remap.RemapSet()
    old = "h2db009beg91d6g310eg03c0g9e0885029fce"
    new = "h99999999g9999g9999g9999g999999999999"
    s.handles.add(old, new)
    out = remap.rewrite_value(f"some-prefix {old};2 suffix", s)
    assert out == f"some-prefix {new};2 suffix"


def test_rewrite_value_handles_bare_handle():
    """Loca handle inside an LSX attribute value (no ;version)."""
    s = remap.RemapSet()
    old = "h2db009beg91d6g310eg03c0g9e0885029fce"
    new = "h99999999g9999g9999g9999g999999999999"
    s.handles.add(old, new)
    assert remap.rewrite_value(old, s) == new


def test_rewrite_value_rewrites_uuid_case_insensitively():
    """Larian uses lowercase but if upstream tools emit mixed case we still match."""
    s = remap.RemapSet()
    s.uuids.add("50deb7b5-8734-7111-cb00-6682390ee00c",
                "00000000-0000-0000-0000-000000000000")
    out = remap.rewrite_value(
        "Module=50deb7b5-8734-7111-CB00-6682390ee00c (mixed case)",
        s,
    )
    assert out == "Module=00000000-0000-0000-0000-000000000000 (mixed case)"


def test_rewrite_value_path_remap_longest_match():
    s = remap.RemapSet()
    s.paths.add_substring("Public/Old", "Public/New")
    s.paths.add_substring("Public/Old/Specific", "Public/SpecificNew")
    out = remap.rewrite_value("Public/Old/Specific/foo.lsf", s)
    # Longest-match wins.
    assert out == "Public/SpecificNew/foo.lsf"


def test_path_remap_rejects_ambiguous():
    p = remap.PathRemap()
    p.add_substring("A", "B")
    with pytest.raises(ValueError):
        p.add_substring("A", "C")


# --- Bulk rewriters ---------------------------------------------------------


def test_rewrite_stats_text_propagates_rename_through_all_data_refs():
    """Renaming INVISIBLEKira should affect:
    - its own definition (new entry)
    - every using ref
    - every data value that mentions it
    """
    txt = (
        'new entry "INVISIBLEKira"\r\n'
        'type "StatusData"\r\n'
        'data "DisplayName" "h11111111g1111g1111g1111g111111111111;1"\r\n'
        '\r\n'
        'new entry "DependsOnIt"\r\n'
        'type "SpellData"\r\n'
        'using "INVISIBLEKira"\r\n'
        'data "SpellProperties" "ApplyStatus(INVISIBLEKira,100,1);"\r\n'
    )
    parsed = stats_text.parse_text(txt)
    s = remap.RemapSet()
    s.stats.add("INVISIBLEKira", "ModB_INVISIBLEKira")

    remap.rewrite_stats_text(parsed, s)

    assert parsed.by_name("ModB_INVISIBLEKira") is not None
    dep = parsed.by_name("DependsOnIt")
    assert dep is not None
    assert dep.using == "ModB_INVISIBLEKira"
    assert dep.data_value("SpellProperties") == (
        "ApplyStatus(ModB_INVISIBLEKira,100,1);"
    )


def test_rewrite_stats_text_icon_remap_only_on_icon_key():
    """Icons get their dedicated remap; other data keys aren't subject to it.
    The 'Icon' key should be a single-token replace, not a token rewrite."""
    txt = (
        'new entry "X"\r\n'
        'type "SpellData"\r\n'
        'data "Icon" "Old_Icon_Name"\r\n'
        'data "Other" "Old_Icon_Name plus more text"\r\n'
    )
    parsed = stats_text.parse_text(txt)
    s = remap.RemapSet()
    s.icons.add("Old_Icon_Name", "New_Icon_Name")

    remap.rewrite_stats_text(parsed, s)

    entry = parsed.by_name("X")
    assert entry.data_value("Icon") == "New_Icon_Name"
    # The "Other" key goes through value rewriter, which doesn't touch
    # icon names (icon remap isn't part of rewrite_value).
    assert entry.data_value("Other") == "Old_Icon_Name plus more text"


def test_rewrite_stats_xml_renames_name_field_and_uuid():
    """The Name field gets stat-remapped; the UUID field gets uuid-remapped."""
    obj = stats_xml.StatsXmlObject(
        is_substat=False,
        fields=[
            stats_xml.StatsXmlField("UUID", "IdTableFieldDefinition",
                                    {"value": "11111111-1111-1111-1111-111111111111"}),
            stats_xml.StatsXmlField("Name", "NameTableFieldDefinition",
                                    {"value": "FooBar"}),
            stats_xml.StatsXmlField("DisplayName", "TranslatedStringTableFieldDefinition",
                                    {"handle": "h11111111g1111g1111g1111g111111111111",
                                     "version": "1"}),
        ],
    )
    f = stats_xml.StatsXmlFile("aaaa-uuid", [obj])
    s = remap.RemapSet()
    s.stats.add("FooBar", "ModB_FooBar")
    s.uuids.add("11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222")
    s.handles.add("h11111111g1111g1111g1111g111111111111",
                  "h22222222g2222g2222g2222g222222222222")

    remap.rewrite_stats_xml(f, s)

    assert obj.field_by_name("Name").value == "ModB_FooBar"
    assert obj.field_by_name("UUID").value == "22222222-2222-2222-2222-222222222222"
    assert obj.field_by_name("DisplayName").handle == (
        "h22222222g2222g2222g2222g222222222222"
    )


def test_rewrite_localization_rewrites_contentuid():
    """contentuid is the handle itself: gets the handle remap."""
    f = localization.LocaFile(entries=[
        localization.LocaEntry(
            contentuid="h11111111g1111g1111g1111g111111111111",
            version="1",
            text="Hello",
        ),
    ])
    s = remap.RemapSet()
    s.handles.add("h11111111g1111g1111g1111g111111111111",
                  "h22222222g2222g2222g2222g222222222222")
    remap.rewrite_localization(f, s)
    assert f.entries[0].contentuid == "h22222222g2222g2222g2222g222222222222"


def test_rewrite_lsx_rewrites_translated_string_handle():
    """TranslatedString attrs hold the handle in their handle field, not value."""
    n = lsx.Node(
        id="Test",
        attributes=[
            lsx.Attribute("DisplayName", "TranslatedString",
                          handle="h11111111g1111g1111g1111g111111111111", version="1"),
        ],
    )
    doc = lsx.LsxDocument(
        version=lsx.Version("4", "0", "0", "0"),
        regions=[lsx.Region(id="R", root_node=n)],
    )
    s = remap.RemapSet()
    s.handles.add("h11111111g1111g1111g1111g111111111111",
                  "h22222222g2222g2222g2222g222222222222")
    remap.rewrite_lsx(doc, s)
    assert n.attr("DisplayName").handle == "h22222222g2222g2222g2222g222222222222"


def test_rewrite_lsx_path_attrs_use_path_remap():
    """SourceFile / Path / ResourcePath get the path-substring remap, not
    the generic value rewrite: they're allowed to contain folder names
    that look like stat tokens (e.g. ``Public/Mod_uuid/Banks/Foo.lsf``)."""
    n = lsx.Node(
        id="Test",
        attributes=[
            lsx.Attribute("SourceFile", "LSString",
                          value="Public/OldFolder/Banks/Foo.lsf"),
        ],
    )
    doc = lsx.LsxDocument(
        version=lsx.Version("4", "0", "0", "0"),
        regions=[lsx.Region(id="R", root_node=n)],
    )
    s = remap.RemapSet()
    s.paths.add_substring("Public/OldFolder", "Public/NewFolder")
    remap.rewrite_lsx(doc, s)
    assert n.attr("SourceFile").value == "Public/NewFolder/Banks/Foo.lsf"


# --- On real fixture files --------------------------------------------------


def test_real_stats_txt_unchanged_with_empty_remaps():
    """The trivial case the merger hits constantly: an input that doesn't
    conflict with anything has its content pass through untouched."""
    path = next(
        (FIXTURES / "ShadowDance").glob("Public/*/Stats/Generated/Data/Spell_Target.txt")
    )
    parsed = stats_text.parse_file(path)
    serialized_before = stats_text.serialize(parsed)
    remap.rewrite_stats_text(parsed, remap.RemapSet())
    assert stats_text.serialize(parsed) == serialized_before


def test_real_stats_txt_full_rename_pass():
    """Apply a stat rename to a real file and confirm both the entry name
    and the value-level reference both update."""
    path = next(
        (FIXTURES / "ShadowDance").glob("Public/*/Stats/Generated/Data/Spell_Shout.txt")
    )
    parsed = stats_text.parse_file(path)

    s = remap.RemapSet()
    s.stats.add("INVISIBLEKira", "ModX_INVISIBLEKira")
    remap.rewrite_stats_text(parsed, s)

    shout = parsed.by_name("Shout_ShadowDanceK")
    assert shout is not None
    # The functor list inside SpellProperties should reflect the rename.
    sp = shout.data_value("SpellProperties")
    assert "ApplyStatus(ModX_INVISIBLEKira," in sp
    # And the unrenamed form should be gone (check with the opening paren
    # so we don't false-positive on the prefixed substring).
    assert "ApplyStatus(INVISIBLEKira," not in sp
