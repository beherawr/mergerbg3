"""Tests for ``core.lsx`` against the real example projects."""

from __future__ import annotations

import pytest

from core import lsx
from .helpers import all_lsx


# --- Parsing -----------------------------------------------------------------


@pytest.mark.parametrize("path", all_lsx(), ids=lambda p: str(p.relative_to(p.parents[6])))
def test_parses_real_file(path):
    """Every .lsx file in the fixtures parses without error."""
    doc = lsx.parse_file(path)
    assert doc.version.major  # always set
    assert len(doc.regions) >= 1


def test_parses_meta_lsx_mod_identity():
    """Mods/<Mod>/meta.lsx has the canonical mod identity in
    region=Config / node=root / children / node=ModuleInfo."""
    path = next(
        p for p in all_lsx()
        if p.name == "meta.lsx" and "Mods" in p.parts and "Projects" not in p.parts
    )
    doc = lsx.parse_file(path)
    config = doc.region("Config")
    assert config is not None
    root = config.root_node
    assert root.id == "root"

    # Find the ModuleInfo node.
    module_info = None
    for child_wrap in root.children:
        if child_wrap.id == "ModuleInfo":
            module_info = child_wrap
            break
    assert module_info is not None

    # The Folder attribute must exist. The Toolkit convention is
    # ``Name_UUID`` (with an underscore separator), but non-Toolkit-pipeline
    # mods sometimes use just ``Name`` (e.g. Bloodfang has Folder="BloodFang").
    # We just require non-empty.
    folder = module_info.attr_value("Folder")
    assert folder is not None
    assert folder
    uuid_val = module_info.attr_value("UUID")
    assert uuid_val is not None
    assert len(uuid_val) == 36


def test_parses_project_meta_lsx():
    """Projects/<Mod>/meta.lsx is a different schema: region=MetaData."""
    path = next(
        p for p in all_lsx()
        if p.name == "meta.lsx" and "Projects" in p.parts
    )
    doc = lsx.parse_file(path)
    metadata = doc.region("MetaData")
    assert metadata is not None
    root = metadata.root_node
    assert root.attr_value("Module") is not None  # points at the mod UUID
    assert root.attr_value("UUID") is not None  # project's own UUID


def test_dependency_extraction():
    """Mods/<Mod>/meta.lsx → ModuleShortDesc children list dependencies."""
    path = next(
        p for p in all_lsx()
        if p.name == "meta.lsx" and "Mods" in p.parts and "Projects" not in p.parts
    )
    doc = lsx.parse_file(path)
    config = doc.region("Config")
    root = config.root_node

    deps_node = None
    for child in root.children:
        if child.id == "Dependencies":
            deps_node = child
            break
    assert deps_node is not None

    # Both fixture projects depend on GustavX.
    dep_uuids: list[str] = []
    for short_desc in deps_node.children_by_id("ModuleShortDesc"):
        u = short_desc.attr_value("UUID")
        if u:
            dep_uuids.append(u)
    assert "cb555efe-2d9e-131f-8195-a89329d218ea" in dep_uuids  # GustavX


# --- Walking & traversal -----------------------------------------------------


def test_walk_visits_all_descendants():
    """node.walk() yields self plus every descendant, pre-order."""
    leaf = lsx.Node(id="leaf")
    mid = lsx.Node(id="mid", children=[leaf])
    root = lsx.Node(id="root", children=[mid])
    ids = [n.id for n in root.walk()]
    assert ids == ["root", "mid", "leaf"]


def test_translated_string_attribute_uses_handle_not_value():
    """TranslatedString attrs serialize with handle+version, not value."""
    a = lsx.Attribute(
        id="DisplayName", type="TranslatedString",
        handle="h2db009beg91d6g310eg03c0g9e0885029fce", version="2",
    )
    elem = a.to_xml()
    assert elem.get("handle") == "h2db009beg91d6g310eg03c0g9e0885029fce"
    assert elem.get("version") == "2"
    assert elem.get("value") is None


def test_fixedstring_attribute_uses_value():
    a = lsx.Attribute(id="UUID", type="FixedString", value="abc-123")
    elem = a.to_xml()
    assert elem.get("value") == "abc-123"
    assert elem.get("handle") is None


# --- Defensive parsing -------------------------------------------------------


def test_rejects_non_save_root():
    with pytest.raises(lsx.LsxParseError, match="expected root <save>"):
        lsx.parse_bytes(b"<foo/>")


def test_rejects_node_without_id():
    bad = b'<save><version major="4" minor="0" revision="0" build="0"/>'\
          b'<region id="X"><node/></region></save>'
    with pytest.raises(lsx.LsxParseError, match="missing id"):
        lsx.parse_bytes(bad)


def test_strips_and_preserves_bom():
    """A BOM in the input shouldn't break parsing, and should be preserved
    on write (so a BOMmed file round-trips byte-for-byte)."""
    body = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<save><version major="4" minor="0" revision="0" build="0"/>'
        b'<region id="X"><node id="root"/></region></save>'
    )
    doc = lsx.parse_bytes(b"\xef\xbb\xbf" + body)
    assert doc.had_bom is True
    out = doc.to_xml_bytes()
    assert out.startswith(b"\xef\xbb\xbf")


def test_preserves_lslib_meta_on_version():
    """LSLib stores serialization options in lslib_meta on <version>; if we
    dropped it, files in the [PAK]_UI/_merged style would corrupt on round-trip."""
    body = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<save><version major="4" minor="0" revision="8" build="2" '
        b'lslib_meta="v1,bswap_guids"/>'
        b'<region id="X"><node id="root"/></region></save>'
    )
    doc = lsx.parse_bytes(body)
    assert doc.version.extra.get("lslib_meta") == "v1,bswap_guids"
    out = doc.to_xml_bytes()
    assert b'lslib_meta="v1,bswap_guids"' in out
