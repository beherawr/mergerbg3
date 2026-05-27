"""Tests for rename-on-collide of referenced binary assets.

Reported bug from user feedback (2026-05): when two mods both ship a
``newAtlas.dds`` (or other referenced binary asset) with differing
content, the merger was dropping B's copy and logging "kept A's". This
silently broke every icon B's UI referenced: the icon UV map pointed
into a bitmap that no longer existed.

The right behavior: keep BOTH files, by renaming B's to
``newAtlas<suffix>.dds``, and rewrite every textual reference inside
B's content (icon UV maps, root templates, stats Icon= rows, etc.) to
the new filename. Path-keyed identity assets (``mod_publish_logo.png``,
``thumbnail.png``) deliberately stay keep-A because renaming wouldn't
help: they're looked up at fixed paths.

These tests are self-contained (synth mods under ``tmp_path``) so they
pass on CI runners without access to the private fixture mods.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import merger
from core.project import FileCategory, Project


# ---------------------------------------------------------------------------
# Mod-building helpers (duplicated from test_structured_lsx_merge.py to keep
# each test file standalone; minor duplication is cheaper than coupling).
# ---------------------------------------------------------------------------


def _write_meta_lsx(path: Path, uuid: str, folder: str, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<save>
  <version major="4" minor="8" revision="0" build="500"/>
  <region id="Config">
    <node id="root">
      <children>
        <node id="ModuleInfo">
          <attribute id="UUID" type="FixedString" value="{uuid}"/>
          <attribute id="Folder" type="LSString" value="{folder}"/>
          <attribute id="Name" type="LSString" value="{name}"/>
          <attribute id="Author" type="LSString" value="Test"/>
          <attribute id="Description" type="LSString" value=""/>
          <attribute id="Version64" type="int64" value="36028797018963968"/>
          <attribute id="PublishHandle" type="uint64" value="0"/>
          <attribute id="NumPlayers" type="uint8" value="4"/>
          <attribute id="MD5" type="LSString" value=""/>
        </node>
      </children>
    </node>
  </region>
</save>
""",
        encoding="utf-8",
    )


def _write_icon_uv_lsx(
    path: Path, atlas_filename: str, icon_name: str = "icon_test",
) -> None:
    """Write an icon UV-coordinate registry that references a texture
    atlas by *bare filename*. This is the structure that breaks if the
    referenced atlas disappears or gets renamed without the UV being
    updated alongside.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<save>
  <version major="4" minor="8" revision="0" build="500"/>
  <region id="IconUVList">
    <node id="root">
      <children>
        <node id="IconUV">
          <attribute id="MapKey" type="FixedString" value="{icon_name}"/>
          <attribute id="U1" type="float" value="0.0"/>
          <attribute id="V1" type="float" value="0.0"/>
          <attribute id="U2" type="float" value="1.0"/>
          <attribute id="V2" type="float" value="1.0"/>
          <attribute id="TextureAtlas" type="LSString" value="{atlas_filename}"/>
        </node>
      </children>
    </node>
  </region>
</save>
""",
        encoding="utf-8",
    )


def _build_mod_with_atlas(
    workspace: Path,
    folder: str,
    uuid: str,
    name: str,
    atlas_filename: str,
    atlas_bytes: bytes,
    icon_name: str = "icon_test",
) -> Path:
    """Build a tiny self-contained mod that ships a .dds atlas plus an
    icon UV map referencing that atlas by bare filename. The structure
    mirrors what real BG3 icon mods look like.
    """
    project_root = workspace / folder
    _write_meta_lsx(
        project_root / "Mods" / folder / "meta.lsx",
        uuid=uuid, folder=folder, name=name,
    )
    # The atlas itself.
    atlas_path = (project_root / "Public" / folder / "Assets" / "Textures"
                  / "Icons" / atlas_filename)
    atlas_path.parent.mkdir(parents=True, exist_ok=True)
    atlas_path.write_bytes(atlas_bytes)
    # The UV map that references the atlas.
    _write_icon_uv_lsx(
        project_root / "Public" / folder / "GUI" / f"Icons_{folder}.lsx",
        atlas_filename=atlas_filename,
        icon_name=icon_name,
    )
    return project_root


# ---------------------------------------------------------------------------
# The bug case the user reported.
# ---------------------------------------------------------------------------


def test_colliding_dds_atlases_both_get_kept_with_rename(tmp_path):
    """Two mods both have ``newAtlas.dds`` at the same destination path
    with DIFFERING contents. After the merge, BOTH files must exist in
    the output. A's keeps its original name; B's is renamed."""
    workspace = tmp_path / "ws"
    out = tmp_path / "out"

    _build_mod_with_atlas(
        workspace, "ModA",
        uuid="aaaaaaaa-1111-1111-1111-111111111111", name="Mod A",
        atlas_filename="newAtlas.dds",
        atlas_bytes=b"AAAAA_mod_a_atlas_bytes",
        icon_name="icon_a",
    )
    _build_mod_with_atlas(
        workspace, "ModB",
        uuid="bbbbbbbb-2222-2222-2222-222222222222", name="Mod B",
        atlas_filename="newAtlas.dds",
        atlas_bytes=b"BBBBB_mod_b_atlas_bytes",  # different bytes!
        icon_name="icon_b",
    )

    a = Project.load(workspace / "ModA")
    b = Project.load(workspace / "ModB")
    result = merger.merge(merger.MergeConfig(
        inputs=[a, b],
        output_dir=out,
        new_uuid="cccccccc-3333-3333-3333-333333333333",
        new_folder="Merged", new_name="Merged",
        conflict_policy="skip",
    ))

    # A's atlas: kept at the original path.
    a_atlas = out / "Public" / "Merged" / "Assets" / "Textures" / "Icons" / "newAtlas.dds"
    assert a_atlas.exists(), "A's atlas should be at the original name"
    assert a_atlas.read_bytes() == b"AAAAA_mod_a_atlas_bytes"

    # B's atlas: renamed, present somewhere in the Icons/ directory.
    icons_dir = out / "Public" / "Merged" / "Assets" / "Textures" / "Icons"
    b_atlas_candidates = [
        p for p in icons_dir.iterdir()
        if p.is_file() and p.suffix == ".dds" and p.name != "newAtlas.dds"
    ]
    assert len(b_atlas_candidates) == 1, (
        f"expected exactly one renamed atlas, got "
        f"{[p.name for p in b_atlas_candidates]}"
    )
    b_atlas = b_atlas_candidates[0]
    assert b_atlas.read_bytes() == b"BBBBB_mod_b_atlas_bytes"
    # The new name should still start with the original stem.
    assert b_atlas.name.startswith("newAtlas"), b_atlas.name

    # And the rename should be logged in the merge result.
    rename_conflicts = [
        c for c in result.conflicts
        if c.kind == "asset_renamed_to_keep_both"
    ]
    assert len(rename_conflicts) == 1
    assert "newAtlas.dds" in rename_conflicts[0].identifier


def test_colliding_dds_references_in_b_get_rewritten(tmp_path):
    """When B's atlas gets renamed, every textual reference inside B's
    content (the icon UV map specifically) must follow. Without this,
    B's UV map would point at a filename that no longer exists."""
    workspace = tmp_path / "ws"
    out = tmp_path / "out"

    _build_mod_with_atlas(
        workspace, "ModA",
        uuid="aaaaaaaa-1111-1111-1111-111111111111", name="Mod A",
        atlas_filename="newAtlas.dds",
        atlas_bytes=b"A" * 64,
        icon_name="icon_a",
    )
    _build_mod_with_atlas(
        workspace, "ModB",
        uuid="bbbbbbbb-2222-2222-2222-222222222222", name="Mod B",
        atlas_filename="newAtlas.dds",
        atlas_bytes=b"B" * 64,
        icon_name="icon_b",
    )

    a = Project.load(workspace / "ModA")
    b = Project.load(workspace / "ModB")
    merger.merge(merger.MergeConfig(
        inputs=[a, b],
        output_dir=out,
        new_uuid="cccccccc-3333-3333-3333-333333333333",
        new_folder="Merged", new_name="Merged",
        conflict_policy="skip",
    ))

    # Find B's UV map in the output (icon name is the giveaway).
    gui_dir = out / "Public" / "Merged" / "GUI"
    uv_files = list(gui_dir.glob("*.lsx"))
    # Both ModA's and ModB's icon UV files got merged into the same dir.
    # The one containing "icon_b" is B's.
    b_uv = next(p for p in uv_files if "icon_b" in p.read_text(encoding="utf-8"))
    b_uv_text = b_uv.read_text(encoding="utf-8")

    # B's UV map's TextureAtlas reference should NOT be "newAtlas.dds" any
    # more: that path now resolves to A's atlas. It must point at the
    # renamed filename so icons render against B's bitmap.
    # We don't know the exact rename (depends on the suffix derivation),
    # but we know it must NOT be the unmodified original.
    assert 'TextureAtlas" type="LSString" value="newAtlas.dds"' not in b_uv_text, (
        f"B's icon UV still references the unmodified atlas filename; "
        f"reference rewrite failed:\n{b_uv_text}"
    )
    # And it should still reference *some* newAtlas-prefixed filename.
    assert 'value="newAtlas' in b_uv_text


def test_byte_identical_dds_files_dedupe_without_rename(tmp_path):
    """If both mods ship the exact same atlas (perhaps copied from a
    shared community resource), there's no real conflict: the file
    should dedupe to one copy with no rename. The user doesn't want to
    see a spurious "asset renamed" conflict for files that were already
    identical."""
    workspace = tmp_path / "ws"
    out = tmp_path / "out"

    same_bytes = b"SHARED_ATLAS_BYTES_" + b"X" * 100

    _build_mod_with_atlas(
        workspace, "ModA",
        uuid="aaaaaaaa-1111-1111-1111-111111111111", name="Mod A",
        atlas_filename="shared.dds", atlas_bytes=same_bytes,
        icon_name="icon_a",
    )
    _build_mod_with_atlas(
        workspace, "ModB",
        uuid="bbbbbbbb-2222-2222-2222-222222222222", name="Mod B",
        atlas_filename="shared.dds", atlas_bytes=same_bytes,
        icon_name="icon_b",
    )

    a = Project.load(workspace / "ModA")
    b = Project.load(workspace / "ModB")
    result = merger.merge(merger.MergeConfig(
        inputs=[a, b],
        output_dir=out,
        new_uuid="cccccccc-3333-3333-3333-333333333333",
        new_folder="Merged", new_name="Merged",
        conflict_policy="skip",
    ))

    # Exactly one shared.dds in the output.
    icons_dir = out / "Public" / "Merged" / "Assets" / "Textures" / "Icons"
    dds_files = list(icons_dir.glob("*.dds"))
    assert len(dds_files) == 1
    assert dds_files[0].name == "shared.dds"

    # No rename conflict was logged.
    assert not any(
        c.kind == "asset_renamed_to_keep_both" for c in result.conflicts
    ), [c.kind for c in result.conflicts]


def test_thumbnail_png_stays_keep_a_not_renamed(tmp_path):
    """``thumbnail.png`` is an IMAGE_ASSET, not in the rename-on-collide
    set. The Toolkit looks for it at a fixed path so renaming wouldn't
    help: only one of them gets used as the project's thumbnail
    regardless. Verify we still keep A's version and don't accidentally
    rename it now that the rename machinery exists.
    """
    workspace = tmp_path / "ws"
    out = tmp_path / "out"

    _write_meta_lsx(
        workspace / "ModA" / "Mods" / "ModA" / "meta.lsx",
        uuid="aaaaaaaa-1111-1111-1111-111111111111", folder="ModA", name="Mod A",
    )
    _write_meta_lsx(
        workspace / "ModB" / "Mods" / "ModB" / "meta.lsx",
        uuid="bbbbbbbb-2222-2222-2222-222222222222", folder="ModB", name="Mod B",
    )
    (workspace / "ModA" / "Projects" / "ModA").mkdir(parents=True)
    (workspace / "ModB" / "Projects" / "ModB").mkdir(parents=True)
    (workspace / "ModA" / "Projects" / "ModA" / "thumbnail.png").write_bytes(
        b"A_thumbnail_bytes"
    )
    (workspace / "ModB" / "Projects" / "ModB" / "thumbnail.png").write_bytes(
        b"B_thumbnail_bytes_different"  # different content
    )

    a = Project.load(workspace / "ModA")
    b = Project.load(workspace / "ModB")
    result = merger.merge(merger.MergeConfig(
        inputs=[a, b],
        output_dir=out,
        new_uuid="cccccccc-3333-3333-3333-333333333333",
        new_folder="Merged", new_name="Merged",
        conflict_policy="skip",
    ))

    # Exactly one thumbnail in the output dir: A's.
    proj_dir = out / "Projects" / "Merged"
    thumbnails = list(proj_dir.glob("*.png"))
    assert len(thumbnails) == 1
    assert thumbnails[0].name == "thumbnail.png"
    assert thumbnails[0].read_bytes() == b"A_thumbnail_bytes"
    # The conflict is the old keep-A flavor, NOT a rename.
    rename_conflicts = [
        c for c in result.conflicts if c.kind == "asset_renamed_to_keep_both"
    ]
    assert not rename_conflicts, (
        f"thumbnail.png should not have been renamed; the IMAGE_ASSET "
        f"category is keep-A by design. Got renames: "
        f"{[c.identifier for c in rename_conflicts]}"
    )


def test_no_collision_means_no_rename(tmp_path):
    """Sanity check: if A and B have entirely different texture names,
    no rename happens. Both files land in the output side-by-side."""
    workspace = tmp_path / "ws"
    out = tmp_path / "out"

    _build_mod_with_atlas(
        workspace, "ModA",
        uuid="aaaaaaaa-1111-1111-1111-111111111111", name="Mod A",
        atlas_filename="atlasA.dds", atlas_bytes=b"A" * 32,
        icon_name="icon_a",
    )
    _build_mod_with_atlas(
        workspace, "ModB",
        uuid="bbbbbbbb-2222-2222-2222-222222222222", name="Mod B",
        atlas_filename="atlasB.dds", atlas_bytes=b"B" * 32,
        icon_name="icon_b",
    )

    a = Project.load(workspace / "ModA")
    b = Project.load(workspace / "ModB")
    result = merger.merge(merger.MergeConfig(
        inputs=[a, b],
        output_dir=out,
        new_uuid="cccccccc-3333-3333-3333-333333333333",
        new_folder="Merged", new_name="Merged",
        conflict_policy="skip",
    ))

    icons_dir = out / "Public" / "Merged" / "Assets" / "Textures" / "Icons"
    names = sorted(p.name for p in icons_dir.glob("*.dds"))
    assert names == ["atlasA.dds", "atlasB.dds"]
    assert not any(
        c.kind == "asset_renamed_to_keep_both" for c in result.conflicts
    )


def test_paired_gr2_and_import_xml_rename_together(tmp_path):
    """An asset-import-settings .xml shares a stem with its binary
    (``foo.GR2`` ↔ ``foo.xml``). The Toolkit's importer relies on the
    stem-matching to associate them. If we rename one and not the other,
    the importer falls over.

    Verifies: when a paired (.GR2, .xml) pair collides between mods,
    the rename of the binary also drags the XML to the same new stem.
    """
    workspace = tmp_path / "ws"
    out = tmp_path / "out"

    def _build(folder: str, uuid: str, name: str, gr2_bytes: bytes, xml_text: str):
        project_root = workspace / folder
        _write_meta_lsx(
            project_root / "Mods" / folder / "meta.lsx",
            uuid=uuid, folder=folder, name=name,
        )
        # The GR2 binary at a typical asset path.
        gr2 = (project_root / "Public" / folder / "Assets" / "Models"
               / "WPN_Sword.GR2")
        gr2.parent.mkdir(parents=True, exist_ok=True)
        gr2.write_bytes(gr2_bytes)
        # The sibling import-settings XML with the matching stem.
        xml = gr2.parent / "WPN_Sword.xml"
        xml.write_text(xml_text, encoding="utf-8")
        return project_root

    _build(
        "ModA", uuid="aaaaaaaa-1111-1111-1111-111111111111", name="Mod A",
        gr2_bytes=b"A" * 100,
        xml_text='<?xml version="1.0"?><AssetImport version="1"><A>1</A></AssetImport>',
    )
    _build(
        "ModB", uuid="bbbbbbbb-2222-2222-2222-222222222222", name="Mod B",
        gr2_bytes=b"B" * 100,
        xml_text='<?xml version="1.0"?><AssetImport version="1"><B>2</B></AssetImport>',
    )

    a = Project.load(workspace / "ModA")
    b = Project.load(workspace / "ModB")
    merger.merge(merger.MergeConfig(
        inputs=[a, b],
        output_dir=out,
        new_uuid="cccccccc-3333-3333-3333-333333333333",
        new_folder="Merged", new_name="Merged",
        conflict_policy="skip",
    ))

    models_dir = out / "Public" / "Merged" / "Assets" / "Models"
    # A's pair stayed at WPN_Sword.{GR2,xml}
    assert (models_dir / "WPN_Sword.GR2").read_bytes() == b"A" * 100
    a_xml = (models_dir / "WPN_Sword.xml").read_text(encoding="utf-8")
    assert "<A>1</A>" in a_xml
    # B's pair is renamed to a new shared stem.
    other_gr2 = [
        p for p in models_dir.glob("*.GR2") if p.name != "WPN_Sword.GR2"
    ]
    assert len(other_gr2) == 1
    b_gr2 = other_gr2[0]
    assert b_gr2.read_bytes() == b"B" * 100
    # The matching XML must exist at the SAME new stem.
    b_xml_path = b_gr2.with_suffix(".xml")
    assert b_xml_path.exists(), (
        f"paired XML wasn't renamed to match {b_gr2.name}; "
        f"found these XMLs: {[p.name for p in models_dir.glob('*.xml')]}"
    )
    assert "<B>2</B>" in b_xml_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Filename-suffix sanitization.
# ---------------------------------------------------------------------------


def test_sanitize_filename_suffix_strips_uuid_tail():
    """Toolkit folder names follow ``ModName_<uuid>``. The suffix should
    use just the human-meaningful part so renamed files are recognizable.
    """
    s = merger._sanitize_filename_suffix(
        "WoWVanish_2328df4d-c877-4bbc-8609-774ba670adab"
    )
    assert s == "WoWVanish"


def test_sanitize_filename_suffix_strips_special_chars():
    """Non-alphanumerics get dropped so the result is filename-safe on
    every filesystem."""
    s = merger._sanitize_filename_suffix("My Mod-v2.0!")
    assert s == "MyModv20"


def test_sanitize_filename_suffix_caps_length():
    """Very long folder names get capped so the resulting filenames
    don't blow through Windows' path limits."""
    s = merger._sanitize_filename_suffix("A" * 200)
    assert len(s) <= 16
    assert all(c == "A" for c in s)


def test_sanitize_filename_suffix_fallback_when_empty():
    """If the folder name had no alphanumerics at all, fall back to a
    safe default rather than producing an empty suffix."""
    s = merger._sanitize_filename_suffix("---___---")
    assert s == "alt"
