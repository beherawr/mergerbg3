"""Tests for the structured keyed-list LSX merge.

The reported bug: ``.lsx`` files like ``Progressions.lsx`` and
``SpellLists.lsx`` were getting copied verbatim ("A wins, B dropped")
when both mods provided one. They're keyed-list LSX files — the right
behavior is to union the children by UUID.

These tests are self-contained (build tiny in-memory projects via
``tmp_path``) so they pass on CI runners that don't have the private
mod fixtures available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import merger, project as proj_mod
from core.project import FileCategory, Project


# ---------------------------------------------------------------------------
# Helpers — build minimal mod projects on disk.
# ---------------------------------------------------------------------------


def _write_meta_lsx(path: Path, uuid: str, folder: str, name: str, author: str = "Test") -> None:
    """Write a minimal Mods/<X>/meta.lsx that the merger can load."""
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
          <attribute id="Author" type="LSString" value="{author}"/>
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


def _write_progressions_lsx(path: Path, entries: list[tuple[str, str]]) -> None:
    """Write a Progressions.lsx with ``(uuid, name)`` entries.

    Real progressions.lsx has many more attributes per entry, but the
    merger only cares about identity (UUID) and structural equality —
    these stub entries exercise the union machinery without needing the
    full BG3 progression schema.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    entry_xml = "".join(
        f"""        <node id="Progression">
          <attribute id="UUID" type="guid" value="{uuid}"/>
          <attribute id="Name" type="FixedString" value="{name}"/>
        </node>
"""
        for uuid, name in entries
    )
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<save>
  <version major="4" minor="8" revision="0" build="500"/>
  <region id="Progressions">
    <node id="root">
      <children>
{entry_xml}      </children>
    </node>
  </region>
</save>
""",
        encoding="utf-8",
    )


def _make_mod(
    workspace: Path,
    folder: str,
    uuid: str,
    name: str,
    progression_entries: list[tuple[str, str]] | None = None,
) -> Path:
    """Build a self-contained mod project at workspace/<folder>/ with a
    meta.lsx and (optionally) a Progressions.lsx. Returns the project
    root path that Project.load can ingest."""
    project_root = workspace / folder
    _write_meta_lsx(project_root / "Mods" / folder / "meta.lsx",
                    uuid=uuid, folder=folder, name=name)
    if progression_entries is not None:
        _write_progressions_lsx(
            project_root / "Public" / folder / "Progressions" / "Progressions.lsx",
            progression_entries,
        )
    return project_root


# ---------------------------------------------------------------------------
# The bug case the user reported.
# ---------------------------------------------------------------------------


def test_progressions_lsx_unions_entries_when_both_mods_provide_one(tmp_path):
    """When both mods have a Progressions.lsx, the merged output must
    contain every entry from both — not just mod A's version.
    Reported bug from user feedback (2026-05): 'progressions.lsx don't
    get merged'."""
    workspace = tmp_path / "ws"
    out = tmp_path / "out"

    a_root = _make_mod(
        workspace, "ModA", uuid="aaaaaaaa-1111-1111-1111-111111111111",
        name="Mod A",
        progression_entries=[
            ("11111111-aaaa-bbbb-cccc-000000000001", "A_Progression_1"),
            ("11111111-aaaa-bbbb-cccc-000000000002", "A_Progression_2"),
        ],
    )
    b_root = _make_mod(
        workspace, "ModB", uuid="bbbbbbbb-2222-2222-2222-222222222222",
        name="Mod B",
        progression_entries=[
            ("22222222-aaaa-bbbb-cccc-000000000003", "B_Progression_3"),
            ("22222222-aaaa-bbbb-cccc-000000000004", "B_Progression_4"),
        ],
    )

    a = Project.load(a_root)
    b = Project.load(b_root)

    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=out,
        new_uuid="cccccccc-3333-3333-3333-333333333333",
        new_folder="Merged",
        new_name="Merged",
        conflict_policy="skip",
    )
    merger.merge(config)

    merged_path = out / "Public" / "Merged" / "Progressions" / "Progressions.lsx"
    assert merged_path.exists(), "merged Progressions.lsx should be emitted"

    text = merged_path.read_text(encoding="utf-8")
    # Every entry from both inputs should be present after the union.
    assert "A_Progression_1" in text
    assert "A_Progression_2" in text
    assert "B_Progression_3" in text
    assert "B_Progression_4" in text


def test_progressions_lsx_duplicate_uuid_keeps_a(tmp_path):
    """When both mods define the SAME UUID with different content, A wins
    silently (matches the user's preference). The merged file still has
    every other entry from both inputs, but the colliding UUID's content
    is A's version."""
    workspace = tmp_path / "ws"
    out = tmp_path / "out"

    shared_uuid = "11111111-aaaa-bbbb-cccc-000000000099"

    _make_mod(
        workspace, "ModA", uuid="aaaaaaaa-1111-1111-1111-111111111111",
        name="Mod A",
        progression_entries=[
            (shared_uuid, "A_version_of_shared"),
            ("11111111-aaaa-bbbb-cccc-000000000001", "A_unique"),
        ],
    )
    _make_mod(
        workspace, "ModB", uuid="bbbbbbbb-2222-2222-2222-222222222222",
        name="Mod B",
        progression_entries=[
            (shared_uuid, "B_version_of_shared"),  # same UUID, different name
            ("22222222-aaaa-bbbb-cccc-000000000002", "B_unique"),
        ],
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

    text = (out / "Public" / "Merged" / "Progressions" / "Progressions.lsx").read_text(
        encoding="utf-8"
    )
    # A's unique entry: present.
    assert "A_unique" in text
    # B's unique entry: present (B-only entry, appended).
    assert "B_unique" in text
    # The shared UUID resolves to A's content (a_wins policy).
    assert "A_version_of_shared" in text
    assert "B_version_of_shared" not in text
    # And we logged the collision so the user can review it.
    collision_kinds = [c.kind for c in result.conflicts]
    assert any("bank_lsx_entry_conflict" in k or "entry_conflict" in k
               for k in collision_kinds), (
        f"expected an entry-conflict in {collision_kinds}"
    )


def test_progressions_lsx_byte_identical_dedupes_silently(tmp_path):
    """If both mods have an identical Progressions.lsx (perhaps copied
    from a shared template), the merged output should just have one copy
    — no conflict, no duplicated entries."""
    workspace = tmp_path / "ws"
    out = tmp_path / "out"

    same_entries = [
        ("11111111-aaaa-bbbb-cccc-000000000001", "Same_1"),
        ("11111111-aaaa-bbbb-cccc-000000000002", "Same_2"),
    ]
    _make_mod(workspace, "ModA",
              uuid="aaaaaaaa-1111-1111-1111-111111111111",
              name="Mod A", progression_entries=same_entries)
    _make_mod(workspace, "ModB",
              uuid="bbbbbbbb-2222-2222-2222-222222222222",
              name="Mod B", progression_entries=same_entries)

    a = Project.load(workspace / "ModA")
    b = Project.load(workspace / "ModB")
    result = merger.merge(merger.MergeConfig(
        inputs=[a, b],
        output_dir=out,
        new_uuid="cccccccc-3333-3333-3333-333333333333",
        new_folder="Merged", new_name="Merged",
        conflict_policy="skip",
    ))

    text = (out / "Public" / "Merged" / "Progressions" / "Progressions.lsx").read_text(
        encoding="utf-8"
    )
    # Both Same_1 entries deduped to one occurrence. (Counting raw text
    # is rough but adequate — if dedup failed we'd see two of each.)
    assert text.count("Same_1") == 1
    assert text.count("Same_2") == 1
    # Byte-identical → no entry conflict raised.
    assert not any(
        "entry_conflict" in c.kind for c in result.conflicts
    ), [c.kind for c in result.conflicts]


# ---------------------------------------------------------------------------
# Single-input case: the file goes through unchanged (still parses + remaps).
# ---------------------------------------------------------------------------


def test_progressions_lsx_only_in_one_mod_passes_through(tmp_path):
    """When only one mod has a Progressions.lsx, it's emitted by the
    single-input path (parse + remap + write). This already worked
    before the structured-merge change — verifying we didn't regress it.
    """
    workspace = tmp_path / "ws"
    out = tmp_path / "out"

    _make_mod(
        workspace, "ModA",
        uuid="aaaaaaaa-1111-1111-1111-111111111111", name="Mod A",
        progression_entries=[
            ("11111111-aaaa-bbbb-cccc-000000000001", "A_only_1"),
        ],
    )
    # Mod B has meta but no Progressions.lsx.
    _make_mod(
        workspace, "ModB",
        uuid="bbbbbbbb-2222-2222-2222-222222222222", name="Mod B",
        progression_entries=None,
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

    merged_path = out / "Public" / "Merged" / "Progressions" / "Progressions.lsx"
    assert merged_path.exists()
    assert "A_only_1" in merged_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Categorization sanity: confirms generic LSX still lands in BANK_LSX,
# which is what the merger routes through the new structured branch.
# ---------------------------------------------------------------------------


def test_progressions_lsx_categorized_as_bank_lsx(tmp_path):
    """The merger relies on Progressions.lsx ending up in BANK_LSX
    (the catch-all for keyed-list LSX files not matching a more specific
    rule). If somebody later adds a dedicated Progressions category,
    they need to remember to put it in
    ``_STRUCTURED_LSX_MERGE_CATEGORIES`` or this test will catch it."""
    workspace = tmp_path / "ws"
    _make_mod(
        workspace, "ModA",
        uuid="aaaaaaaa-1111-1111-1111-111111111111", name="Mod A",
        progression_entries=[("11111111-aaaa-bbbb-cccc-000000000001", "X")],
    )
    p = Project.load(workspace / "ModA")
    prog_files = [f for f in p.files if f.path.name == "Progressions.lsx"]
    assert len(prog_files) == 1
    assert prog_files[0].category == FileCategory.BANK_LSX, (
        f"expected BANK_LSX (the catch-all that the structured merger "
        f"handles), got {prog_files[0].category}"
    )
    # And BANK_LSX is in the structured-merge set.
    assert FileCategory.BANK_LSX in merger._STRUCTURED_LSX_MERGE_CATEGORIES
