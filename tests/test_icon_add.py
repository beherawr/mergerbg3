"""Tests for core.icon_add: generating BG3 icon assets from a PNG.

Self-contained: builds a tiny mod skeleton under tmp_path and a synthetic
source PNG, so these run on CI without the private fixture mods.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from core import icon_add
from core import lsx


def _make_png(path: Path, size: int = 512, color=(200, 120, 40, 255)) -> Path:
    """Write a simple square RGBA PNG to use as icon source."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (size, size), color)
    # A diagonal so resizes/rotations would be visible if something's wrong.
    for i in range(min(size, size)):
        img.putpixel((i, i), (255, 255, 255, 255))
    img.save(path)
    return path


def _mod_skeleton(tmp_path: Path, mod_folder: str = "TestMod") -> Path:
    """Create a minimal Public/<mod>/ tree and return the data_root."""
    data_root = tmp_path / "ws"
    (data_root / "Public" / mod_folder).mkdir(parents=True)
    return data_root


def _is_readable_dds(path: Path) -> bool:
    """A DDS we wrote should be re-openable by Pillow and report a size."""
    try:
        im = Image.open(path)
        im.load()
        return im.width > 0 and im.height > 0
    except Exception:
        return False


# --- ATLAS family -----------------------------------------------------------


def test_atlas_spell_icon_creates_all_files(tmp_path):
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png", 512)

    result = icon_add.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="MySpellIcon", icon_type="Spell / Skill", png_path=png,
    )
    assert result.family is icon_add.IconFamily.ATLAS

    base = data_root / "Public" / "TestMod"
    # Tooltip (380, uppercase .DDS), Icons subfolder for spells.
    tooltip = base / "GUI" / "Assets" / "Tooltips" / "Icons" / "MySpellIcon.DDS"
    assert tooltip.exists() and _is_readable_dds(tooltip)
    assert Image.open(tooltip).size == (380, 380)

    # Controller (144), skills_png.
    controller = (data_root / "Public" / "Game" / "GUI" / "Assets"
                  / "ControllerUIIcons" / "skills_png" / "MySpellIcon.DDS")
    assert controller.exists() and _is_readable_dds(controller)
    assert Image.open(controller).size == (144, 144)

    # Atlas (lowercase .dds), 2048x2048.
    atlas = base / "Assets" / "Textures" / "Icons" / "Icons_TestMod.dds"
    assert atlas.exists() and _is_readable_dds(atlas)
    assert Image.open(atlas).size == (2048, 2048)

    # UV map and TextureBank.
    uv = base / "GUI" / "Icons_TestMod.lsx"
    merged = base / "Content" / "UI" / "[PAK]_UI" / "_merged.lsx"
    assert uv.exists()
    assert merged.exists()


def test_atlas_item_icon_uses_item_folders(tmp_path):
    """Item type routes to ItemIcons + items_png, not Icons + skills_png."""
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png", 400)
    icon_add.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="CoolSword", icon_type="Item", png_path=png,
    )
    base = data_root / "Public" / "TestMod"
    assert (base / "GUI" / "Assets" / "Tooltips" / "ItemIcons" / "CoolSword.DDS").exists()
    assert (data_root / "Public" / "Game" / "GUI" / "Assets"
            / "ControllerUIIcons" / "items_png" / "CoolSword.DDS").exists()
    # Spell folders should NOT have it.
    assert not (base / "GUI" / "Assets" / "Tooltips" / "Icons" / "CoolSword.DDS").exists()


def test_atlas_uv_map_has_correct_first_slot_coords(tmp_path):
    """First icon lands at slot 0: U1=V1=0, U2=V2=0.03125."""
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png")
    icon_add.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="First", icon_type="Spell / Skill", png_path=png,
    )
    uv = data_root / "Public" / "TestMod" / "GUI" / "Icons_TestMod.lsx"
    doc = lsx.parse_file(uv)
    region = doc.region("IconUVList")
    entries = [n for n in _iter(region.root_node) if n.id == "IconUV"]
    assert len(entries) == 1
    e = entries[0]
    assert e.attr_value("MapKey") == "First"
    assert float(e.attr_value("U1")) == 0.0
    assert float(e.attr_value("V1")) == 0.0
    assert abs(float(e.attr_value("U2")) - 0.03125) < 1e-6
    assert abs(float(e.attr_value("V2")) - 0.03125) < 1e-6


def test_atlas_second_icon_appends_to_existing(tmp_path):
    """Adding a second icon appends to the same atlas + UV map, landing in
    slot 1, and keeps the first icon."""
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png")

    icon_add.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="IconA", icon_type="Spell / Skill", png_path=png,
    )
    result_b = icon_add.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="IconB", icon_type="Spell / Skill", png_path=png,
    )
    # B should have appended (atlas + uv reported as updated, not written).
    assert result_b.slot_index == 1

    uv = data_root / "Public" / "TestMod" / "GUI" / "Icons_TestMod.lsx"
    doc = lsx.parse_file(uv)
    region = doc.region("IconUVList")
    names = {n.attr_value("MapKey") for n in _iter(region.root_node) if n.id == "IconUV"}
    assert names == {"IconA", "IconB"}

    # IconB at slot 1: U1 == 0.03125, V1 == 0.
    entry_b = next(n for n in _iter(region.root_node)
                   if n.id == "IconUV" and n.attr_value("MapKey") == "IconB")
    assert abs(float(entry_b.attr_value("U1")) - 0.03125) < 1e-6
    assert float(entry_b.attr_value("V1")) == 0.0


def test_atlas_readd_same_name_is_idempotent(tmp_path):
    """Re-adding an icon with the same name reuses its slot and doesn't
    create a duplicate UV entry."""
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png")
    icon_add.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="Dup", icon_type="Spell / Skill", png_path=png,
    )
    r2 = icon_add.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="Dup", icon_type="Spell / Skill", png_path=png,
    )
    assert r2.slot_index == 0  # reused, not slot 1
    uv = data_root / "Public" / "TestMod" / "GUI" / "Icons_TestMod.lsx"
    doc = lsx.parse_file(uv)
    region = doc.region("IconUVList")
    dups = [n for n in _iter(region.root_node)
            if n.id == "IconUV" and n.attr_value("MapKey") == "Dup"]
    assert len(dups) == 1


def test_atlas_texturebank_uuid_matches_uv_map(tmp_path):
    """The atlas UUID in the TextureBank must match the one in the UV map,
    or the game can't link them."""
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png")
    icon_add.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="Linked", icon_type="Spell / Skill", png_path=png,
    )
    base = data_root / "Public" / "TestMod"
    uv_doc = lsx.parse_file(base / "GUI" / "Icons_TestMod.lsx")
    merged_doc = lsx.parse_file(base / "Content" / "UI" / "[PAK]_UI" / "_merged.lsx")

    uv_uuid = None
    for n in _iter(uv_doc.region("TextureAtlasInfo").root_node):
        if n.id == "TextureAtlasPath":
            uv_uuid = n.attr_value("UUID")
    bank_uuid = None
    for n in _iter(merged_doc.region("TextureBank").root_node):
        if n.id == "Resource":
            bank_uuid = n.attr_value("ID")
    assert uv_uuid and bank_uuid and uv_uuid == bank_uuid


# --- CLASS family -----------------------------------------------------------


def test_class_icon_creates_four_dds_no_atlas(tmp_path):
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png", 512)
    result = icon_add.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="MyClass", icon_type="Class / Subclass", png_path=png,
    )
    assert result.family is icon_add.IconFamily.CLASS
    base = data_root / "Public" / "TestMod"
    expected = [
        base / "Assets" / "Textures" / "Icons" / "ClassIcons" / "MyClass.dds",
        base / "Assets" / "Textures" / "Icons" / "ClassIcons" / "hotbar" / "MyClass.dds",
        base / "AssetsLowRes" / "Textures" / "Icons" / "ClassIcons" / "MyClass.dds",
        base / "AssetsLowRes" / "Textures" / "Icons" / "ClassIcons" / "hotbar" / "MyClass.dds",
    ]
    for p in expected:
        assert p.exists() and _is_readable_dds(p), p
    # Hotbar copies are smaller than standard.
    std = Image.open(expected[0]).size
    hot = Image.open(expected[1]).size
    assert hot[0] < std[0]
    # No atlas / UV map for classes.
    assert not (base / "Assets" / "Textures" / "Icons" / "Icons_TestMod.dds").exists()
    assert not (base / "GUI" / "Icons_TestMod.lsx").exists()


# --- CC family --------------------------------------------------------------


def test_race_icon_creates_cc_files(tmp_path):
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png", 600)
    result = icon_add.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="MyRace", icon_type="Race", png_path=png,
    )
    assert result.family is icon_add.IconFamily.CC
    base = data_root / "Public" / "TestMod"
    full = base / "Assets" / "Textures" / "Icons" / "CC" / "icons_races" / "MyRace.dds"
    lowres = base / "AssetsLowRes" / "Textures" / "Icons" / "CC" / "icons_races" / "MyRace.dds"
    assert full.exists() and _is_readable_dds(full)
    assert lowres.exists() and _is_readable_dds(lowres)
    assert Image.open(full).size == (500, 500)


def test_background_and_god_use_distinct_subfolders(tmp_path):
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png", 600)
    icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                      icon_name="BgIcon", icon_type="Background", png_path=png)
    icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                      icon_name="GodIcon", icon_type="God / Deity", png_path=png)
    base = data_root / "Public" / "TestMod" / "Assets" / "Textures" / "Icons" / "CC"
    assert (base / "icons_backgrounds" / "BgIcon.dds").exists()
    assert (base / "icons_deities" / "GodIcon.dds").exists()


# --- validation -------------------------------------------------------------


def test_rejects_empty_icon_name(tmp_path):
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png")
    with pytest.raises(icon_add.IconAddError, match="empty"):
        icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                          icon_name="   ", icon_type="Spell / Skill", png_path=png)


def test_rejects_icon_name_with_spaces_or_special_chars(tmp_path):
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png")
    for bad in ["my icon", "icon/slash", "icon:colon", "icon*star"]:
        with pytest.raises(icon_add.IconAddError):
            icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                              icon_name=bad, icon_type="Spell / Skill", png_path=png)


def test_rejects_unknown_icon_type(tmp_path):
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png")
    with pytest.raises(icon_add.IconAddError, match="Unknown icon type"):
        icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                          icon_name="X", icon_type="Vehicle", png_path=png)


def test_rejects_missing_png(tmp_path):
    data_root = _mod_skeleton(tmp_path)
    with pytest.raises(icon_add.IconAddError, match="not found"):
        icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                          icon_name="X", icon_type="Spell / Skill",
                          png_path=tmp_path / "nope.png")


def test_small_source_png_warns_but_succeeds(tmp_path):
    """A sub-380px source still works, with a note about upscaling."""
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "small.png", 64)
    result = icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                               icon_name="Smol", icon_type="Spell / Skill",
                               png_path=png)
    assert any("upscaled" in n for n in result.notes)


def _iter(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)
