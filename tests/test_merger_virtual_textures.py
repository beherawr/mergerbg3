"""Tests for the four fixes that make merging mods with virtual textures
work end-to-end. See the matching changes in core/project.py and
core/merger.py:

  1. New VIRTUAL_TEXTURE_BANK category for [PAK]_VirtualTextures/_merged.{lsx,lsf,lsf.lsx}.
  2. VTB participates in structured-LSX merge so two mods' banks union.
  3. Binary .lsf in structured-merge categories now round-trips through
     divine so its mod-folder Path attributes get rewritten on rename.
  4. New VIRTUAL_TEXTURE_ASSET category for .gts/.gtp tilesets, so two
     mods' identically-named files both get preserved (rename-on-collide
     instead of silently dropping B's copy).

These tests don't require a real divine.exe: where divine is needed,
they wire up a FakeDivine that mimics the lsf↔lsx round-trip by reading
and writing LSX text (treating LSX text as if it were LSF binary for
the purposes of round-trip testing).
"""
from __future__ import annotations

from pathlib import Path

from core import merger, meta as _meta
from core.project import Project, FileCategory, _categorize


# ---------------------------------------------------------------------------
# Categorization tests (no merger run required)
# ---------------------------------------------------------------------------


def test_vtb_merged_lsf_categorized_as_virtual_texture_bank(tmp_path):
    """`Public/<mod>/Content/[PAK]_VirtualTextures/_merged.lsf` should be
    a VIRTUAL_TEXTURE_BANK, not a generic BANK_LSF. Otherwise the
    structural-merge path and binary round-trip won't engage."""
    mod_folder = "TestMod"
    root = tmp_path
    file_path = (root / "Public" / mod_folder / "Content"
                 / "[PAK]_VirtualTextures" / "_merged.lsf")
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"")
    cf = _categorize(file_path, root, mod_folder)
    assert cf.category == FileCategory.VIRTUAL_TEXTURE_BANK


def test_vtb_text_form_also_categorized(tmp_path):
    """Both the .lsf binary and .lsf.lsx text fallback should classify
    as VTB so the merger can use either as the source."""
    mod_folder = "TestMod"
    root = tmp_path
    for name in ("_merged.lsx", "_merged.lsf.lsx"):
        file_path = (root / "Public" / mod_folder / "Content"
                     / "[PAK]_VirtualTextures" / name)
        if file_path.exists():
            file_path.unlink()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("<save/>")
        cf = _categorize(file_path, root, mod_folder)
        assert cf.category == FileCategory.VIRTUAL_TEXTURE_BANK, name


def test_gts_categorized_as_virtual_texture_asset(tmp_path):
    """`.gts` / `.gtp` files under Assets/VirtualTextures should be
    VIRTUAL_TEXTURE_ASSET, putting them in the rename-on-collide set."""
    mod_folder = "TestMod"
    root = tmp_path
    for ext in (".gts", ".gtp"):
        file_path = (root / "Public" / mod_folder / "Assets"
                     / "VirtualTextures" / f"newTileset{ext}")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"\x00" * 100)
        cf = _categorize(file_path, root, mod_folder)
        assert cf.category == FileCategory.VIRTUAL_TEXTURE_ASSET, ext


def test_other_paks_unaffected_still_bank_lsf(tmp_path):
    """The VTB-specific rule mustn't accidentally swallow other PAK
    folders' _merged.lsf — those should still classify as BANK_LSF."""
    mod_folder = "TestMod"
    root = tmp_path
    file_path = (root / "Public" / mod_folder / "Content"
                 / "[PAK]_MaterialBank" / "_merged.lsf")
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"")
    cf = _categorize(file_path, root, mod_folder)
    assert cf.category == FileCategory.BANK_LSF


# ---------------------------------------------------------------------------
# Helpers for full-merge tests
# ---------------------------------------------------------------------------


def _make_vt_project(tmp_path: Path, name: str, gts_filename: str = "newTileset.gts") -> Path:
    """Build a minimal Toolkit project that includes a virtual texture:
    one tileset binary plus a VirtualTextureBank entry that references
    it by full ``Public/<mod>/Assets/VirtualTextures/<name>`` path."""
    uuid = _meta.generate_uuid()
    folder = f"{name}_{uuid}"
    root = tmp_path / name
    root.mkdir()

    mod_path = root / "Mods" / folder
    mod_path.mkdir(parents=True)
    mm = _meta.ModMeta(uuid=uuid, folder=folder, name=name, author="test")
    _meta.write_mod_meta_file(mm, mod_path / "meta.lsx")

    # Tileset binary - opaque, just needs to exist. Include the mod
    # name so two mods' "same-named" files have DIFFERENT bytes (else
    # the merger correctly dedupes them and the collision-rename path
    # never triggers).
    vt_dir = root / "Public" / folder / "Assets" / "VirtualTextures"
    vt_dir.mkdir(parents=True)
    (vt_dir / gts_filename).write_bytes(
        b"GTS\x00" + name.encode() + b"\x00" * 100
    )
    (vt_dir / gts_filename.replace(".gts", ".gtp")).write_bytes(
        b"GTP\x00" + name.encode() + b"\x00" * 50
    )

    # VirtualTextureBank LSX with one entry whose Path attribute uses
    # the full mod-folder-qualified form. This is the string the
    # merger needs to remap on mod-folder rename - if it doesn't,
    # the bank still points at the OLD mod folder after the merge.
    gtex_uuid = _meta.generate_uuid()
    vtb_dir = root / "Public" / folder / "Content" / "[PAK]_VirtualTextures"
    vtb_dir.mkdir(parents=True)
    (vtb_dir / "_merged.lsf.lsx").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<save>
    <version major="4" minor="8" revision="0" build="400"/>
    <region id="VirtualTextureBank">
        <node id="VirtualTextureBank">
            <children>
                <node id="Resource">
                    <attribute id="GTexFileName" type="FixedString" value="{gtex_uuid}"/>
                    <attribute id="Name" type="LSString" value="{name}_Sample_VT"/>
                    <attribute id="Path" type="string" value="Public/{folder}/Assets/VirtualTextures/{gts_filename}"/>
                    <attribute id="UUID" type="FixedString" value="{_meta.generate_uuid()}"/>
                </node>
            </children>
        </node>
    </region>
</save>
""", encoding="utf-8")
    return root


class FakeDivine:
    """Stand-in for the real Divine wrapper. Treats LSX text and binary
    LSF as the same bytes - good enough to verify the round-trip path
    is being taken and the LSX-side remap happens.

    Real divine reads a real LSF and produces real LSX; here we just
    copy the bytes. Since the merger writes LSF→LSX→remap→LSX→LSF, the
    'binary' form in tests stays parseable as XML, which lets the tests
    inspect the resulting paths directly."""
    def __init__(self, *_a, **_kw): pass

    def lsx_to_lsf(self, src, dst):
        Path(dst).write_bytes(Path(src).read_bytes())

    def lsf_to_lsx(self, src, dst):
        Path(dst).write_bytes(Path(src).read_bytes())

    def loca_to_xml(self, src, dst):
        Path(dst).write_bytes(Path(src).read_bytes())

    def xml_to_loca(self, src, dst):
        Path(dst).write_bytes(Path(src).read_bytes())


# ---------------------------------------------------------------------------
# Binary LSF round-trip remap: the main bug-fix test
# ---------------------------------------------------------------------------


def test_virtual_texture_bank_paths_remapped_to_new_mod_folder(tmp_path):
    """The user-reported bug: virtual textures appear black after the
    merge because the VTB binary's Path attributes still reference the
    pre-merge mod folder names. With divine configured, the round-trip
    should rewrite those paths to the new merged-mod folder."""
    a_root = _make_vt_project(tmp_path, "ModA")
    b_root = _make_vt_project(tmp_path, "ModB", gts_filename="otherTileset.gts")

    # Rename A's VTB to .lsf so it goes through the binary round-trip
    # path (the bug only manifests on binary, since text gets remapped
    # via lsx.rewrite directly).
    a_vtb = next((a_root / "Public").rglob("_merged.lsf.lsx"))
    a_vtb.rename(a_vtb.with_suffix("").with_suffix(".lsf"))

    new_folder = "MergedVT"
    config = merger.MergeConfig(
        inputs=[Project.load(a_root), Project.load(b_root)],
        output_dir=tmp_path / "out",
        new_uuid=_meta.generate_uuid(),
        new_folder=new_folder,
        new_name="MergedVT",
        conflict_policy="skip",
        divine=FakeDivine(),
    )
    merger.merge(config)

    # The output VTB should reference the NEW mod folder, not ModA's
    # original folder.
    out_vtb = (config.output_dir / "Public" / new_folder
               / "Content" / "[PAK]_VirtualTextures" / "_merged.lsf")
    if not out_vtb.exists():
        # The merger may have written the text form depending on which
        # input won; either way one of them should be present.
        out_vtb = out_vtb.with_suffix(".lsf.lsx")
    assert out_vtb.exists(), "VTB not emitted to output"

    body = out_vtb.read_text(encoding="utf-8", errors="replace")

    # Parse the Path attribute values so we check the actual references,
    # not unrelated content. (Earlier versions of this test scanned for
    # any 'ModA_' substring, which falsely matched the human-readable
    # Name attribute 'ModA_Sample_VT' even though the Path itself had
    # been correctly remapped.)
    import re
    path_values = re.findall(r'attribute id="Path"[^>]*value="([^"]+)"', body)
    assert path_values, "VTB has no Path attribute - test fixture wrong"
    for p in path_values:
        # The remapped Path should use the merged mod's folder.
        assert p.startswith(f"Public/{new_folder}/"), \
            f"Path attribute not remapped: {p}"
        # And must not still reference either input mod's folder.
        assert "ModA_" not in p, f"Path still references ModA's folder: {p}"
        assert "ModB_" not in p, f"Path still references ModB's folder: {p}"


def test_vtb_warns_when_divine_missing(tmp_path):
    """When divine isn't configured and a binary VTB needs to be
    emitted, the merger should fall back to verbatim copy AND record
    a clear note explaining why the textures will render black."""
    a_root = _make_vt_project(tmp_path, "ModA")
    a_vtb = next((a_root / "Public").rglob("_merged.lsf.lsx"))
    a_vtb.rename(a_vtb.with_suffix("").with_suffix(".lsf"))

    b_root = _make_vt_project(tmp_path, "ModB", gts_filename="b.gts")

    config = merger.MergeConfig(
        inputs=[Project.load(a_root), Project.load(b_root)],
        output_dir=tmp_path / "out",
        new_uuid=_meta.generate_uuid(),
        new_folder="MergedVT",
        new_name="MergedVT",
        conflict_policy="skip",
        divine=None,  # the trigger
    )
    result = merger.merge(config)

    # A warning note mentions VirtualTextureBank.
    notes = " ".join(result.notes if hasattr(result, "notes") else [])
    if not notes:
        # The merger surfaces notes via FileEmission.note or
        # result.notes depending on version; try both shapes.
        notes = " ".join(n for n in getattr(result, "notes", []))
    # Some merge result types use a different field; check the union.
    all_text = notes + " ".join(getattr(e, "note", "") or "" for e in
                                getattr(result, "emissions", []))
    assert "VirtualTextureBank" in all_text or "virtual" in all_text.lower()


# ---------------------------------------------------------------------------
# .gts collision: rename-on-collide should preserve both
# ---------------------------------------------------------------------------


def test_two_mods_with_same_gts_filename_both_preserved(tmp_path):
    """Two mods that each ship 'newTileset.gts' should both end up in
    the merged output (one renamed), not silently lose B's copy."""
    a_root = _make_vt_project(tmp_path, "ModA", gts_filename="newTileset.gts")
    b_root = _make_vt_project(tmp_path, "ModB", gts_filename="newTileset.gts")

    config = merger.MergeConfig(
        inputs=[Project.load(a_root), Project.load(b_root)],
        output_dir=tmp_path / "out",
        new_uuid=_meta.generate_uuid(),
        new_folder="MergedGTS",
        new_name="MergedGTS",
        conflict_policy="skip",
        divine=FakeDivine(),
    )
    merger.merge(config)

    vt_dir = (config.output_dir / "Public" / "MergedGTS"
              / "Assets" / "VirtualTextures")
    gts_files = sorted(p.name for p in vt_dir.glob("*.gts"))
    # Original + at least one renamed copy.
    assert len(gts_files) >= 2, \
        f"Expected both mods' .gts files to be preserved, got: {gts_files}"
    # The original name still exists.
    assert "newTileset.gts" in gts_files
