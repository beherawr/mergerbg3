"""Tests for ``core.project``: walk and catalog real fixture projects."""

from __future__ import annotations

import pytest

from core import project
from core.project import FileCategory
from .helpers import FIXTURES


# --- Loading -----------------------------------------------------------------


def test_load_shadow_dance():
    """ShadowDance loads, has the right identity, and includes all expected
    file categories."""
    p = project.Project.load(FIXTURES / "ShadowDance")
    assert p.mod_meta.name == "Shadow Dance"
    assert p.mod_folder_name == "ShadowDance_50deb7b5-8734-7111-cb00-6682390ee00c"
    assert p.project_meta is not None
    assert p.project_meta.module == p.mod_meta.uuid

    cats = p.categories_present()
    # ShadowDance is a spell + Osiris mod, so it should have:
    assert FileCategory.MOD_META in cats
    assert FileCategory.PROJECT_META in cats
    assert FileCategory.STATS_TXT in cats
    assert FileCategory.STATS_XML in cats
    assert FileCategory.LOCALIZATION in cats
    assert FileCategory.STORY_GOAL in cats
    assert FileCategory.STORY_COMPILED in cats
    assert FileCategory.STORY_HEADER in cats
    assert FileCategory.MINIMAP in cats
    assert FileCategory.GUI_METADATA in cats

    # Specifically: 4 packed .txt, 4 source .stats, 3 goal scripts.
    counts = p.file_count_by_category()
    assert counts.get(FileCategory.STATS_TXT) == 4
    assert counts.get(FileCategory.STATS_XML) == 4
    assert counts.get(FileCategory.STORY_GOAL) == 3
    # Story compiled outputs we want to discard later.
    # story.div, story.div.osi, goals.raw, story_ac.dat,
    # story_orphanqueries_found.txt
    assert counts.get(FileCategory.STORY_COMPILED) == 5


def test_load_shadowdancer():
    """Shadowdancer is a weapon + visual mod; categories should reflect that."""
    p = project.Project.load(FIXTURES / "Shadowdancer")
    assert p.mod_meta.name == "Shadowdancer"

    cats = p.categories_present()
    assert FileCategory.STATS_TXT in cats
    assert FileCategory.STATS_XML in cats
    assert FileCategory.MODEL_GR2 in cats
    assert FileCategory.TEXTURE_TIF in cats
    assert FileCategory.ASSET_IMPORT_SETTINGS in cats
    assert FileCategory.BANK_LSF in cats
    assert FileCategory.ROOT_TEMPLATE_LSF in cats
    assert FileCategory.VFX_LSFX in cats
    assert FileCategory.LOCALIZATION in cats

    counts = p.file_count_by_category()
    assert counts.get(FileCategory.MODEL_GR2) == 3
    assert counts.get(FileCategory.TEXTURE_TIF) == 3
    # 7 import-settings XMLs (3 GR2 + 4 TIF/PNG-related: but we found 3 TIF;
    # the actual count we measured during research was 7 XML files total
    # including the .lsx files. The asset XMLs (paired with GR2/TIF) = 6:
    # ShaBla3.xml, ShadBla_BM.xml, ShadBla_NM.xml, ShadBla_PM.xml + 2 more
    # for the unpaired GR2s.)
    assert counts.get(FileCategory.ASSET_IMPORT_SETTINGS, 0) >= 4


def test_load_rejects_non_project_directory(tmp_path):
    """A directory with no Mods/<Folder>/meta.lsx isn't a project."""
    with pytest.raises(ValueError, match="No Mods.*meta.lsx"):
        project.Project.load(tmp_path)


def test_load_rejects_mismatched_folder_name(tmp_path):
    """If meta.lsx's Folder attribute doesn't match the on-disk directory,
    raise: this is a corrupted project state we shouldn't try to merge."""
    # Construct a minimal fake project where the folder name disagrees.
    bad_folder = tmp_path / "Mods" / "WrongName_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    bad_folder.mkdir(parents=True)
    (bad_folder / "meta.lsx").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<save>\n'
        '  <version major="4" minor="8" revision="0" build="500"/>\n'
        '  <region id="Config">\n'
        '    <node id="root">\n'
        '      <children>\n'
        '        <node id="ModuleInfo">\n'
        '          <attribute id="Folder" type="LSString" value="DifferentName_abc"/>\n'
        '          <attribute id="UUID" type="FixedString" value="ffffffff-aaaa-aaaa-aaaa-aaaaaaaaaaaa"/>\n'
        '          <attribute id="Name" type="LSString" value="Bad"/>\n'
        '        </node>\n'
        '      </children>\n'
        '    </node>\n'
        '  </region>\n'
        '</save>\n'
    )
    with pytest.raises(ValueError, match="Folder attribute"):
        project.Project.load(tmp_path)


# --- Categorization ---------------------------------------------------------


def test_no_files_categorized_as_other_in_real_projects():
    """If we leave OTHER files behind, it means we missed a file type in the
    categorizer. Fail loudly so we can investigate.

    (When this test fails after a real-mod upload, that's how we discover
    new file types to handle.)
    """
    for project_dir in (FIXTURES / "ShadowDance", FIXTURES / "Shadowdancer"):
        p = project.Project.load(project_dir)
        others = p.files_by_category(FileCategory.OTHER)
        assert others == [], (
            f"Uncategorized files in {project_dir.name}: "
            f"{[str(o.rel_to_project_root) for o in others]}"
        )


def test_asset_import_xml_paired_with_binary():
    """The categorizer distinguishes asset-import-settings XML (sibling to
    a GR2/TIF) from generic LSX. ShadBla_BM.xml is paired, so should be
    ASSET_IMPORT_SETTINGS, not a generic bucket."""
    p = project.Project.load(FIXTURES / "Shadowdancer")
    bm = next(f for f in p.files if f.path.name == "ShadBla_BM.xml")
    assert bm.category == FileCategory.ASSET_IMPORT_SETTINGS


def test_rel_under_mod_folder_correct():
    """The rel_under_mod_folder path is the path-remap key. For
    Public/<Mod>/Stats/Generated/Data/Weapon.txt the value should be
    Stats/Generated/Data/Weapon.txt with no <Mod> prefix."""
    p = project.Project.load(FIXTURES / "Shadowdancer")
    weapon_txt = next(
        f for f in p.files if f.path.name == "Weapon.txt" and f.bucket == "Public"
    )
    assert weapon_txt.rel_under_mod_folder is not None
    assert str(weapon_txt.rel_under_mod_folder).replace("\\", "/") == (
        "Stats/Generated/Data/Weapon.txt"
    )


def test_summary_includes_identity_and_dep():
    p = project.Project.load(FIXTURES / "ShadowDance")
    summary = p.summary()
    assert "Shadow Dance" in summary
    assert "GustavX" in summary
    assert "Files" in summary
