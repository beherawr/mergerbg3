"""Tests for the LSX union merge helper and the GUI metadata path in the merger.

The LSX-level union tests are pure (no divine.exe needed). The merger-level
tests mock the Divine wrapper so we can exercise the LSF→union→LSF path
end-to-end on Linux.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core import lsx, lsx_merge, merger
from core.project import FileCategory, Project


# --- Synthetic LSX builders (used across tests) -----------------------------


def _attr(id: str, type: str, value: str) -> lsx.Attribute:
    return lsx.Attribute(id=id, type=type, value=value)


def _widget(uuid: str, path: str) -> lsx.Node:
    """A single UIWidget node."""
    return lsx.Node(id="UIWidget", attributes=[
        _attr("UUID", "guid", uuid),
        _attr("Path", "LSString", path),
    ])


def _doc_with_widgets(widgets: list[lsx.Node]) -> lsx.LsxDocument:
    """An LsxDocument shaped like a typical GUI metadata.lsf."""
    return lsx.LsxDocument(
        version=lsx.Version(major="4", minor="8", revision="0", build="500"),
        regions=[
            lsx.Region(
                id="UIWidgets",
                root_node=lsx.Node(id="root", children=widgets),
            ),
        ],
    )


# --- Pure LSX union tests ---------------------------------------------------


def test_union_appends_b_only_entries():
    a = _doc_with_widgets([_widget("uuid-X", "A/X.swf")])
    b = _doc_with_widgets([_widget("uuid-Y", "B/Y.swf")])
    result = lsx_merge.union_documents(a, b)
    out = result.document.regions[0].root_node.children
    assert len(out) == 2
    assert {w.attr_value("UUID") for w in out} == {"uuid-X", "uuid-Y"}
    assert result.added_from_b == 1
    assert result.deduped == 0
    assert result.conflicts == []


def test_union_silently_dedupes_byte_identical_entries():
    """Two mods that both register the same widget (same UUID + same body)
    shouldn't produce a conflict: just a silent dedup."""
    shared = _widget("uuid-shared", "shared/x.swf")
    a = _doc_with_widgets([shared])
    b = _doc_with_widgets([_widget("uuid-shared", "shared/x.swf")])  # identical
    result = lsx_merge.union_documents(a, b)
    out = result.document.regions[0].root_node.children
    assert len(out) == 1
    assert result.deduped == 1
    assert result.conflicts == []


def test_union_a_wins_on_conflicting_uuid():
    """Same UUID, different body → conflict; A's content kept by default."""
    a = _doc_with_widgets([_widget("uuid-X", "A/X.swf")])
    b = _doc_with_widgets([_widget("uuid-X", "B/X_OVERRIDDEN.swf")])
    result = lsx_merge.union_documents(a, b)
    out = result.document.regions[0].root_node.children
    assert len(out) == 1
    assert out[0].attr_value("Path") == "A/X.swf"
    assert len(result.conflicts) == 1
    assert result.conflicts[0].resolution == "kept_a"


def test_union_b_wins_policy_overrides_a():
    a = _doc_with_widgets([_widget("uuid-X", "A/X.swf")])
    b = _doc_with_widgets([_widget("uuid-X", "B/X_OVERRIDDEN.swf")])
    result = lsx_merge.union_documents(a, b, conflict_policy="b_wins")
    out = result.document.regions[0].root_node.children
    assert out[0].attr_value("Path") == "B/X_OVERRIDDEN.swf"
    assert result.conflicts[0].resolution == "kept_b"


def test_union_fail_policy_raises_on_conflict():
    a = _doc_with_widgets([_widget("uuid-X", "A/X.swf")])
    b = _doc_with_widgets([_widget("uuid-X", "B/X_OVERRIDDEN.swf")])
    with pytest.raises(lsx_merge.UnionError):
        lsx_merge.union_documents(a, b, conflict_policy="fail")


def test_union_preserves_order_a_then_new_b():
    """Determinism: A's widgets keep their order; new B widgets get
    appended after. Important for git diffs and Toolkit stability."""
    a = _doc_with_widgets([
        _widget("uuid-A1", "A/1.swf"),
        _widget("uuid-A2", "A/2.swf"),
        _widget("uuid-A3", "A/3.swf"),
    ])
    b = _doc_with_widgets([
        _widget("uuid-B1", "B/1.swf"),
        _widget("uuid-A2", "A/2.swf"),  # identical to A's
        _widget("uuid-B2", "B/2.swf"),
    ])
    result = lsx_merge.union_documents(a, b)
    out = result.document.regions[0].root_node.children
    assert [w.attr_value("UUID") for w in out] == [
        "uuid-A1", "uuid-A2", "uuid-A3", "uuid-B1", "uuid-B2",
    ]


def test_union_appends_b_only_region_whole():
    """B introduces a region A doesn't have: the merged doc keeps both."""
    a = _doc_with_widgets([_widget("uuid-X", "A/X.swf")])
    b = lsx.LsxDocument(
        version=lsx.Version(major="4", minor="8", revision="0", build="500"),
        regions=[
            lsx.Region(id="DifferentRegion",
                       root_node=lsx.Node(id="root", children=[
                           _widget("uuid-Z", "B/Z.swf"),
                       ])),
        ],
    )
    result = lsx_merge.union_documents(a, b)
    assert len(result.document.regions) == 2
    assert {r.id for r in result.document.regions} == {"UIWidgets", "DifferentRegion"}


def test_union_uses_mapkey_when_no_uuid():
    """Some LSX node types use MapKey instead of UUID for identity."""
    def mk(key, value):
        return lsx.Node(id="ListEntry", attributes=[
            _attr("MapKey", "FixedString", key),
            _attr("Value", "LSString", value),
        ])
    a = _doc_with_widgets([mk("alpha", "valueA")])
    b = _doc_with_widgets([
        mk("alpha", "valueA"),   # identical → dedupe
        mk("beta",  "valueB"),    # new      → append
    ])
    result = lsx_merge.union_documents(a, b)
    keys = [c.attr_value("MapKey") for c in result.document.regions[0].root_node.children]
    assert keys == ["alpha", "beta"]
    assert result.deduped == 1


def test_union_does_not_mutate_inputs():
    """Union must deep-copy so the caller's input docs are pristine
    after the merge. Tests this by mutating an input's child afterwards
    and confirming the merged doc didn't change."""
    a_widget = _widget("uuid-A", "A/x.swf")
    a = _doc_with_widgets([a_widget])
    b = _doc_with_widgets([_widget("uuid-B", "B/y.swf")])
    result = lsx_merge.union_documents(a, b)

    # Mutate A's widget post-merge.
    a_widget.attributes[1] = _attr("Path", "LSString", "MUTATED")
    merged = result.document.regions[0].root_node.children[0]
    assert merged.attr_value("Path") == "A/x.swf"  # didn't follow the mutation


# --- Merger-level GUI metadata path (with mocked divine) --------------------


@pytest.fixture
def mock_divine(tmp_path):
    """A MagicMock that implements lsf_to_lsx + lsx_to_lsf by treating
    .lsf as already-LSX. Lets us exercise the merger's GUI metadata path
    end-to-end without a real divine.exe."""
    divine = MagicMock()

    def lsf_to_lsx(src, dst):
        # In tests our "LSF" files are actually LSX text. Just copy.
        shutil.copy2(src, dst)

    def lsx_to_lsf(src, dst):
        shutil.copy2(src, dst)

    divine.lsf_to_lsx.side_effect = lsf_to_lsx
    divine.lsx_to_lsf.side_effect = lsx_to_lsf
    return divine


def _write_gui_metadata_lsf(path: Path, widgets: list[lsx.Node]) -> None:
    """Write a 'GUI metadata.lsf' file as LSX bytes (mock divine treats
    these as LSF for the purposes of conversion)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = _doc_with_widgets(widgets)
    path.write_bytes(doc.to_xml_bytes())


def _make_project_with_gui_metadata(
    base: Path, folder_name: str, mod_uuid: str, widgets: list[lsx.Node],
) -> Path:
    """Build a minimal project layout with one GUI metadata.lsf and the
    bare minimum to be Project.load-able."""
    proj = base / folder_name
    mods_dir = proj / "Mods" / folder_name
    mods_dir.mkdir(parents=True)
    # Minimal mod meta.lsx so Project.load accepts it.
    (mods_dir / "meta.lsx").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<save>
  <version major="4" minor="8" revision="0" build="500"/>
  <region id="Config">
    <node id="root">
      <children>
        <node id="ModuleInfo">
          <attribute id="UUID" type="FixedString" value="{mod_uuid}"/>
          <attribute id="Folder" type="LSString" value="{folder_name}"/>
          <attribute id="Name" type="LSString" value="{folder_name}"/>
          <attribute id="Description" type="LSString" value=""/>
          <attribute id="Author" type="LSString" value="Test"/>
          <attribute id="Version64" type="int64" value="36028797018963968"/>
          <attribute id="MD5" type="LSString" value=""/>
          <attribute id="NumPlayers" type="uint8" value="4"/>
          <attribute id="PublishVersion" type="uint8" value="0"/>
          <attribute id="PublishHandle" type="uint64" value="0"/>
          <attribute id="FileSize" type="uint64" value="0"/>
          <attribute id="CharacterCreationLevelName" type="FixedString" value=""/>
          <attribute id="LobbyLevelName" type="FixedString" value=""/>
          <attribute id="MenuLevelName" type="FixedString" value=""/>
          <attribute id="MainMenuBackground" type="FixedString" value=""/>
          <attribute id="PhotoBooth" type="FixedString" value=""/>
          <attribute id="StartupLevelName" type="FixedString" value=""/>
          <children>
            <node id="Dependencies"/>
            <node id="Conflicts"/>
            <node id="TargetModes"/>
            <node id="Scripts"/>
          </children>
        </node>
      </children>
    </node>
  </region>
</save>""")
    # Write the GUI metadata.lsf (mock-LSF; really an LSX file).
    _write_gui_metadata_lsf(mods_dir / "GUI" / "metadata.lsf", widgets)
    return proj


def test_merger_unions_gui_metadata_when_divine_provided(tmp_path, mock_divine):
    """End-to-end through the merger: GUI metadata.lsf in both inputs
    is structurally merged, not just keep-A-discarded."""
    a_root = _make_project_with_gui_metadata(
        tmp_path / "a", "ModA_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        widgets=[_widget("uuid-A1", "A/widget1.swf")],
    )
    b_root = _make_project_with_gui_metadata(
        tmp_path / "b", "ModB_bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        widgets=[_widget("uuid-B1", "B/widget1.swf")],
    )
    a = Project.load(a_root)
    b = Project.load(b_root)

    out = tmp_path / "out"
    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=out,
        new_uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        new_folder="MergedABC",
        new_name="A+B",
        conflict_policy="skip",
        divine=mock_divine,
    )
    result = merger.merge(config)

    # The merged metadata.lsf should contain BOTH widgets.
    merged_lsf = out / "Mods" / "MergedABC" / "GUI" / "metadata.lsf"
    assert merged_lsf.is_file()
    merged_doc = lsx.parse_file(merged_lsf)
    widgets = merged_doc.regions[0].root_node.children
    uuids = sorted(w.attr_value("UUID") for w in widgets)
    assert uuids == ["uuid-A1", "uuid-B1"], (
        f"expected both A and B widgets in merged GUI metadata, got {uuids}"
    )

    # No conflict surfaced (these are different UUIDs).
    gui_conflicts = [c for c in result.conflicts
                     if c.kind in ("gui_widget_conflict", "gui_metadata_unmerged")]
    assert gui_conflicts == []


def test_merger_falls_back_when_divine_unavailable(tmp_path):
    """Without divine, the merger keeps A's metadata.lsf verbatim and
    surfaces a clear conflict so the user knows B's widgets were
    dropped (the current state the user was concerned about)."""
    a_root = _make_project_with_gui_metadata(
        tmp_path / "a", "ModA_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        widgets=[_widget("uuid-A1", "A/widget1.swf")],
    )
    b_root = _make_project_with_gui_metadata(
        tmp_path / "b", "ModB_bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        widgets=[_widget("uuid-B1", "B/widget1.swf")],
    )
    a = Project.load(a_root)
    b = Project.load(b_root)

    out = tmp_path / "out"
    config = merger.MergeConfig(
        inputs=[a, b],
        output_dir=out,
        new_uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        new_folder="MergedABC", new_name="A+B",
        conflict_policy="skip",
        divine=None,  # explicit: no divine available
    )
    result = merger.merge(config)

    # Should surface the unmerged-GUI-metadata conflict so the user is
    # aware B's widgets are missing from the output.
    gui_conflicts = [c for c in result.conflicts if c.kind == "gui_metadata_unmerged"]
    assert len(gui_conflicts) == 1
    assert gui_conflicts[0].resolution == "kept_a_copied_verbatim"


def test_merger_dedupes_byte_identical_gui_metadata(tmp_path, mock_divine):
    """If both inputs have a byte-identical GUI metadata.lsf, no
    conflict surfaces and the file is emitted once."""
    shared = [_widget("uuid-shared", "shared/x.swf")]
    a_root = _make_project_with_gui_metadata(
        tmp_path / "a", "ModA_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", widgets=shared,
    )
    b_root = _make_project_with_gui_metadata(
        tmp_path / "b", "ModB_bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        widgets=[_widget("uuid-shared", "shared/x.swf")],  # identical body
    )
    a = Project.load(a_root)
    b = Project.load(b_root)

    out = tmp_path / "out"
    config = merger.MergeConfig(
        inputs=[a, b], output_dir=out,
        new_uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        new_folder="MergedABC", new_name="A+B", conflict_policy="skip",
        divine=mock_divine,
    )
    result = merger.merge(config)

    # Single widget in the merged doc, no conflicts.
    merged_lsf = out / "Mods" / "MergedABC" / "GUI" / "metadata.lsf"
    merged_doc = lsx.parse_file(merged_lsf)
    assert len(merged_doc.regions[0].root_node.children) == 1

    gui_conflicts = [c for c in result.conflicts
                     if c.kind in ("gui_widget_conflict", "gui_metadata_unmerged")]
    assert gui_conflicts == []
