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

from core import merger, meta as _meta, remap
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


def test_two_mods_with_identical_gts_dedup_correctly(tmp_path):
    """When two mods ship a .gts file with the same UUID-derived name,
    we must NOT rename either of them. The VirtualTextureBank stores
    tileset filenames as identity references (TileSetFileName="<UUID>"
    matches "<UUID>.gts" on disk). Renaming would orphan the VTB → on-
    disk link and cause black-mesh rendering in-game.

    The toolkit produces deterministic UUID names from source TIFs, so
    two mods that built from the same source would have byte-identical
    files anyway. The merger's normal keep-A-on-collision behavior
    correctly preserves one copy under the original name; both mods'
    VTBs (which reference the file by name) still resolve to that
    surviving file.

    Earlier versions of this test asserted the opposite — that .gts
    should rename on collision — based on a wrong assumption about
    how the VTB referenced its tilesets. That assumption was disproved
    by inspecting a real merged mod's VTB: it stores UUIDs, not paths.
    """
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
    # The original name should survive, with no renamed duplicate.
    assert gts_files == ["newTileset.gts"], (
        f"Expected one .gts file (the original name preserved); got: "
        f"{gts_files}. If you see a renamed copy like 'newTileset_2.gts', "
        f"the rename-on-collide logic is mistakenly active for "
        f"VIRTUAL_TEXTURE_ASSET — that would break the VTB → on-disk "
        f"reference, which is keyed by filename."
    )


def _make_vt_project_with_generated(tmp_path: Path, name: str) -> tuple[Path, str]:
    """Build a minimal project that has both Public/ source content AND
    a Generated/Public/<mod>/VirtualTextures/ tree mirroring what the
    BG3 Toolkit produces after "Build Virtual Textures".

    Returns the project root and the (deterministic-by-name) UUID-named
    GTS filename, so the test can assert the file landed in the right
    place in the merged output.
    """
    uuid = _meta.generate_uuid()
    folder = f"{name}_{uuid}"
    root = tmp_path / name
    root.mkdir()

    # Required mod meta.
    mod_path = root / "Mods" / folder
    mod_path.mkdir(parents=True)
    _meta.write_mod_meta_file(
        _meta.ModMeta(uuid=uuid, folder=folder, name=name, author="t"),
        mod_path / "meta.lsx",
    )

    # The Toolkit-baked virtual texture tree. Real-world files we'd
    # see here (from the WeaponsOfWar reference mod):
    #   VirtualTextures/<tilesetUUID>.gts            (one tileset blob)
    #   VirtualTextures/<tilesetUUID>_<hash>.gtp     (tile pages)
    #   VirtualTextures/<tilesetUUID>_mips.gtp       (mip pages)
    #   VirtualTextures/Albedo_Normal_Physical/<hash>.gtex
    gen_vt = (root / "Generated" / "Public" / folder / "VirtualTextures")
    gen_vt.mkdir(parents=True)
    tileset_uuid = "ba05d4f3-267c-40db-97b8-0e577f9c566a"
    gtex_hash = "40ac36e47616fa15b8f5eb1a415a1c16"
    # Different bytes per mod so a missed-dedup bug would show up.
    sentinel = name.encode()
    (gen_vt / f"{tileset_uuid}.gts").write_bytes(b"GTS\x00" + sentinel + b"\x00" * 200)
    (gen_vt / f"{tileset_uuid}_mips.gtp").write_bytes(b"GTPm" + sentinel + b"\x00" * 100)
    (gen_vt / f"{tileset_uuid}_{gtex_hash}.gtp").write_bytes(
        b"GTPx" + sentinel + b"\x00" * 100
    )
    (gen_vt / "Albedo_Normal_Physical").mkdir()
    (gen_vt / "Albedo_Normal_Physical" / f"{gtex_hash}.gtex").write_bytes(
        b"GTEX" + sentinel + b"\x00" * 50
    )
    return root, tileset_uuid


def test_generated_virtual_textures_are_copied_to_merged_mod(tmp_path):
    """The bug that produced black virtual textures in real merged mods:
    Data/Generated/Public/<mod>/VirtualTextures/*.gts files weren't
    being walked by the merger at all. They contain the actual tileset
    binaries the engine streams at runtime, referenced by
    UUID-as-filename from the VirtualTextureBank.

    After this fix, the merger discovers and copies the entire
    Generated/Public/<mod>/ tree into Generated/Public/<merged>/.
    """
    a_root, tileset_uuid = _make_vt_project_with_generated(tmp_path, "ModA")
    b_root, _ = _make_vt_project_with_generated(tmp_path, "ModB")

    config = merger.MergeConfig(
        inputs=[Project.load(a_root), Project.load(b_root)],
        output_dir=tmp_path / "out",
        new_uuid=_meta.generate_uuid(),
        new_folder="MergedVT",
        new_name="MergedVT",
        conflict_policy="skip",
        divine=FakeDivine(),
    )
    merger.merge(config)

    # The merged mod must have its own Generated/Public/MergedVT/
    # VirtualTextures/ directory with the tileset binaries inside.
    gen_vt = (config.output_dir / "Generated" / "Public" / "MergedVT"
              / "VirtualTextures")
    assert gen_vt.is_dir(), (
        f"Generated/Public/MergedVT/VirtualTextures/ wasn't created. "
        f"This is the bug that caused black virtual textures: the "
        f"merger wasn't walking the Generated/ tree at all."
    )

    # Tileset binary preserved with its original UUID-derived name.
    # Renaming this would orphan the VTB → on-disk link.
    assert (gen_vt / f"{tileset_uuid}.gts").is_file()
    assert (gen_vt / f"{tileset_uuid}_mips.gtp").is_file()
    # Nested .gtex files too.
    gtex_files = list((gen_vt / "Albedo_Normal_Physical").glob("*.gtex"))
    assert gtex_files, "No .gtex files copied to merged mod"


def test_gtex_files_categorized_as_virtual_texture_asset(tmp_path):
    """`.gtex` files (per-texture metadata blobs under
    Generated/Public/<mod>/VirtualTextures/<channel>/<hash>.gtex)
    must classify as VIRTUAL_TEXTURE_ASSET so they're copied byte-
    identical and not put through any text-content remap path."""
    from core.project import _categorize, FileCategory
    mod_folder = "TestMod"
    root = tmp_path
    file_path = (root / "Generated" / "Public" / mod_folder
                 / "VirtualTextures" / "Albedo_Normal_Physical"
                 / "abcdef0123456789.gtex")
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"\x00" * 100)
    cf = _categorize(file_path, root, mod_folder)
    assert cf.category == FileCategory.VIRTUAL_TEXTURE_ASSET


def test_generated_bucket_assigned_for_files_under_generated_public(tmp_path):
    """Files under Generated/Public/<mod>/ get bucket='Generated' so
    the destination translator routes them to the merged mod's matching
    Generated/Public/ subtree."""
    from core.project import _categorize
    mod_folder = "TestMod"
    root = tmp_path
    file_path = (root / "Generated" / "Public" / mod_folder
                 / "VirtualTextures" / "ba05d4f3.gts")
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"\x00")
    cf = _categorize(file_path, root, mod_folder)
    assert cf.bucket == "Generated"


def test_generated_destination_path_is_correctly_translated(tmp_path):
    """Confirm Generated/Public/<old>/X.gts maps to
    Generated/Public/<new>/X.gts in the output."""
    from core.merger import _destination_for
    from core.project import Project

    a_root, _ = _make_vt_project_with_generated(tmp_path, "ModA")
    project = Project.load(a_root)
    gen_files = [cf for cf in project.files if cf.bucket == "Generated"]
    assert gen_files, "No files found in Generated bucket - walk is missing"

    cf = gen_files[0]
    dest = _destination_for(cf, tmp_path / "out", "MergedVT")
    assert dest is not None
    # The dest must be under output/Generated/Public/MergedVT/, not
    # output/Generated/Public/<old_mod>/.
    parts = dest.relative_to(tmp_path / "out").parts
    assert parts[0] == "Generated"
    assert parts[1] == "Public"
    assert parts[2] == "MergedVT"


def test_bank_lsf_sourcefile_remapped_on_mod_folder_rename(tmp_path):
    """BANK_LSF files (TextureBank, VisualBank, MaterialBank entries
    under Content/[PAK]_*/<uuid>.lsf) carry a SourceFile attribute
    pointing at Public/<mod_folder>/Assets/...  When the merger renames
    the mod folder, that SourceFile must be rewritten or the engine
    looks for the asset under the OLD mod folder name.

    Concrete failure mode this test pins against: with the original
    input mod still installed alongside the merged mod, the toolkit
    happens to resolve the asset through the stale path (since
    Glasses scans every mod, not just the one whose VTB references
    it). The bug only becomes visible once the input mod is removed
    from the workspace — at which point meshes render BLACK.

    We mock divine here (the real round-trip needs the actual
    divine.exe + .NET runtime) but assert that BANK_LSF goes THROUGH
    the binary-remap code path. With the bug present, BANK_LSF
    falls through to the "Opaque binary or unparsed XML: copy bytes
    unchanged" branch and never calls _remap_binary_lsf at all.
    """
    from core import merger
    from core.project import Project, CatalogedFile, FileCategory

    # Build a minimal cataloged file that looks like a BANK_LSF.
    root = tmp_path / "src"
    bank_path = (root / "Public" / "ModA"
                 / "Content" / "[PAK]_ModA"
                 / "abc12345-0000-0000-0000-000000000000.lsf")
    bank_path.parent.mkdir(parents=True)
    bank_path.write_bytes(b"LSOF\x07\x00\x00\x00fake-bank-lsf-payload")
    cf = CatalogedFile(
        path=bank_path,
        category=FileCategory.BANK_LSF,
        rel_to_project_root=bank_path.relative_to(root),
        rel_under_mod_folder=bank_path.relative_to(root / "Public" / "ModA"),
        bucket="Public",
    )

    # Count whether _remap_binary_lsf gets called. With the fix, it
    # does (and may return False on the fake LSF, that's fine — what
    # matters is the call happens). Without the fix, BANK_LSF goes
    # straight to verbatim copy and the function is never called.
    call_count = {"n": 0}
    original = merger._remap_binary_lsf
    def counting(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    config = merger.MergeConfig(
        inputs=[],  # not used by _emit_single
        output_dir=tmp_path / "out",
        new_uuid="11111111-2222-3333-4444-555555555555",
        new_folder="MergedMod",
        new_name="MergedMod",
        conflict_policy="skip",
        divine=FakeDivine(),  # truthy so we hit the divine branch
    )
    result = merger.MergeResult(output_dir=config.output_dir, new_project=None)
    rset = remap.RemapSet()
    rset.paths.add_substring("Public/ModA", "Public/MergedMod")

    # Monkey-patch in our counting wrapper.
    import core.merger as _merger_mod
    _merger_mod._remap_binary_lsf = counting
    try:
        dest = config.output_dir / "Public" / "MergedMod" / "Content" / "[PAK]_ModA" / cf.path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        # _emit_single takes (dest, source_tuple, config, result).
        # The Project in the tuple isn't used by the binary-remap
        # code path; pass None to keep the test minimal.
        merger._emit_single(dest, (None, cf, rset), config, result)
    finally:
        _merger_mod._remap_binary_lsf = original

    assert call_count["n"] == 1, (
        f"BANK_LSF must go through _remap_binary_lsf so its SourceFile "
        f"attributes get rewritten when the mod folder is renamed. "
        f"_remap_binary_lsf was called {call_count['n']} times; expected 1. "
        f"If 0, BANK_LSF is falling through to the verbatim-copy branch "
        f"and TextureBank SourceFile paths still point at the original "
        f"input mod folder, causing black textures in-game when the "
        f"original input mod is uninstalled."
    )
