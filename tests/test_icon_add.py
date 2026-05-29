"""Tests for core.icon_add.

Asserts against the file layout established by inspecting a real,
in-game-working mod (BloodFang/Class_RogueKira). Where the layout
differs from public tutorials, the tests follow the working mod.

Self-contained: builds a tiny mod skeleton under tmp_path and a
synthetic source PNG; runs without the private fixture mods.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from core import icon_add
from core import lsx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_png(path: Path, size: int = 512, color=(200, 120, 40, 255)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (size, size), color)
    for i in range(min(size, size)):
        img.putpixel((i, i), (255, 255, 255, 255))
    img.save(path)
    return path


def _mod_skeleton(tmp_path: Path, mod_folder: str = "TestMod") -> Path:
    """Minimal Mods/<mod>/ and Public/<mod>/ trees; return data_root."""
    data_root = tmp_path / "ws"
    (data_root / "Mods" / mod_folder).mkdir(parents=True)
    (data_root / "Public" / mod_folder).mkdir(parents=True)
    return data_root


def _is_readable_dds(path: Path) -> bool:
    try:
        im = Image.open(path)
        im.load()
        return im.width > 0 and im.height > 0
    except Exception:
        return False


def _read_metadata_keys(data_root: Path, mod_folder: str) -> set[str]:
    """All MapKeys registered in the text-form metadata.lsf.lsx."""
    p = data_root / "Mods" / mod_folder / "GUI" / "metadata.lsf.lsx"
    if not p.exists():
        return set()
    doc = lsx.parse_file(p)
    region = doc.region("config")
    if region is None:
        return set()
    container = next(c for c in region.root_node.children if c.id == "entries")
    return {c.attr_value("MapKey") for c in container.children if c.id == "Object"}


def _read_metadata_entry(
    data_root: Path, mod_folder: str, map_key: str,
) -> tuple[int, int, int] | None:
    """(w, h, mipcount) for one MapKey, or None if absent."""
    p = data_root / "Mods" / mod_folder / "GUI" / "metadata.lsf.lsx"
    if not p.exists():
        return None
    doc = lsx.parse_file(p)
    region = doc.region("config")
    container = next(c for c in region.root_node.children if c.id == "entries")
    for obj in container.children:
        if obj.id != "Object" or obj.attr_value("MapKey") != map_key:
            continue
        inner = next(c for c in obj.children if c.id == "entries")
        return (
            int(inner.attr_value("w")),
            int(inner.attr_value("h")),
            int(inner.attr_value("mipcount")),
        )
    return None


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def _iter(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


# ---------------------------------------------------------------------------
# ATLAS family
# ---------------------------------------------------------------------------


class TestAtlasFamily:
    def test_spell_writes_tooltip_under_mods_not_public(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MySpell", icon_type="Spell / Skill", png_path=png,
        )
        base = data_root / "Mods" / "TestMod" / "GUI"
        tooltip = base / "Assets" / "Tooltips" / "Icons" / "MySpell.DDS"
        tooltip_lowres = base / "AssetsLowRes" / "Tooltips" / "Icons" / "MySpell.DDS"
        assert tooltip.exists() and _is_readable_dds(tooltip)
        assert tooltip_lowres.exists() and _is_readable_dds(tooltip_lowres)
        # Cross-checked against nightb/mysticw: tooltip Assets is 380x380,
        # AssetsLowRes is the half-resolution version at 192x192 (the
        # exact size both real mods use, not the simple 380/2=190).
        assert Image.open(tooltip).size == (380, 380)
        assert Image.open(tooltip_lowres).size == (192, 192)
        # NOT in the old Public location.
        assert not (data_root / "Public" / "TestMod" / "GUI" / "Assets"
                    / "Tooltips" / "Icons" / "MySpell.DDS").exists()

    def test_assets_lowres_is_half_resolution_for_atlas(self, tmp_path):
        """Cross-checked against nightb (380→192) and mysticw (380→192,
        144→72): AssetsLowRes files are genuine lower-resolution copies
        at roughly half the Assets dimensions, NOT byte-identical."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MySpell", icon_type="Spell / Skill", png_path=png,
        )
        base = data_root / "Mods" / "TestMod" / "GUI"
        full = base / "Assets" / "Tooltips" / "Icons" / "MySpell.DDS"
        lowres = base / "AssetsLowRes" / "Tooltips" / "Icons" / "MySpell.DDS"
        # Different sizes → cannot be byte-identical.
        assert _md5(full) != _md5(lowres)
        assert Image.open(full).size == (380, 380)
        assert Image.open(lowres).size == (192, 192)
        # Same applies to controller: 144 → 72.
        ctl = base / "Assets" / "ControllerUIIcons" / "skills_png" / "MySpell.DDS"
        ctl_lr = base / "AssetsLowRes" / "ControllerUIIcons" / "skills_png" / "MySpell.DDS"
        assert Image.open(ctl).size == (144, 144)
        assert Image.open(ctl_lr).size == (72, 72)

    def test_controller_writes_at_144(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MySpell", icon_type="Spell / Skill", png_path=png,
        )
        ctl = (data_root / "Mods" / "TestMod" / "GUI" / "Assets"
               / "ControllerUIIcons" / "skills_png" / "MySpell.DDS")
        assert ctl.exists() and Image.open(ctl).size == (144, 144)

    def test_item_dual_writes_to_both_tooltip_subfolders(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MySword", icon_type="Item", png_path=png,
        )
        base = data_root / "Mods" / "TestMod" / "GUI"
        assert (base / "Assets" / "Tooltips" / "Icons" / "MySword.DDS").exists()
        assert (base / "Assets" / "Tooltips" / "ItemIcons" / "MySword.DDS").exists()
        assert (base / "AssetsLowRes" / "Tooltips" / "Icons" / "MySword.DDS").exists()
        assert (base / "AssetsLowRes" / "Tooltips" / "ItemIcons" / "MySword.DDS").exists()
        assert (base / "Assets" / "ControllerUIIcons" / "items_png"
                / "MySword.DDS").exists()

    def test_spell_does_not_write_item_tooltips(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MySpell", icon_type="Spell / Skill", png_path=png,
        )
        assert not (data_root / "Mods" / "TestMod" / "GUI" / "Assets"
                    / "Tooltips" / "ItemIcons" / "MySpell.DDS").exists()

    def test_atlas_dds_is_512_with_lowercase_extension(self, tmp_path):
        """Cross-checked against nightb (newAtlas.dds) and mysticw
        (newAtlas.dds + named atlases): atlas DDS is 512x512 with a
        lowercase '.dds' extension, named per the toolkit's default
        of 'newAtlas.dds' (not 'Icons_<mod>.DDS' like we used to emit)."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MySpell", icon_type="Spell / Skill", png_path=png,
        )
        atlas = (data_root / "Public" / "TestMod" / "Assets" / "Textures"
                 / "Icons" / "newAtlas.dds")
        assert atlas.exists() and Image.open(atlas).size == (512, 512)
        # Old path should not exist.
        old = (data_root / "Public" / "TestMod" / "Assets" / "Textures"
               / "Icons" / "Icons_TestMod.DDS")
        assert not old.exists()

    def test_first_atlas_slot_uv_coords_have_half_pixel_inset(self, tmp_path):
        """Cross-checked against nightb (NB438_Atlas.lsx) and mysticw
        (ArcaneVanguardAtlas.lsx): UV coordinates use a half-pixel
        inset of 0.5/512 = 0.0009765625, NOT exact tile boundaries.
        Without the inset, adjacent tiles can bleed into each other at
        smaller mip levels."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
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
        # Slot 0 in a 512x512 / 8x8 grid with half-pixel inset:
        #   U1 = V1 = 0.5/512               = 0.0009765625
        #   U2 = V2 = (64-0.5)/512           = 0.1240234375 (~0.12402344)
        assert abs(float(e.attr_value("U1")) - 0.0009765625) < 1e-9
        assert abs(float(e.attr_value("V1")) - 0.0009765625) < 1e-9
        assert abs(float(e.attr_value("U2")) - 0.1240234375) < 1e-9
        assert abs(float(e.attr_value("V2")) - 0.1240234375) < 1e-9

    def test_atlas_lsx_schema_matches_reference_mods(self, tmp_path):
        """Critical schema requirements cross-checked against nightb and
        mysticw, with each detail mattering for the toolkit to actually
        render the icon:
          - region order: IconUVList first, TextureAtlasInfo second
          - Path attribute uses type='string' (not 'LSString')
          - Height/Width use type='int32' (not 'int64')
          - Path value is MOD-RELATIVE (no 'Public/<mod>/' prefix)
        """
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="X", icon_type="Spell / Skill", png_path=png,
        )
        uv = data_root / "Public" / "TestMod" / "GUI" / "Icons_TestMod.lsx"
        raw = uv.read_text()

        # Region order: IconUVList must come before TextureAtlasInfo.
        i_uv = raw.index('region id="IconUVList"')
        i_info = raw.index('region id="TextureAtlasInfo"')
        assert i_uv < i_info, "IconUVList must precede TextureAtlasInfo"

        # Path attribute type and value.
        doc = lsx.parse_file(uv)
        info = doc.region("TextureAtlasInfo")
        path_node = next(n for n in _iter(info.root_node)
                         if n.id == "TextureAtlasPath")
        path_attr = next(a for a in path_node.attributes if a.id == "Path")
        assert path_attr.type == "string"
        # Mod-relative: no Public/<mod>/ prefix.
        assert path_attr.value == "Assets/Textures/Icons/newAtlas.dds"

        # Size types.
        for size_node_id in ("TextureAtlasIconSize", "TextureAtlasTextureSize"):
            size_node = next(n for n in _iter(info.root_node)
                             if n.id == size_node_id)
            for a in size_node.attributes:
                if a.id in ("Height", "Width"):
                    assert a.type == "int32", \
                        f"{size_node_id}.{a.id} should be int32, got {a.type}"

    def test_atlas_writes_lsf_binary_when_divine_configured(self, tmp_path, monkeypatch):
        """Cross-checked against nightb and mysticw: both ship a binary
        .lsf alongside the .lsx in Public/<mod>/GUI/. Without the .lsf,
        the toolkit can index the icon by name but the game won't render
        its texture at runtime. We require the .lsf when divine is
        configured."""
        from core import icon_add as ia

        # Build a fake "divine" that just copies LSX → LSF so we don't
        # need the real binary to test the write happens.
        fake_exe = tmp_path / "divine.exe"
        fake_exe.write_text("")

        class FakeDivine:
            def __init__(self, *a, **kw): pass
            def lsx_to_lsf(self, src, dst):
                Path(dst).write_bytes(Path(src).read_bytes())
            def lsf_to_lsx(self, src, dst):
                Path(dst).write_bytes(Path(src).read_bytes())
        monkeypatch.setattr(ia.divine_mod, "Divine", FakeDivine)

        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        ia.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MySpell", icon_type="Spell / Skill", png_path=png,
            divine_path=str(fake_exe),
        )
        gui = data_root / "Public" / "TestMod" / "GUI"
        assert (gui / "Icons_TestMod.lsx").exists()
        assert (gui / "Icons_TestMod.lsf").exists()

    def test_no_mods_side_uv_lsf_or_lsx_lsx_written(self, tmp_path):
        """Neither nightb nor mysticw has any Mods-side UV map file.
        The old code wrote Mods/<mod>/GUI/Icons_<mod>.lsf.lsx; we now
        don't, since real mods don't."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="X", icon_type="Spell / Skill", png_path=png,
        )
        mods_gui = data_root / "Mods" / "TestMod" / "GUI"
        for stale in [
            mods_gui / "Icons_TestMod.lsf",
            mods_gui / "Icons_TestMod.lsf.lsx",
            mods_gui / "Simple_Icons.lsf",
            mods_gui / "Simple_Icons.lsf.lsx",
        ]:
            assert not stale.exists(), f"old/wrong file should not exist: {stale}"

    def test_no_texturebank_written(self, tmp_path):
        """Neither nightb nor mysticw has a TextureBank _merged.lsf.lsx.
        The old code wrote one; we now don't."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="X", icon_type="Spell / Skill", png_path=png,
        )
        tb = (data_root / "Public" / "TestMod" / "Content" / "UI"
              / "[PAK]_UI" / "_merged.lsf.lsx")
        assert not tb.exists()

    def test_second_icon_appends_to_atlas(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png")
        icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                          icon_name="IconA", icon_type="Spell / Skill", png_path=png)
        r2 = icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                               icon_name="IconB", icon_type="Spell / Skill", png_path=png)
        assert r2.slot_index == 1
        uv = data_root / "Public" / "TestMod" / "GUI" / "Icons_TestMod.lsx"
        doc = lsx.parse_file(uv)
        names = {n.attr_value("MapKey") for n in _iter(doc.region("IconUVList").root_node) if n.id == "IconUV"}
        assert names == {"IconA", "IconB"}

    def test_readd_same_name_reuses_slot(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png")
        icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                          icon_name="Dup", icon_type="Spell / Skill", png_path=png)
        r2 = icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                               icon_name="Dup", icon_type="Spell / Skill", png_path=png)
        assert r2.slot_index == 0

    def test_65th_icon_overflows_into_second_atlas(self, tmp_path):
        """The 8x8 grid fits 64 icons. The 65th should land in a new
        atlas: newAtlas_2.dds + Icons_<mod>_2.lsx, with the icon at
        slot 0 of the new atlas. Mirrors mysticw's pattern of shipping
        multiple atlases (newAtlas.dds + SecondAVAtlas.dds + ...)."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 100)
        # Fill the first atlas.
        for i in range(64):
            icon_add.add_icon(
                data_root=data_root, mod_folder="TestMod",
                icon_name=f"Icon{i:02d}", icon_type="Spell / Skill", png_path=png,
            )
        # 65th icon: should overflow.
        result = icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="Overflow", icon_type="Spell / Skill", png_path=png,
        )
        assert result.slot_index == 0  # First slot of the new atlas
        # New atlas DDS exists.
        atlas2 = (data_root / "Public" / "TestMod" / "Assets" / "Textures"
                  / "Icons" / "newAtlas_2.dds")
        assert atlas2.exists()
        # New atlas LSX exists and has 'Overflow' in it.
        lsx2 = data_root / "Public" / "TestMod" / "GUI" / "Icons_TestMod_2.lsx"
        assert lsx2.exists()
        doc2 = lsx.parse_file(lsx2)
        names = {n.attr_value("MapKey") for n in _iter(doc2.region("IconUVList").root_node) if n.id == "IconUV"}
        assert names == {"Overflow"}
        # First atlas LSX is unchanged (still has 64 icons, no "Overflow").
        lsx1 = data_root / "Public" / "TestMod" / "GUI" / "Icons_TestMod.lsx"
        doc1 = lsx.parse_file(lsx1)
        names1 = {n.attr_value("MapKey") for n in _iter(doc1.region("IconUVList").root_node) if n.id == "IconUV"}
        assert len(names1) == 64
        assert "Overflow" not in names1

    def test_overflow_atlas_path_value_uses_overflow_filename(self, tmp_path):
        """The 2nd atlas's LSX should reference 'newAtlas_2.dds', not
        'newAtlas.dds', so its TextureAtlasPath points at the right
        sheet. Otherwise both atlas LSXs would point at the same DDS."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 100)
        for i in range(64):
            icon_add.add_icon(
                data_root=data_root, mod_folder="TestMod",
                icon_name=f"Icon{i:02d}", icon_type="Spell / Skill", png_path=png,
            )
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="Overflow", icon_type="Spell / Skill", png_path=png,
        )
        lsx2 = data_root / "Public" / "TestMod" / "GUI" / "Icons_TestMod_2.lsx"
        doc2 = lsx.parse_file(lsx2)
        info = doc2.region("TextureAtlasInfo")
        path_node = next(n for n in _iter(info.root_node) if n.id == "TextureAtlasPath")
        path_value = next(a.value for a in path_node.attributes if a.id == "Path")
        assert path_value == "Assets/Textures/Icons/newAtlas_2.dds"


# ---------------------------------------------------------------------------
# ATLAS family - metadata.lsf registration
# ---------------------------------------------------------------------------


class TestAtlasMetadata:
    def test_spell_registers_two_keys_assets_only(self, tmp_path):
        """Cross-checked against nightb (96 keys, 0 AssetsLowRes) and
        mysticw (354 keys, 0 AssetsLowRes): only Assets/ paths register
        in metadata.lsf. A spell gets two keys: tooltip + controller."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MySpell", icon_type="Spell / Skill", png_path=png,
        )
        keys = _read_metadata_keys(data_root, "TestMod")
        # Only the Assets/ paths register.
        for k in [
            "Assets/Tooltips/Icons/MySpell.png",
            "Assets/ControllerUIIcons/skills_png/MySpell.png",
        ]:
            assert k in keys, f"missing key: {k}"
        # AssetsLowRes/ keys must NOT be registered.
        for k in [
            "AssetsLowRes/Tooltips/Icons/MySpell.png",
            "AssetsLowRes/ControllerUIIcons/skills_png/MySpell.png",
        ]:
            assert k not in keys, f"unexpected LowRes key registered: {k}"

    def test_item_registers_three_keys_assets_only(self, tmp_path):
        """Item: tooltip Icons + tooltip ItemIcons + controller items_png
        all under Assets/. No AssetsLowRes entries."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MySword", icon_type="Item", png_path=png,
        )
        keys = _read_metadata_keys(data_root, "TestMod")
        expected = {
            "Assets/Tooltips/Icons/MySword.png",
            "Assets/Tooltips/ItemIcons/MySword.png",
            "Assets/ControllerUIIcons/items_png/MySword.png",
        }
        assert expected.issubset(keys)
        # And no LowRes counterparts.
        for k in list(keys):
            assert not k.startswith("AssetsLowRes/"), f"unexpected LowRes key: {k}"

    def test_metadata_entry_records_correct_dims(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="X", icon_type="Spell / Skill", png_path=png,
        )
        # Assets entry records the full-resolution dimensions.
        assert _read_metadata_entry(data_root, "TestMod",
                                    "Assets/Tooltips/Icons/X.png") == (380, 380, 1)
        assert _read_metadata_entry(data_root, "TestMod",
                                    "Assets/ControllerUIIcons/skills_png/X.png") == (144, 144, 1)


# ---------------------------------------------------------------------------
# CLASS family - 300x300, uppercase .DDS, four byte-identical files
# ---------------------------------------------------------------------------


class TestClassFamily:
    def test_writes_four_files_at_nightb_sizes(self, tmp_path):
        """Cross-checked against nightb (NIGHTBRINGER_NB438.DDS):
          Assets/ClassIcons/<n>.DDS              300x300
          Assets/ClassIcons/hotbar/<n>.DDS       144x144
          AssetsLowRes/ClassIcons/<n>.DDS        152x152
          AssetsLowRes/ClassIcons/hotbar/<n>.DDS  72x72
        Note: hotbar is NOT the same size as standard - the
        Class_RogueKira byte-identical pattern was author-specific."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 512)
        result = icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MyClass", icon_type="Class / Subclass", png_path=png,
        )
        assert result.family is icon_add.IconFamily.CLASS
        gui = data_root / "Mods" / "TestMod" / "GUI"
        expected = {
            gui / "Assets" / "ClassIcons" / "MyClass.DDS": (300, 300),
            gui / "Assets" / "ClassIcons" / "hotbar" / "MyClass.DDS": (144, 144),
            gui / "AssetsLowRes" / "ClassIcons" / "MyClass.DDS": (152, 152),
            gui / "AssetsLowRes" / "ClassIcons" / "hotbar" / "MyClass.DDS": (72, 72),
        }
        for path, size in expected.items():
            assert path.exists() and Image.open(path).size == size, \
                f"{path}: expected {size}, got {Image.open(path).size if path.exists() else 'MISSING'}"

    def test_extension_is_uppercase(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 512)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MyClass", icon_type="Class / Subclass", png_path=png,
        )
        gui = data_root / "Mods" / "TestMod" / "GUI"
        for p in (gui / "Assets" / "ClassIcons").iterdir():
            if p.is_file():
                assert p.suffix == ".DDS"

    def test_registers_two_metadata_entries_assets_only(self, tmp_path):
        """Cross-checked against nightb: only the two Assets/ paths
        register, at their respective full-resolution sizes. The
        AssetsLowRes/ files exist on disk but are NOT in metadata.lsf."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 512)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MyClass", icon_type="Class / Subclass", png_path=png,
        )
        keys = _read_metadata_keys(data_root, "TestMod")
        # Both Assets/ paths registered.
        assert "Assets/ClassIcons/MyClass.png" in keys
        assert "Assets/ClassIcons/hotbar/MyClass.png" in keys
        # Sizes match each file's actual resolution.
        assert _read_metadata_entry(data_root, "TestMod",
                                    "Assets/ClassIcons/MyClass.png") == (300, 300, 1)
        assert _read_metadata_entry(data_root, "TestMod",
                                    "Assets/ClassIcons/hotbar/MyClass.png") == (144, 144, 1)
        # No AssetsLowRes keys.
        for k in keys:
            assert not k.startswith("AssetsLowRes/"), f"unexpected LowRes key: {k}"


# ---------------------------------------------------------------------------
# ACTION_RESOURCE family
# ---------------------------------------------------------------------------


class TestActionResourceFamily:
    def test_creates_full_set_at_correct_sizes(self, tmp_path):
        """Per cross-checked convention: Assets at full size,
        AssetsLowRes at half. Half is by integer division: 80→40,
        44×64→22×32, 48→24, 128→64."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 256)
        result = icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="ComboPoints", icon_type="Action Resource", png_path=png,
        )
        assert result.family is icon_add.IconFamily.ACTION_RESOURCE
        gui = data_root / "Mods" / "TestMod" / "GUI"

        expected_sizes = {
            # Assets - full size
            gui / "Assets" / "ActionResources_c" / "Icons" / "ComboPoints.DDS": (80, 80),
            gui / "Assets" / "ActionResources_c" / "Icons" / "Resources" / "ComboPoints.DDS": (44, 64),
            gui / "Assets" / "ActionResources_c" / "Icons" / "Resources" / "Highlight" / "ComboPoints.DDS": (44, 64),
            gui / "Assets" / "ActionResources_c" / "Icons" / "Resources" / "Missing" / "ComboPoints.DDS": (44, 64),
            gui / "Assets" / "ActionResources_c" / "Icons" / "Resources" / "Used" / "ComboPoints.DDS": (44, 64),
            gui / "Assets" / "Shared" / "Resources" / "ComboPoints.DDS": (48, 48),
            gui / "Assets" / "Shared" / "Resources" / "Highlight" / "ComboPoints.DDS": (48, 48),
            gui / "Assets" / "CC" / "icons_resources" / "ComboPoints.DDS": (128, 128),
            # AssetsLowRes - half resolution
            gui / "AssetsLowRes" / "ActionResources_c" / "Icons" / "ComboPoints.DDS": (40, 40),
            gui / "AssetsLowRes" / "ActionResources_c" / "Icons" / "Resources" / "Used" / "ComboPoints.DDS": (22, 32),
            gui / "AssetsLowRes" / "Shared" / "Resources" / "ComboPoints.DDS": (24, 24),
            gui / "AssetsLowRes" / "CC" / "icons_resources" / "ComboPoints.DDS": (64, 64),
        }
        for path, size in expected_sizes.items():
            assert path.exists() and _is_readable_dds(path), path
            assert Image.open(path).size == size, f"{path}: expected {size}"

    def test_state_variants_are_44x64_not_square(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 256)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="ComboPoints", icon_type="Action Resource", png_path=png,
        )
        state = (data_root / "Mods" / "TestMod" / "GUI" / "Assets"
                 / "ActionResources_c" / "Icons" / "Resources" / "Used"
                 / "ComboPoints.DDS")
        assert Image.open(state).size == (44, 64)

    def test_registers_assets_keys_only(self, tmp_path):
        """Only Assets/ paths in metadata.lsf; AssetsLowRes files exist
        on disk but aren't registered."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 256)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="ComboPoints", icon_type="Action Resource", png_path=png,
        )
        keys = _read_metadata_keys(data_root, "TestMod")
        # All four AR subtrees registered.
        for k in [
            "Assets/ActionResources_c/Icons/ComboPoints.png",
            "Assets/ActionResources_c/Icons/Resources/Highlight/ComboPoints.png",
            "Assets/Shared/Resources/Missing/ComboPoints.png",
            "Assets/CC/icons_resources/ComboPoints.png",
        ]:
            assert k in keys, f"missing key: {k}"
        # No AssetsLowRes keys.
        for k in keys:
            assert not k.startswith("AssetsLowRes/"), f"unexpected LowRes key: {k}"


# ---------------------------------------------------------------------------
# PORTRAIT family
# ---------------------------------------------------------------------------


class TestPortraitFamily:
    def test_writes_two_distinct_sizes(self, tmp_path):
        """The ONE family where AssetsLowRes is genuinely smaller."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 512)
        result = icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="MyPortrait", icon_type="Portrait", png_path=png,
        )
        assert result.family is icon_add.IconFamily.PORTRAIT
        base = data_root / "Mods" / "TestMod" / "GUI"
        assert Image.open(base / "Assets" / "Portraits" / "MyPortrait.DDS").size == (152, 152)
        assert Image.open(base / "AssetsLowRes" / "Portraits" / "MyPortrait.DDS").size == (76, 76)

    def test_accepts_guid_prefixed_override_filename(self, tmp_path):
        data_root = _mod_skeleton(tmp_path, "GustavDev")
        png = _make_png(tmp_path / "src.png", 200)
        name = "eb90eea1-afd3-4ad5-c6ee-79f26fcb8c26-(Icon_Human_Female_Strong)"
        icon_add.add_icon(
            data_root=data_root, mod_folder="GustavDev",
            icon_name=name, icon_type="Portrait", png_path=png,
        )
        assert (data_root / "Mods" / "GustavDev" / "GUI" / "Assets"
                / "Portraits" / f"{name}.DDS").exists()

    def test_metadata_records_assets_only(self, tmp_path):
        """Portrait registers ONLY the Assets/Portraits path in
        metadata.lsf. The 76x76 AssetsLowRes file exists on disk but
        isn't registered - matches third-party convention and Fade's
        original example metadata."""
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 200)
        icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="Hero", icon_type="Portrait", png_path=png,
        )
        assert _read_metadata_entry(data_root, "TestMod",
                                    "Assets/Portraits/Hero.png") == (152, 152, 1)
        # AssetsLowRes not registered.
        assert _read_metadata_entry(data_root, "TestMod",
                                    "AssetsLowRes/Portraits/Hero.png") is None


# ---------------------------------------------------------------------------
# Cross-family metadata accumulation
# ---------------------------------------------------------------------------


class TestMetadataAccumulation:
    def test_three_icons_share_one_metadata(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                          icon_name="MySpell", icon_type="Spell / Skill", png_path=png)
        icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                          icon_name="MyClass", icon_type="Class / Subclass", png_path=png)
        icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                          icon_name="MyPortrait", icon_type="Portrait", png_path=png)
        keys = _read_metadata_keys(data_root, "TestMod")
        assert any("MySpell" in k for k in keys)
        assert any("MyClass" in k for k in keys)
        assert any("MyPortrait" in k for k in keys)

    def test_readd_does_not_duplicate_metadata_entries(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png", 400)
        icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                          icon_name="X", icon_type="Spell / Skill", png_path=png)
        icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                          icon_name="X", icon_type="Spell / Skill", png_path=png)
        keys = _read_metadata_keys(data_root, "TestMod")
        # A spell registers two Assets/ keys: tooltip + controller.
        # Re-adding should replace, not append (still 2, not 4).
        related = {k for k in keys if "/X.png" in k}
        assert len(related) == 2


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_rejects_empty_name(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png")
        with pytest.raises(icon_add.IconAddError, match="empty"):
            icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                              icon_name="   ", icon_type="Spell / Skill", png_path=png)

    def test_rejects_path_breaking_chars(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png")
        for bad in ["my icon", "icon/slash", "icon:colon", "icon*star"]:
            with pytest.raises(icon_add.IconAddError):
                icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                                  icon_name=bad, icon_type="Spell / Skill", png_path=png)

    def test_rejects_unknown_type(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "src.png")
        with pytest.raises(icon_add.IconAddError, match="Unknown icon type"):
            icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                              icon_name="X", icon_type="Vehicle", png_path=png)

    def test_rejects_missing_png(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        with pytest.raises(icon_add.IconAddError, match="not found"):
            icon_add.add_icon(data_root=data_root, mod_folder="TestMod",
                              icon_name="X", icon_type="Spell / Skill",
                              png_path=tmp_path / "nope.png")

    def test_small_png_warns_but_succeeds(self, tmp_path):
        data_root = _mod_skeleton(tmp_path)
        png = _make_png(tmp_path / "small.png", 64)
        result = icon_add.add_icon(
            data_root=data_root, mod_folder="TestMod",
            icon_name="Smol", icon_type="Spell / Skill", png_path=png,
        )
        assert any("upscaled" in n for n in result.notes)


# ---------------------------------------------------------------------------
# Binary .lsf path when divine.exe is available
# ---------------------------------------------------------------------------


def test_uses_binary_lsf_when_divine_available(tmp_path, monkeypatch):
    """When divine is configured, metadata.lsf is written as real
    binary and the .lsf.lsx text fallback is cleaned up."""
    from core import icon_add as ia
    fake_exe = tmp_path / "divine.exe"
    fake_exe.write_text("")

    class FakeDivine:
        def __init__(self, *args, **kwargs):
            pass
        def lsx_to_lsf(self, src, dst):
            Path(dst).write_bytes(Path(src).read_bytes())
        def lsf_to_lsx(self, src, dst):
            Path(dst).write_bytes(Path(src).read_bytes())

    monkeypatch.setattr(ia.divine_mod, "Divine", FakeDivine)

    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png", 200)
    ia.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="Hero", icon_type="Portrait", png_path=png,
        divine_path=str(fake_exe),
    )
    meta_lsf = data_root / "Mods" / "TestMod" / "GUI" / "metadata.lsf"
    meta_lsx = data_root / "Mods" / "TestMod" / "GUI" / "metadata.lsf.lsx"
    assert meta_lsf.exists()
    assert not meta_lsx.exists()


# ---------------------------------------------------------------------------
# Helpful fallback note when divine_path is misconfigured
# ---------------------------------------------------------------------------


def test_fallback_note_when_divine_path_is_invalid(tmp_path):
    """Regression: user reported getting 'divine.exe not configured' even
    though they HAD configured divine. The cause was a stale or quoted
    path. The note now distinguishes the two cases so the user can see
    which problem they have."""
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png", 200)
    nonexistent = str(tmp_path / "actually_missing.exe")
    result = icon_add.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="X", icon_type="Spell / Skill", png_path=png,
        divine_path=nonexistent,
    )
    note = " ".join(result.notes)
    # New message names the path so the user can see what didn't resolve.
    assert nonexistent in note
    # Doesn't claim they didn't configure it.
    assert "not configured" not in note


def test_fallback_note_when_no_divine_path_given(tmp_path):
    """When divine_path is None (user genuinely hasn't configured it),
    the old message is still appropriate."""
    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png", 200)
    result = icon_add.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="X", icon_type="Spell / Skill", png_path=png,
        divine_path=None,
    )
    note = " ".join(result.notes)
    assert "not configured" in note


def test_quoted_divine_path_is_accepted(tmp_path, monkeypatch):
    """Windows 'Copy as path' wraps paths in quotes. find_divine now
    strips surrounding quotes, so a path passed in that form should
    still let divine run (here we monkeypatch divine to skip the actual
    binary execution)."""
    from core import icon_add as ia
    fake_exe = tmp_path / "divine.exe"
    fake_exe.write_text("")

    class FakeDivine:
        def __init__(self, *a, **kw): pass
        def lsx_to_lsf(self, src, dst): Path(dst).write_bytes(Path(src).read_bytes())
        def lsf_to_lsx(self, src, dst): Path(dst).write_bytes(Path(src).read_bytes())
    monkeypatch.setattr(ia.divine_mod, "Divine", FakeDivine)

    data_root = _mod_skeleton(tmp_path)
    png = _make_png(tmp_path / "src.png", 200)
    quoted_path = f'"{fake_exe}"'
    result = ia.add_icon(
        data_root=data_root, mod_folder="TestMod",
        icon_name="Hero", icon_type="Portrait", png_path=png,
        divine_path=quoted_path,
    )
    # divine ran successfully → binary .lsf written, no fallback note.
    note = " ".join(result.notes)
    assert "fallback" not in note
    assert (data_root / "Mods" / "TestMod" / "GUI" / "metadata.lsf").exists()
