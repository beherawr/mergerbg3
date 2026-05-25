"""Tests for ``core.treasure_table``."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core import merger, treasure_table as tt, meta as _meta
from core.project import Project
from .helpers import FIXTURES


# --- Parsing -----------------------------------------------------------------


def test_parses_real_bloodfang_treasure_table():
    """Bloodfang's TreasureTable.txt: 1 table (TUT_Chest_Potions), CanMerge=1,
    4 subtables (one per armor piece)."""
    p = (FIXTURES / "Bloodfang" / "Public" / "BloodFang"
         / "Stats" / "Generated" / "TreasureTable.txt")
    parsed = tt.parse_file(p)

    assert parsed.itemtypes == [
        "Common", "Uncommon", "Rare", "Epic", "Legendary", "Divine", "Unique",
    ]
    assert len(parsed.tables) == 1
    t = parsed.by_name("TUT_Chest_Potions")
    assert t is not None
    assert t.can_merge is True
    assert len(t.subtables) == 4
    # First subtable references the BloodFang_Helmet2 category.
    assert t.subtables[0].objects[0][0] == "I_BloodFang_Helmet2"
    assert t.subtables[0].drop_count == "1,1"


def test_parses_real_lamp_treasure_table():
    """LampOfLuxury's TreasureTable.txt: 2 tables (TUT_Chest_Potions + MysticFountain)."""
    p = (FIXTURES / "LampOfLuxury" / "Public"
         / "LampOfLuxury_8d879fd3-1bcf-ecf6-91d2-5afcad7bd4a6"
         / "Stats" / "Generated" / "TreasureTable.txt")
    parsed = tt.parse_file(p)

    assert {t.name for t in parsed.tables} == {"TUT_Chest_Potions", "MysticFountain"}
    tut = parsed.by_name("TUT_Chest_Potions")
    assert tut is not None
    assert tut.can_merge is True
    assert len(tut.subtables) == 1
    assert tut.subtables[0].objects[0][0] == "I_LampOLLamp"


@pytest.mark.parametrize("path", [
    (FIXTURES / "Bloodfang" / "Public" / "BloodFang"
     / "Stats" / "Generated" / "TreasureTable.txt"),
    (FIXTURES / "LampOfLuxury" / "Public"
     / "LampOfLuxury_8d879fd3-1bcf-ecf6-91d2-5afcad7bd4a6"
     / "Stats" / "Generated" / "TreasureTable.txt"),
])
def test_real_files_roundtrip_byte_exact(path):
    """Read, serialize, compare bytes. Catches any silent reformatting."""
    original = path.read_bytes()
    if original.startswith(b"\xef\xbb\xbf"):
        original = original[3:]
    parsed = tt.parse_file(path)
    rewritten = tt.serialize(parsed).encode("utf-8")
    assert rewritten == original, (
        f"round-trip mismatch in {path}\n"
        f"--- original ({len(original)} bytes) ---\n{original[:200]!r}\n"
        f"--- rewritten ({len(rewritten)} bytes) ---\n{rewritten[:200]!r}\n"
    )


def test_parser_rejects_subtable_before_table():
    with pytest.raises(tt.TreasureParseError, match="outside any treasuretable"):
        tt.parse_text('new subtable "1,1"\r\n')


def test_parser_rejects_object_before_subtable():
    with pytest.raises(tt.TreasureParseError, match="outside any subtable"):
        tt.parse_text(
            'new treasuretable "X"\r\n'
            'object category "I_Foo",1,0,0,0,0,0,0,0\r\n'
        )


# --- Merging -----------------------------------------------------------------


HEADER = (
    'treasure itemtypes "Common","Uncommon","Rare","Epic","Legendary","Divine","Unique"\r\n'
)


def test_merge_disjoint_tables():
    a = tt.parse_text(HEADER + 'new treasuretable "A1"\r\nnew subtable "1,1"\r\nobject category "I_X",1,0,0,0,0,0,0,0\r\n')
    b = tt.parse_text(HEADER + 'new treasuretable "B1"\r\nnew subtable "1,1"\r\nobject category "I_Y",1,0,0,0,0,0,0,0\r\n')
    merged, conflicts = tt.merge(a, b)
    assert conflicts == []
    assert [t.name for t in merged.tables] == ["A1", "B1"]


def test_merge_canmerge_tables_concatenate_subtables():
    """The flagship behavior: both inputs define TUT_Chest_Potions with
    CanMerge=1 — output table holds the union of their subtables (no
    conflict, no user prompt needed). Mirrors the game's own runtime
    merging behavior."""
    a = tt.parse_text(
        HEADER
        + 'new treasuretable "TUT_Chest_Potions"\r\n'
        + 'CanMerge 1\r\n'
        + 'new subtable "1,1"\r\nobject category "I_FromA",1,0,0,0,0,0,0,0\r\n'
    )
    b = tt.parse_text(
        HEADER
        + 'new treasuretable "TUT_Chest_Potions"\r\n'
        + 'CanMerge 1\r\n'
        + 'new subtable "1,1"\r\nobject category "I_FromB",1,0,0,0,0,0,0,0\r\n'
    )
    merged, conflicts = tt.merge(a, b)
    assert conflicts == []
    out_t = merged.by_name("TUT_Chest_Potions")
    assert out_t is not None
    # Two subtables: one from A, one from B.
    assert len(out_t.subtables) == 2
    categories = [obj[0] for sub in out_t.subtables for obj in sub.objects]
    assert categories == ["I_FromA", "I_FromB"]


def test_merge_canmerge_with_real_fixtures():
    """Bloodfang + LampOfLuxury both have TUT_Chest_Potions with CanMerge=1.
    The merged file should hold ALL of Bloodfang's 4 subtables PLUS Lamp's 1,
    AND Lamp's MysticFountain table — no conflicts emitted."""
    a = tt.parse_file(
        FIXTURES / "Bloodfang" / "Public" / "BloodFang"
        / "Stats" / "Generated" / "TreasureTable.txt"
    )
    b = tt.parse_file(
        FIXTURES / "LampOfLuxury" / "Public"
        / "LampOfLuxury_8d879fd3-1bcf-ecf6-91d2-5afcad7bd4a6"
        / "Stats" / "Generated" / "TreasureTable.txt"
    )
    merged, conflicts = tt.merge(a, b)
    assert conflicts == []  # CanMerge handles the overlap cleanly.

    tut = merged.by_name("TUT_Chest_Potions")
    assert tut is not None
    # 4 from Bloodfang + 1 from Lamp = 5 subtables.
    assert len(tut.subtables) == 5

    cats = [obj[0] for sub in tut.subtables for obj in sub.objects]
    assert cats == [
        "I_BloodFang_Helmet2", "I_BloodFang_Armor2",
        "I_BloodFang_Gloves2", "I_BloodFang_Legs2",
        "I_LampOLLamp",
    ]
    # Lamp's MysticFountain (unique to B) came through.
    assert merged.by_name("MysticFountain") is not None


def test_merge_different_content_without_canmerge_is_a_conflict():
    """If two tables share a name but neither has CanMerge=1, that's a real
    conflict requiring user resolution."""
    a = tt.parse_text(HEADER + 'new treasuretable "FixedDrop"\r\nnew subtable "1,1"\r\nobject category "I_A",1,0,0,0,0,0,0,0\r\n')
    b = tt.parse_text(HEADER + 'new treasuretable "FixedDrop"\r\nnew subtable "1,1"\r\nobject category "I_B",1,0,0,0,0,0,0,0\r\n')
    merged, conflicts = tt.merge(a, b)
    assert len(conflicts) == 1
    # A wins by default; only one table named FixedDrop in output.
    fixed = [t for t in merged.tables if t.name == "FixedDrop"]
    assert len(fixed) == 1
    assert fixed[0].subtables[0].objects[0][0] == "I_A"


def test_merge_conflict_with_prefix_renames_b():
    a = tt.parse_text(HEADER + 'new treasuretable "FixedDrop"\r\nnew subtable "1,1"\r\nobject category "I_A",1,0,0,0,0,0,0,0\r\n')
    b = tt.parse_text(HEADER + 'new treasuretable "FixedDrop"\r\nnew subtable "1,1"\r\nobject category "I_B",1,0,0,0,0,0,0,0\r\n')
    merged, conflicts = tt.merge(a, b, prefix_b_on_conflict="ModB_")
    assert len(conflicts) == 1
    names = sorted(t.name for t in merged.tables)
    assert "FixedDrop" in names
    assert "ModB_FixedDrop" in names


def test_merge_refuses_mismatched_itemtypes():
    """If the rarity column ordering differs, weights wouldn't line up.
    Refuse the merge rather than produce silently corrupt output."""
    a = tt.parse_text(HEADER + 'new treasuretable "X"\r\n')
    b = tt.parse_text(
        'treasure itemtypes "Common","Uncommon","Rare"\r\n'
        + 'new treasuretable "Y"\r\n'
    )
    with pytest.raises(ValueError, match="itemtypes"):
        tt.merge(a, b)


def test_identical_tables_dedupe_silently():
    a = tt.parse_text(HEADER + 'new treasuretable "X"\r\nnew subtable "1,1"\r\nobject category "I_F",1,0,0,0,0,0,0,0\r\n')
    b = tt.parse_text(HEADER + 'new treasuretable "X"\r\nnew subtable "1,1"\r\nobject category "I_F",1,0,0,0,0,0,0,0\r\n')
    merged, conflicts = tt.merge(a, b)
    assert conflicts == []
    assert len(merged.tables) == 1


# --- Integration: real merge produces a correct TreasureTable.txt -----------


def test_real_merge_treasure_table_canmerge_concatenation(tmp_path):
    """End-to-end: merge Bloodfang + Lamp, verify the output's
    TreasureTable.txt contains all 5 subtables and both table names —
    not just A's content."""
    a = Project.load(FIXTURES / "Bloodfang")
    b = Project.load(FIXTURES / "LampOfLuxury")

    new_uuid = _meta.generate_uuid()
    new_folder = f"TT_Test_{new_uuid.replace('-','')[:8]}"
    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=tmp_path / "out",
        new_uuid=new_uuid,
        new_folder=new_folder,
        new_name="TT integration",
        conflict_policy="skip",
    )
    result = merger.merge(config)

    # The merged .txt must include both Bloodfang's armor categories
    # AND Lamp's lamp, plus MysticFountain.
    out_txt_path = (
        tmp_path / "out" / "Public" / new_folder / "Stats" / "Generated"
        / "TreasureTable.txt"
    )
    assert out_txt_path.is_file()
    parsed = tt.parse_file(out_txt_path)

    assert {t.name for t in parsed.tables} == {
        "TUT_Chest_Potions", "MysticFountain",
    }
    tut = parsed.by_name("TUT_Chest_Potions")
    assert tut is not None
    assert len(tut.subtables) == 5  # 4 + 1
    cats = [obj[0] for sub in tut.subtables for obj in sub.objects]
    assert "I_BloodFang_Helmet2" in cats
    assert "I_LampOLLamp" in cats

    # No more file_overlap conflict on TreasureTable.txt — it merged properly.
    tt_conflicts = [
        c for c in result.conflicts
        if "TreasureTable.txt" in c.identifier
    ]
    assert tt_conflicts == [], (
        f"TreasureTable.txt should now merge cleanly, "
        f"got conflicts: {tt_conflicts}"
    )


# --- CanMerge on the .stats side (mirroring the .txt CanMerge concat) ----


def test_real_merge_no_canmerge_id_clashes(tmp_path):
    """Bloodfang + LampOfLuxury both define TUT_Chest_Potions with
    CanMerge=Yes in their .stats files. The reference index and merger
    should both treat this as a runtime-shared name (NOT a definition
    collision), so the merge produces zero identifier clashes — only the
    legitimate file overlaps (per-mod thumbnails) remain."""
    a = Project.load(FIXTURES / "Bloodfang")
    b = Project.load(FIXTURES / "LampOfLuxury")

    new_uuid = _meta.generate_uuid()
    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=tmp_path / "out",
        new_uuid=new_uuid,
        new_folder=f"CM_{new_uuid.replace('-','')[:8]}",
        new_name="CanMerge integration",
        conflict_policy="skip",
    )
    result = merger.merge(config)

    id_clashes = [c for c in result.conflicts if c.kind != "file_overlap"]
    assert id_clashes == [], (
        f"CanMerge tables should not produce id clashes; got: {id_clashes}"
    )
