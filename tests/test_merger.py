"""End-to-end merger tests.

These tests run the full pipeline against the two real fixture projects.
After session 3 closes out, this is what proves the engine actually works:
a real merge succeeds, the output is loadable as a Toolkit project, and
validation comes up clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import merger, validate, meta as _meta
from core.project import Project, FileCategory
from core.references import IdKind
from .helpers import FIXTURES


# --- The flagship integration test ------------------------------------------


def test_real_merge_shadow_dance_and_shadowdancer(tmp_path):
    """Merge the two real fixture projects end-to-end and confirm:

    - The pipeline runs without error.
    - The output directory has the expected Toolkit layout.
    - The output is loadable as a Project.
    - Both inputs' stat names show up in the merged output.
    - Both inputs' SE script registrations show up in the merged meta.
    - Validation produces no merger-bug findings (no definition collisions).
    """
    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Shadowdancer")

    new_uuid = _meta.generate_uuid()
    new_folder = f"MergerTest_{new_uuid.replace('-', '')[:12]}"

    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=tmp_path / "merged",
        new_uuid=new_uuid,
        new_folder=new_folder,
        new_name="Merger Test",
        new_author="(test suite)",
        conflict_policy="skip",
    )
    result = merger.merge(config)

    # Output exists.
    assert result.output_dir.is_dir()
    assert (result.output_dir / "Mods" / new_folder / "meta.lsx").is_file()
    assert (result.output_dir / "Projects" / new_folder / "meta.lsx").is_file()

    # The clean-union case produces no identifier clashes: only a soft
    # "file_overlap" record because both projects include a Toolkit-generated
    # MinimapAtlas.mmxml placeholder. That's not a real merge conflict; the
    # files are identical placeholders. We assert that's the only one.
    identifier_clashes = [c for c in result.conflicts if c.kind != "file_overlap"]
    assert identifier_clashes == []
    file_overlaps = [c for c in result.conflicts if c.kind == "file_overlap"]
    assert all("MinimapAtlas" in c.identifier for c in file_overlaps), (
        f"unexpected file_overlap conflicts: {file_overlaps}"
    )

    # The output is loadable as a Project.
    assert result.new_project is not None
    out = result.new_project
    assert out.mod_meta.uuid == new_uuid
    assert out.mod_meta.name == "Merger Test"
    assert out.mod_folder_name == new_folder

    # Stats from both inputs are present.
    stats_files = out.files_by_category(FileCategory.STATS_TXT)
    names = {f.path.name for f in stats_files}
    assert {
        "Spell_Shout.txt", "Spell_Target.txt",
        "Status_BOOST.txt", "Status_INVISIBLE.txt",  # from A
        "Weapon.txt",                                  # from B
    } == names

    # SE scripts are unioned.
    assert len(out.mod_meta.scripts) == 2  # both of A's

    # Dependencies deduplicated to one (both depend on GustavX).
    assert len(out.mod_meta.dependencies) == 1
    assert out.mod_meta.dependencies[0].name == "GustavX"


def test_merged_output_passes_validation(tmp_path):
    """The merged output should validate cleanly: no definition collisions
    (which would be a merger bug). Orphans are fine and expected."""
    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Shadowdancer")

    new_uuid = _meta.generate_uuid()
    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=tmp_path / "merged",
        new_uuid=new_uuid,
        new_folder=f"V_{new_uuid.replace('-', '')[:12]}",
        new_name="Validation Test",
        conflict_policy="skip",
    )
    result = merger.merge(config)
    report = validate.validate(result.new_project)

    # Critical: no definition collisions. Each identifier appears exactly
    # once in the merged output's definition side.
    assert not report.definition_collisions, (
        f"merger produced duplicate definitions: {report.definition_collisions}"
    )
    assert not report.is_blocked()


# --- Synthetic conflict tests (small in-memory fixtures) --------------------


def _make_minimal_project(tmp_path: Path, name: str, stat_name: str,
                         stat_value: str = "Common") -> Path:
    """Build a tiny Toolkit-shaped project in ``tmp_path/<name>`` with
    exactly one Weapon stat entry."""
    uuid = _meta.generate_uuid()
    folder = f"{name}_{uuid}"
    root = tmp_path / name
    root.mkdir()

    # Mods/<Folder>/meta.lsx
    mod_path = root / "Mods" / folder
    mod_path.mkdir(parents=True)
    mm = _meta.ModMeta(uuid=uuid, folder=folder, name=name, author="test")
    _meta.write_mod_meta_file(mm, mod_path / "meta.lsx")

    # Public/<Folder>/Stats/Generated/Data/Weapon.txt
    stats_path = root / "Public" / folder / "Stats" / "Generated" / "Data"
    stats_path.mkdir(parents=True)
    (stats_path / "Weapon.txt").write_text(
        f'new entry "{stat_name}"\r\n'
        f'type "Weapon"\r\n'
        f'using "WPN_Dagger"\r\n'
        f'data "Rarity" "{stat_value}"\r\n'
        f'\r\n',
        encoding="utf-8",
    )

    return root


def test_skip_policy_keeps_a_drops_b(tmp_path):
    """Two inputs define the same stat with different values; skip policy
    keeps A's, drops B's, records the conflict."""
    a_root = _make_minimal_project(tmp_path, "ModA", "MyBlade", "Rare")
    b_root = _make_minimal_project(tmp_path, "ModB", "MyBlade", "Common")

    config = merger.MergeConfig(
        inputs=[Project.load(a_root), Project.load(b_root)],
        output_dir=tmp_path / "out",
        new_uuid=_meta.generate_uuid(),
        new_folder="Merged_skip",
        new_name="Merged",
        conflict_policy="skip",
    )
    result = merger.merge(config)

    # The conflict was recorded.
    assert any(c.identifier == "MyBlade" and c.resolution == "skipped"
               for c in result.conflicts)

    # The merged output keeps A's rarity.
    merged_txt = (
        result.output_dir / "Public" / "Merged_skip" / "Stats"
        / "Generated" / "Data" / "Weapon.txt"
    ).read_text(encoding="utf-8")
    assert '"Rare"' in merged_txt
    assert '"Common"' not in merged_txt


def test_prefix_policy_renames_b_and_propagates(tmp_path):
    """Same scenario with prefix policy: B's MyBlade becomes ModB_MyBlade."""
    a_root = _make_minimal_project(tmp_path, "ModA", "MyBlade", "Rare")
    b_root = _make_minimal_project(tmp_path, "ModB", "MyBlade", "Common")

    config = merger.MergeConfig(
        inputs=[Project.load(a_root), Project.load(b_root)],
        output_dir=tmp_path / "out",
        new_uuid=_meta.generate_uuid(),
        new_folder="Merged_prefix",
        new_name="Merged",
        conflict_policy="prefix",
        conflict_prefix="ModB_",
    )
    result = merger.merge(config)

    # The conflict was recorded with the new name.
    assert any(c.identifier == "MyBlade"
               and c.resolution == "prefixed_ModB_MyBlade"
               for c in result.conflicts)

    merged_txt = (
        result.output_dir / "Public" / "Merged_prefix" / "Stats"
        / "Generated" / "Data" / "Weapon.txt"
    ).read_text(encoding="utf-8")
    # Both entries are present.
    assert '"MyBlade"' in merged_txt
    assert '"ModB_MyBlade"' in merged_txt
    # Their respective values are preserved.
    assert '"Rare"' in merged_txt
    assert '"Common"' in merged_txt


def test_fail_policy_aborts_with_clash(tmp_path):
    """Fail policy raises MergeError on any clash, before writing anything."""
    a_root = _make_minimal_project(tmp_path, "ModA", "MyBlade", "Rare")
    b_root = _make_minimal_project(tmp_path, "ModB", "MyBlade", "Common")

    config = merger.MergeConfig(
        inputs=[Project.load(a_root), Project.load(b_root)],
        output_dir=tmp_path / "out",
        new_uuid=_meta.generate_uuid(),
        new_folder="Merged_fail",
        new_name="Merged",
        conflict_policy="fail",
    )
    with pytest.raises(merger.MergeError, match="unresolved clashes"):
        merger.merge(config)


# --- Output-directory handling ---------------------------------------------


def test_allows_non_empty_output_dir_without_bucket_collision(tmp_path):
    """Stray content in the output_dir is fine: the merger only cares
    that its own bucket subfolders don't collide with existing mods.
    This is what makes canonical-workspace output work (the workspace
    is always non-empty)."""
    (tmp_path / "leftover.txt").write_text("hi")
    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Shadowdancer")
    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=tmp_path,
        new_uuid=_meta.generate_uuid(),
        new_folder="UniqueNewModFolder",
        new_name="X",
    )
    # Should complete without raising.
    result = merger.merge(config)
    assert result.new_project is not None


def test_refuses_collision_with_existing_mod_folder(tmp_path):
    """When the output_dir already contains a mod with the same folder
    name we'd overwrite, refuse: that's silent data loss otherwise."""
    # Pre-create Mods/X/ to simulate an existing mod with that folder name.
    (tmp_path / "Mods" / "X").mkdir(parents=True)
    (tmp_path / "Mods" / "X" / "meta.lsx").write_text("<lsx/>")

    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Shadowdancer")
    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=tmp_path,
        new_uuid=_meta.generate_uuid(),
        new_folder="X",   # collides with the pre-existing Mods/X
        new_name="X",
    )
    with pytest.raises(merger.MergeError, match="already exists"):
        merger.merge(config)


def test_allow_existing_output_bypasses_collision_check(tmp_path):
    """allow_existing_output=True skips the collision check: used by
    in-place mode, which intentionally overwrites the target mod."""
    (tmp_path / "Mods" / "X").mkdir(parents=True)
    (tmp_path / "Mods" / "X" / "meta.lsx").write_text("<lsx/>")
    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Shadowdancer")
    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=tmp_path,
        new_uuid=_meta.generate_uuid(),
        new_folder="X",
        new_name="X",
        allow_existing_output=True,
    )
    # Should NOT raise the collision check (may still raise something
    # else if the stub meta.lsx is malformed, but we get past the check).
    try:
        merger.merge(config)
    except merger.MergeError as e:
        # The empty stub meta we wrote may cause a downstream failure;
        # any MergeError that ISN'T "already exists" is fine here.
        assert "already exists" not in str(e)


def test_requires_two_inputs():
    a = Project.load(FIXTURES / "ShadowDance")
    config = merger.MergeConfig(
        inputs=[a],
        output_dir=Path("/tmp/never_written"),
        new_uuid=_meta.generate_uuid(),
        new_folder="X",
        new_name="X",
    )
    with pytest.raises(merger.MergeError, match="at least two"):
        merger.merge(config)


def test_prefix_policy_requires_prefix_string(tmp_path):
    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Shadowdancer")
    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=tmp_path / "out",
        new_uuid=_meta.generate_uuid(),
        new_folder="X",
        new_name="X",
        conflict_policy="prefix",
        conflict_prefix="",  # missing!
    )
    with pytest.raises(merger.MergeError, match="non-empty conflict_prefix"):
        merger.merge(config)


# --- Validation report -----------------------------------------------------


def test_validation_report_lists_orphans():
    """A clean Project (one of our fixtures) validates with no definition
    collisions but does list orphans (GustavX dependency, WPN_Dagger,
    base-game tokens)."""
    p = Project.load(FIXTURES / "Shadowdancer")
    report = validate.validate(p)
    assert not report.is_blocked()  # no merger bugs in a regular project
    # WPN_Dagger is referenced (inherits from it) but not defined.
    orphan_stat_names = [
        e.value for e in report.orphan_references.get("stat_name", [])
    ]
    assert "WPN_Dagger" in orphan_stat_names


def test_validation_render_does_not_crash():
    """The text render is what shows up in CLI / GUI; basic sanity check."""
    p = Project.load(FIXTURES / "ShadowDance")
    report = validate.validate(p)
    text = report.render()
    assert "Validation of" in text
    assert "Shadow Dance" in text
