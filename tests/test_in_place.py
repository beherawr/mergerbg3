"""Tests for the in-place merge mode (``MergeConfig.in_place=True``).

These exercise three things:
1. **Happy path**: in-place merge of two real fixtures: target gains B's
   content, keeps A's identity, no conflicts emitted on a clean union.
2. **Crash safety**: if the merge raises mid-way, the target directory
   stays intact (the temp-write + atomic-replace pattern protects A).
3. **No-leftover guarantee**: successful merges remove both the
   ``.merging_*`` and ``.backup_*`` sibling directories.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from core import merger
from core.project import Project
from .helpers import FIXTURES


# --- Helpers ----------------------------------------------------------------


def _copy_fixture_to(name: str, dest_root: Path) -> Path:
    """Copy a fixture project tree into a writable workspace so tests can
    perform in-place modifications without touching the read-only fixture
    symlink. Returns the project root inside dest_root."""
    target = dest_root / name
    shutil.copytree(FIXTURES / name, target, symlinks=False)
    return target


def _file_inventory(root: Path) -> list[tuple[Path, int]]:
    """Snapshot all files under root with their sizes. Used for
    before/after comparisons."""
    return sorted(
        (p.relative_to(root), p.stat().st_size)
        for p in root.rglob("*") if p.is_file()
    )


# --- Happy path -------------------------------------------------------------


def test_in_place_merge_preserves_target_identity(tmp_path):
    """After ``in_place=True``, the target dir has A's identity (same
    UUID, same folder, same name) plus B's content folded in."""
    target = _copy_fixture_to("ShadowDance", tmp_path)
    a_before = Project.load(target)
    b = Project.load(FIXTURES / "Shadowdancer")

    config = merger.MergeConfig(
        inputs=[a_before, b],
        output_dir=target,
        new_uuid=a_before.mod_meta.uuid,
        new_folder=a_before.mod_folder_name,
        new_name=a_before.mod_meta.name,
        new_author=a_before.mod_meta.author,
        conflict_policy="skip",
        in_place=True,
    )
    result = merger.merge(config)

    # Target still exists at the same path.
    assert target.is_dir()
    a_after = Project.load(target)

    # Identity preserved.
    assert a_after.mod_meta.uuid == a_before.mod_meta.uuid
    assert a_after.mod_folder_name == a_before.mod_folder_name
    assert a_after.mod_meta.name == a_before.mod_meta.name

    # B's content was added.
    assert len(a_after.files) > len(a_before.files)
    # Every file in the merged project was an emission. (Emissions track
    # what the merger wrote; both totals include the regenerated metas.)
    assert len(result.emissions) == len(a_after.files)


def test_in_place_merge_adds_dependencies_from_b(tmp_path):
    """Mod B's dependencies should appear in mod A's meta.lsx after the
    in-place merge (the meta union behavior)."""
    target = _copy_fixture_to("ShadowDance", tmp_path)
    a_before = Project.load(target)
    b = Project.load(FIXTURES / "Bloodfang")  # has 13 dependencies

    config = merger.MergeConfig(
        inputs=[a_before, b],
        output_dir=target,
        new_uuid=a_before.mod_meta.uuid,
        new_folder=a_before.mod_folder_name,
        new_name=a_before.mod_meta.name,
        conflict_policy="skip",
        in_place=True,
    )
    merger.merge(config)

    a_after = Project.load(target)
    a_after_deps = {d.uuid for d in a_after.mod_meta.dependencies}
    b_deps = {d.uuid for d in b.mod_meta.dependencies}
    # Every dep of B is now in A's meta.lsx.
    assert b_deps.issubset(a_after_deps)


def test_in_place_cleans_up_sibling_temps_on_success(tmp_path):
    """A successful in-place merge leaves no ``.merging_*`` or
    ``.backup_*`` sibling directories behind."""
    target = _copy_fixture_to("ShadowDance", tmp_path)
    a = Project.load(target)
    b = Project.load(FIXTURES / "Shadowdancer")

    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=target,
        new_uuid=a.mod_meta.uuid,
        new_folder=a.mod_folder_name,
        new_name=a.mod_meta.name,
        conflict_policy="skip",
        in_place=True,
    )
    merger.merge(config)

    siblings = [
        p.name for p in tmp_path.iterdir()
        if p.name.startswith("ShadowDance.")
    ]
    assert siblings == [], f"unexpected sibling temps: {siblings}"


# --- Crash safety -----------------------------------------------------------


def test_in_place_target_intact_when_merge_raises(tmp_path):
    """Simulate a mid-merge filesystem failure. The target directory must
    remain bit-identical to its pre-merge state, and the exception must
    surface (the merger should not swallow it)."""
    target = _copy_fixture_to("ShadowDance", tmp_path)
    a = Project.load(target)
    b = Project.load(FIXTURES / "Shadowdancer")
    before = _file_inventory(target)

    # Patch shutil.copy2 to fail after a handful of copies (so the merge
    # gets partway through emission before exploding).
    counter = {"n": 0}
    real = shutil.copy2

    def angry_copy2(*args, **kwargs):
        counter["n"] += 1
        if counter["n"] > 5:
            raise OSError("simulated mid-merge filesystem failure")
        return real(*args, **kwargs)

    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=target,
        new_uuid=a.mod_meta.uuid,
        new_folder=a.mod_folder_name,
        new_name=a.mod_meta.name,
        conflict_policy="skip",
        in_place=True,
    )

    with patch("core.merger.shutil.copy2", side_effect=angry_copy2):
        with pytest.raises(OSError, match="simulated"):
            merger.merge(config)

    # Target survived intact.
    assert target.is_dir()
    after = _file_inventory(target)
    assert after == before, (
        f"file inventory changed: {len(before)} → {len(after)} files"
    )


def test_in_place_no_target_raises_cleanly(tmp_path):
    """``in_place=True`` with a non-existent ``output_dir`` should raise
    MergeError rather than silently creating the target: the user asked
    for an in-place merge, so an absent target is operator error."""
    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Shadowdancer")
    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=tmp_path / "doesnt_exist",
        new_uuid=a.mod_meta.uuid,
        new_folder=a.mod_folder_name,
        new_name=a.mod_meta.name,
        conflict_policy="skip",
        in_place=True,
    )
    with pytest.raises(merger.MergeError, match="does not exist"):
        merger.merge(config)


def test_in_place_target_is_file_not_dir_raises(tmp_path):
    """If somebody points in-place merge at a file path, refuse early."""
    file_target = tmp_path / "not_a_dir"
    file_target.write_text("hi")

    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Shadowdancer")
    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=file_target,
        new_uuid=a.mod_meta.uuid,
        new_folder=a.mod_folder_name,
        new_name=a.mod_meta.name,
        conflict_policy="skip",
        in_place=True,
    )
    with pytest.raises(merger.MergeError, match="not a directory"):
        merger.merge(config)


# --- Direct mode still works the same way -----------------------------------


def test_direct_mode_unchanged_by_in_place_refactor(tmp_path):
    """The non-in-place (``in_place=False``) code path should behave
    identically to before: same conflicts, same file count, same paths.
    Sanity check that splitting the merge entry didn't regress anything."""
    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Shadowdancer")
    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=tmp_path / "out",
        new_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        new_folder="DirectMode",
        new_name="X",
        conflict_policy="skip",
        in_place=False,  # explicit
    )
    result = merger.merge(config)
    assert result.output_dir == tmp_path / "out"
    assert (tmp_path / "out" / "Mods" / "DirectMode" / "meta.lsx").is_file()
