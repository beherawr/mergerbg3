"""Tests for ``core.stats_xml`` against the real example projects."""

from __future__ import annotations

import pytest

from core import stats_xml
from .helpers import all_stats_xml


# --- Parsing -----------------------------------------------------------------


@pytest.mark.parametrize("path", all_stats_xml(), ids=lambda p: str(p.relative_to(p.parents[6])))
def test_parses_real_file(path):
    """Every real .stats file parses cleanly. Empty <stat_objects/> is a
    legitimate Toolkit state — the user has declared a stat schema in the
    file but added no entries yet."""
    parsed = stats_xml.parse_file(path)
    assert parsed.stat_object_definition_id  # always a UUID
    for obj in parsed.objects:
        assert obj.name is not None, f"object with no Name in {path}"


# --- Content spot-checks -----------------------------------------------------


def test_shadow_dance_target_stats_content():
    """Spot-check the .stats source of Target_BackstabK matches what we saw."""
    path = next(p for p in all_stats_xml() if p.name == "Target.stats")
    parsed = stats_xml.parse_file(path)
    obj = parsed.object_by_name("BackstabK")
    assert obj is not None
    # The DisplayName field carries a localization handle, NOT a value.
    display = obj.field_by_name("DisplayName")
    assert display is not None
    assert display.type == "TranslatedStringTableFieldDefinition"
    assert display.handle == "h2db009beg91d6g310eg03c0g9e0885029fce"
    assert display.version == "2"
    # The Name field is the unprefixed form (".txt" prefixes "Target_").
    name_field = obj.field_by_name("Name")
    assert name_field is not None and name_field.value == "BackstabK"


def test_shadowdancer_weapon_stats():
    """Shadowdancer's .stats file should describe SDancerBlade."""
    path = next(p for p in all_stats_xml() if p.name == "Weapon.stats")
    parsed = stats_xml.parse_file(path)
    obj = parsed.object_by_name("SDancerBlade")
    assert obj is not None
    rt = obj.field_by_name("RootTemplate")
    assert rt is not None
    assert rt.value == "d21296e6-898c-4072-8c24-4c5a26f249f0"


def test_stat_object_uuid_separate_from_name():
    """Every .stats object carries both a UUID (toolkit identity) and a Name
    (human ID). They're independent and the merger needs to track both."""
    path = next(p for p in all_stats_xml() if p.name == "Status_INVISIBLE.stats")
    parsed = stats_xml.parse_file(path)
    obj = parsed.objects[0]
    assert obj.uuid is not None and len(obj.uuid) == 36  # UUID format
    assert obj.name is not None and obj.name != obj.uuid


# --- Merging -----------------------------------------------------------------


def _make(sod_id: str, name: str, value: str = "X") -> stats_xml.StatsXmlObject:
    """Build a minimal stat_object for tests."""
    return stats_xml.StatsXmlObject(
        is_substat=False,
        fields=[
            stats_xml.StatsXmlField("UUID", "IdTableFieldDefinition",
                                    {"value": "00000000-0000-0000-0000-000000000000"}),
            stats_xml.StatsXmlField("Name", "NameTableFieldDefinition",
                                    {"value": name}),
            stats_xml.StatsXmlField("Boosts", "StringTableFieldDefinition",
                                    {"value": value}),
        ],
    )


def test_merge_requires_matching_definition_id():
    """Cannot merge two .stats files of different stat types."""
    a = stats_xml.StatsXmlFile("aaaaaaaa-...", [_make("aaaaaaaa-...", "X")])
    b = stats_xml.StatsXmlFile("bbbbbbbb-...", [_make("bbbbbbbb-...", "Y")])
    with pytest.raises(ValueError, match="stat_object_definition_id"):
        stats_xml.merge(a, b)


def test_merge_disjoint_names():
    sod = "e988a674-28fe-49d2-a6ce-c5c1e0141f4c"  # SpellData type UUID
    a = stats_xml.StatsXmlFile(sod, [_make(sod, "Foo")])
    b = stats_xml.StatsXmlFile(sod, [_make(sod, "Bar")])
    merged, conflicts = stats_xml.merge(a, b)
    assert [o.name for o in merged.objects] == ["Foo", "Bar"]
    assert conflicts == []


def test_merge_identical_dedupes():
    sod = "e988a674-28fe-49d2-a6ce-c5c1e0141f4c"
    a = stats_xml.StatsXmlFile(sod, [_make(sod, "Foo", "shared")])
    b = stats_xml.StatsXmlFile(sod, [_make(sod, "Foo", "shared")])
    merged, conflicts = stats_xml.merge(a, b)
    assert [o.name for o in merged.objects] == ["Foo"]
    assert conflicts == []


def test_merge_conflict_with_prefix_renames_name_field():
    sod = "e988a674-28fe-49d2-a6ce-c5c1e0141f4c"
    a = stats_xml.StatsXmlFile(sod, [_make(sod, "Foo", "A")])
    b = stats_xml.StatsXmlFile(sod, [_make(sod, "Foo", "B")])
    merged, conflicts = stats_xml.merge(a, b, prefix_b_on_conflict="ModB_")
    assert [o.name for o in merged.objects] == ["Foo", "ModB_Foo"]
    assert merged.objects[1].field_by_name("Name").value == "ModB_Foo"
    assert len(conflicts) == 1
    assert conflicts[0].name == "Foo"


def test_unknown_field_extras_round_trip():
    """A future Larian patch might add new field attributes. We round-trip
    them verbatim so we don't silently drop data."""
    f = stats_xml.StatsXmlField(
        "Custom", "NewTypeInFuturePatch",
        extra={"value": "1", "future_attr": "2"},
    )
    elem = f.to_xml()
    assert elem.get("value") == "1"
    assert elem.get("future_attr") == "2"


def test_diff_objects_returns_empty_for_identical():
    sod = "e988a674-28fe-49d2-a6ce-c5c1e0141f4c"
    a = _make(sod, "Foo", "X")
    b = _make(sod, "Foo", "X")
    assert stats_xml.diff_objects(a, b) == []


def test_diff_objects_finds_field_value_change():
    sod = "e988a674-28fe-49d2-a6ce-c5c1e0141f4c"
    a = _make(sod, "Foo", "X")
    b = _make(sod, "Foo", "Y")
    diffs = stats_xml.diff_objects(a, b)
    assert len(diffs) == 1
    assert "Boosts" in diffs[0]
