"""Regression tests for bugs surfaced by the second wave of fixtures
(Bloodfang, LampOfLuxury, Treehome).

Each test pins down a behavior that was broken before and would be hard
to spot without re-walking real data, so we want a deterministic guard
against it coming back.
"""

from __future__ import annotations

import pytest

from core import (
    merger, references, stats_text, stats_xml, validate, meta as _meta,
)
from core.project import Project, FileCategory
from .helpers import FIXTURES


# --- using_index: interleaved 'using' position ------------------------------


def test_using_interleaved_between_data_lines_roundtrips():
    """Some authors place `using` AFTER one or more `data` lines (Bloodfang's
    Spell_Shout.txt is a real example). The byte-exact round-trip should
    preserve that ordering."""
    txt = (
        'new entry "X"\r\n'
        'type "SpellData"\r\n'
        'data "First" "1"\r\n'
        'using "Parent"\r\n'
        'data "Second" "2"\r\n'
    )
    parsed = stats_text.parse_text(txt)
    entry = parsed.entries[0]
    # Recorded between data[0] and data[1] = index 1
    assert entry.using == "Parent"
    assert entry.using_index == 1
    # Round-trips to the same bytes.
    assert stats_text.serialize(parsed) == txt


def test_using_at_conventional_position_roundtrips():
    """The conventional Toolkit position (right after `type`, before any
    `data`) corresponds to using_index == 0 and must round-trip too."""
    txt = (
        'new entry "X"\r\n'
        'type "SpellData"\r\n'
        'using "Parent"\r\n'
        'data "K" "V"\r\n'
    )
    parsed = stats_text.parse_text(txt)
    entry = parsed.entries[0]
    assert entry.using_index == 0
    assert stats_text.serialize(parsed) == txt


def test_programmatic_entry_without_using_index_emits_conventional():
    """Code that constructs StatsEntry directly (without going through the
    parser) shouldn't have to set using_index — the serializer should
    default to the conventional position."""
    e = stats_text.StatsEntry(
        name="X", type="SpellData", using="Parent",
        data=[("K", "V")],
        # using_index left as default None
    )
    text = stats_text.serialize_entry(e)
    assert text == 'new entry "X"\r\ntype "SpellData"\r\nusing "Parent"\r\ndata "K" "V"'


def test_using_index_preserved_when_prefixed_on_conflict():
    """When the merger renames an entry on conflict, the using_index has
    to come along — otherwise the merged file's ordering would differ from
    B's source even when there's no semantic reason to change it."""
    a_text = (
        'new entry "Foo"\r\n'
        'type "SpellData"\r\n'
        'using "ParentA"\r\n'
        'data "Cost" "1"\r\n'
    )
    b_text = (
        'new entry "Foo"\r\n'
        'type "SpellData"\r\n'
        'data "Cost" "2"\r\n'
        'using "ParentB"\r\n'  # interleaved
    )
    a = stats_text.parse_text(a_text)
    b = stats_text.parse_text(b_text)
    merged, conflicts = stats_text.merge(a, b, prefix_b_on_conflict="ModB_")
    assert len(conflicts) == 1
    renamed = merged.by_name("ModB_Foo")
    assert renamed is not None
    assert renamed.using_index == 1  # preserved from B


# --- Substats: shared Name is legitimate, identity is UUID ------------------


def _make_substat(name: str, uuid: str) -> stats_xml.StatsXmlObject:
    return stats_xml.StatsXmlObject(
        is_substat=True,
        fields=[
            stats_xml.StatsXmlField("UUID", "IdTableFieldDefinition", {"value": uuid}),
            stats_xml.StatsXmlField("Name", "NameTableFieldDefinition", {"value": name}),
        ],
    )


def _make_primary(name: str, uuid: str) -> stats_xml.StatsXmlObject:
    return stats_xml.StatsXmlObject(
        is_substat=False,
        fields=[
            stats_xml.StatsXmlField("UUID", "IdTableFieldDefinition", {"value": uuid}),
            stats_xml.StatsXmlField("Name", "NameTableFieldDefinition", {"value": name}),
        ],
    )


def test_substats_with_shared_name_distinct_uuids_all_kept():
    """The classic treasure-table case: four substats all named
    ``X_substat`` with different UUIDs and different roles. All four
    must survive a merge (none silently dropped)."""
    sod = "e4012e18-6a6b-4f40-aefa-c83b078c136c"  # arbitrary type UUID
    a = stats_xml.StatsXmlFile(sod, [
        _make_primary("Parent", "00000000-0000-0000-0000-000000000001"),
        _make_substat("Parent_substat", "00000000-0000-0000-0000-000000000002"),
        _make_substat("Parent_substat", "00000000-0000-0000-0000-000000000003"),
        _make_substat("Parent_substat", "00000000-0000-0000-0000-000000000004"),
        _make_substat("Parent_substat", "00000000-0000-0000-0000-000000000005"),
    ])
    # Merge against an empty file of the same type — all of A's must pass through.
    b = stats_xml.StatsXmlFile(sod, [])
    merged, conflicts = stats_xml.merge(a, b)
    assert conflicts == []
    assert len(merged.objects) == 5
    # All four substats retained.
    substats = [o for o in merged.objects if o.is_substat]
    assert len(substats) == 4
    assert all(o.name == "Parent_substat" for o in substats)


def test_substat_same_uuid_in_both_inputs_dedupes():
    """If A and B both happen to declare the same substat (same UUID, same
    body), the merge dedups silently rather than emitting twice."""
    sod = "e4012e18-6a6b-4f40-aefa-c83b078c136c"
    s = _make_substat("X_substat", "11111111-1111-1111-1111-111111111111")
    a = stats_xml.StatsXmlFile(sod, [s])
    b = stats_xml.StatsXmlFile(sod, [s])
    merged, conflicts = stats_xml.merge(a, b)
    assert len(merged.objects) == 1
    assert conflicts == []


def test_substats_in_a_dont_collide_by_name_with_b_primary():
    """A's substats indexed by UUID; B's primary stat with the same Name
    as an A-substat should still be treated as new (NAME index lookup
    won't find it)."""
    sod = "e4012e18-6a6b-4f40-aefa-c83b078c136c"
    a = stats_xml.StatsXmlFile(sod, [
        _make_substat("FooBar", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
    ])
    b = stats_xml.StatsXmlFile(sod, [
        _make_primary("FooBar", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
    ])
    merged, conflicts = stats_xml.merge(a, b)
    # No name collision (A has no PRIMARY named FooBar, just a substat).
    # Both objects survive.
    assert conflicts == []
    assert len(merged.objects) == 2


def test_reference_index_doesnt_flag_substat_name_as_collision():
    """The validator was previously flagging multiple substats with the
    same Name as a definition collision. After the fix, substats only
    define UUIDs, not stat names."""
    p = Project.load(FIXTURES / "Bloodfang")
    report = validate.validate(p)
    # Bloodfang's TreasureTable.stats has 4 substats named TUT_Chest_Potions_substat.
    # Pre-fix this would be flagged as a stat_name collision.
    if "stat_name" in report.definition_collisions:
        for entry in report.definition_collisions["stat_name"]:
            assert entry.value != "TUT_Chest_Potions_substat", (
                f"validator incorrectly flagged a substat name as a collision: "
                f"{entry.value!r}"
            )


# --- Merger gates LSX-parsing on extension ---------------------------------


def test_merger_copies_binary_lsf_in_lsx_categories_verbatim(tmp_path):
    """UI_MERGED, ICON_UV_LSF, ROOT_TEMPLATE_MERGED can include .lsf binary
    files. The merger must NOT try to parse those as text; it should copy
    bytes through unchanged."""
    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Bloodfang")

    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=tmp_path / "out",
        new_uuid=_meta.generate_uuid(),
        new_folder="LSF_Gate_Test",
        new_name="LSF Gate Test",
        conflict_policy="skip",
    )
    # Pre-fix this would have crashed with `Start tag expected, '<' not found`
    # when the merger reached Bloodfang's _merged.lsf or Simple_Icons.lsf.
    result = merger.merge(config)

    # The binary LSF files came through:
    icon_lsf = tmp_path / "out" / "Public" / "LSF_Gate_Test" / "GUI" / "Simple_Icons.lsf"
    assert icon_lsf.is_file()
    # Bytes identical to the source (verbatim copy).
    src = (FIXTURES / "Bloodfang" / "Public" / "BloodFang" / "GUI" / "Simple_Icons.lsf").read_bytes()
    assert icon_lsf.read_bytes() == src

    ui_merged = tmp_path / "out" / "Public" / "LSF_Gate_Test" / "Content" / "UI" / "[PAK]_UI" / "_merged.lsf"
    assert ui_merged.is_file()

    rt_merged = tmp_path / "out" / "Public" / "LSF_Gate_Test" / "RootTemplates" / "_merged.lsf"
    assert rt_merged.is_file()


# --- New file categories: every file is categorized ------------------------


@pytest.mark.parametrize("project_name", [
    "ShadowDance", "Shadowdancer", "Bloodfang", "LampOfLuxury", "Treehome",
])
def test_no_files_categorized_as_other(project_name):
    """The categorizer's coverage invariant: every file in any fixture
    project maps to a specific FileCategory. Any new file landing in
    OTHER tells us about a pattern we haven't seen before."""
    p = Project.load(FIXTURES / project_name)
    others = p.files_by_category(FileCategory.OTHER)
    assert others == [], (
        f"{project_name}: {len(others)} uncategorized files. "
        f"Sample: {[str(o.rel_to_project_root) for o in others[:5]]}"
    )


def test_bloodfang_folder_name_without_uuid_loads():
    """Non-Toolkit-pipeline mods can use folder names that don't follow
    the Toolkit's ``Name_UUID`` convention. Bloodfang uses just
    ``BloodFang``. The Project loader must accept this."""
    p = Project.load(FIXTURES / "Bloodfang")
    assert p.mod_folder_name == "BloodFang"
    assert "_" not in p.mod_folder_name
    # And the loader still validated meta.lsx Folder against on-disk.
    assert p.mod_meta.folder == "BloodFang"


def test_treehome_loads_at_scale():
    """Treehome has 3,300+ files (mostly per-region scenery LSFs). The
    loader must walk all of them and categorize each. This is a smoke
    test for scale, not a correctness test for specific entries."""
    p = Project.load(FIXTURES / "Treehome")
    assert len(p.files) > 3000
    # Sanity: most are level content under Mods/<Mod>/Levels/<Region>/.
    level_lsfs = p.files_by_category(FileCategory.LEVEL_CONTENT_LSF)
    assert len(level_lsfs) > 2500


def test_empty_stats_xml_file_is_legitimate():
    """The Toolkit creates a `.stats` file with empty `<stat_objects />`
    when a schema is declared but no entries added. The parser handles it
    without raising; the merger copies it through."""
    sod = "00000000-0000-0000-0000-000000000000"
    empty = stats_xml.StatsXmlFile(sod, [])
    # Serialize and reparse — empty file round-trips.
    body = empty.to_xml_bytes()
    reparsed = stats_xml.parse_bytes(body)
    assert reparsed.stat_object_definition_id == sod
    assert reparsed.objects == []
