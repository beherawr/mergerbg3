"""Tests for ``core.meta`` against the real example projects."""

from __future__ import annotations

import re

import pytest

from core import meta, lsx
from .helpers import FIXTURES


# --- Parsing real files ------------------------------------------------------


def test_parse_shadow_dance_mod_meta():
    """ShadowDance's Mods/<Folder>/meta.lsx round-trips into a ModMeta."""
    sd_mod = next((FIXTURES / "ShadowDance").glob("Mods/*/meta.lsx"))
    m = meta.parse_mod_meta_file(sd_mod)
    assert m.name == "Shadow Dance"
    assert m.uuid == "50deb7b5-8734-7111-cb00-6682390ee00c"
    assert m.folder == "ShadowDance_50deb7b5-8734-7111-cb00-6682390ee00c"
    assert m.author == "For_Kiramay"
    # GustavX is the only dependency.
    assert len(m.dependencies) == 1
    assert m.dependencies[0].name == "GustavX"
    assert m.dependencies[0].uuid == "cb555efe-2d9e-131f-8195-a89329d218ea"
    # Two SE script registrations, both with a HardcoreOnly parameter.
    assert len(m.scripts) == 2
    for script in m.scripts:
        assert len(script.parameters) == 1
        assert script.parameters[0].map_key == "HardcoreOnly"


def test_parse_shadowdancer_mod_meta():
    """Shadowdancer's mod meta has no SE scripts and an empty author."""
    sdancer_mod = next((FIXTURES / "Shadowdancer").glob("Mods/*/meta.lsx"))
    m = meta.parse_mod_meta_file(sdancer_mod)
    assert m.name == "Shadowdancer"
    assert m.author == ""
    assert m.scripts == []
    assert len(m.dependencies) == 1


def test_parse_project_meta():
    """Projects/<Folder>/meta.lsx is a separate file with its own UUID
    that points at the mod by Module=..."""
    for project_dir in [FIXTURES / "ShadowDance", FIXTURES / "Shadowdancer"]:
        path = next(project_dir.glob("Projects/*/meta.lsx"))
        m = meta.parse_project_meta_file(path)
        assert m.uuid  # project UUID
        assert m.module  # mod UUID it references
        assert m.uuid != m.module  # they are not the same UUID
        assert m.name  # display name


def test_parse_rejects_swapped_files():
    """Handing parse_mod_meta a Projects/-style file (or vice versa) should
    raise rather than silently produce garbage."""
    sd_project_meta = next((FIXTURES / "ShadowDance").glob("Projects/*/meta.lsx"))
    with pytest.raises(ValueError, match="region id='Config'"):
        meta.parse_mod_meta_file(sd_project_meta)

    sd_mod_meta = next((FIXTURES / "ShadowDance").glob("Mods/*/meta.lsx"))
    with pytest.raises(ValueError, match="region id='MetaData'"):
        meta.parse_project_meta_file(sd_mod_meta)


# --- Round-trip --------------------------------------------------------------


def test_mod_meta_roundtrip_preserves_semantics():
    """Parse → build → reparse should give back the same ModMeta values.

    We compare field-by-field rather than byte-for-byte because Larian's
    attribute ordering inside ModuleInfo isn't strictly fixed (the Toolkit
    sometimes shuffles them).
    """
    sd_mod = next((FIXTURES / "ShadowDance").glob("Mods/*/meta.lsx"))
    original = meta.parse_mod_meta_file(sd_mod)
    rebuilt_doc = meta.build_mod_meta_doc(original)
    rebuilt_bytes = rebuilt_doc.to_xml_bytes()
    reparsed = meta.parse_mod_meta(lsx.parse_bytes(rebuilt_bytes))

    assert reparsed.uuid == original.uuid
    assert reparsed.folder == original.folder
    assert reparsed.name == original.name
    assert reparsed.author == original.author
    assert len(reparsed.dependencies) == len(original.dependencies)
    assert reparsed.dependencies[0].uuid == original.dependencies[0].uuid
    assert len(reparsed.scripts) == len(original.scripts)
    if reparsed.scripts:
        assert reparsed.scripts[0].uuid == original.scripts[0].uuid
        assert len(reparsed.scripts[0].parameters) == len(
            original.scripts[0].parameters
        )


def test_project_meta_roundtrip():
    sd_proj = next((FIXTURES / "ShadowDance").glob("Projects/*/meta.lsx"))
    original = meta.parse_project_meta_file(sd_proj)
    rebuilt_doc = meta.build_project_meta_doc(original)
    rebuilt_bytes = rebuilt_doc.to_xml_bytes()
    reparsed = meta.parse_project_meta(lsx.parse_bytes(rebuilt_bytes))

    assert reparsed.uuid == original.uuid
    assert reparsed.module == original.module
    assert reparsed.name == original.name


# --- Identity generation -----------------------------------------------------


def test_generate_uuid_format():
    """Generated UUIDs are RFC 4122 v4, dashed, lower-case."""
    u = meta.generate_uuid()
    assert re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        u,
    )


def test_generate_uuid_is_unique():
    """Sanity: each call produces a different UUID."""
    seen = {meta.generate_uuid() for _ in range(100)}
    assert len(seen) == 100


# --- Unions ------------------------------------------------------------------


def test_union_dependencies_dedupes_gustavx():
    """The expected real-world case: both inputs depend on GustavX.
    The merged list has it exactly once."""
    sd_mod = next((FIXTURES / "ShadowDance").glob("Mods/*/meta.lsx"))
    sdancer_mod = next((FIXTURES / "Shadowdancer").glob("Mods/*/meta.lsx"))
    a = meta.parse_mod_meta_file(sd_mod)
    b = meta.parse_mod_meta_file(sdancer_mod)

    merged_deps = meta.union_dependencies(a.dependencies, b.dependencies)
    assert len(merged_deps) == 1
    assert merged_deps[0].uuid == "cb555efe-2d9e-131f-8195-a89329d218ea"


def test_union_scripts_dedupes_by_uuid():
    s1 = meta.ScriptRegistration(uuid="aaa-111")
    s2 = meta.ScriptRegistration(uuid="bbb-222")
    s1_dup = meta.ScriptRegistration(uuid="aaa-111")
    merged = meta.union_scripts([s1, s2], [s1_dup])
    assert len(merged) == 2
    assert [s.uuid for s in merged] == ["aaa-111", "bbb-222"]


def test_union_conflicts_preserves_order_and_dedupes():
    merged = meta.union_conflicts(
        ["uuid-a", "uuid-b"],
        ["uuid-b", "uuid-c"],
    )
    assert merged == ["uuid-a", "uuid-b", "uuid-c"]


# --- merge_mod_meta (full merge) --------------------------------------------


def test_merge_mod_meta_keeps_se_scripts_from_either_input():
    """The merged mod's Scripts list is the union — ShadowDance's two scripts
    are kept even though Shadowdancer registers none."""
    sd_mod = next((FIXTURES / "ShadowDance").glob("Mods/*/meta.lsx"))
    sdancer_mod = next((FIXTURES / "Shadowdancer").glob("Mods/*/meta.lsx"))
    a = meta.parse_mod_meta_file(sd_mod)
    b = meta.parse_mod_meta_file(sdancer_mod)

    merged = meta.merge_mod_meta(
        a, b,
        new_uuid=meta.generate_uuid(),
        new_folder="ShadowDance_Plus_Shadowdancer_Merged",
        new_name="Shadow Dance + Shadowdancer (merged)",
        new_author="Test Suite",
    )
    assert len(merged.scripts) == 2  # both of ShadowDance's
    assert merged.author == "Test Suite"
    assert merged.name == "Shadow Dance + Shadowdancer (merged)"
    assert merged.uuid != a.uuid
    assert merged.uuid != b.uuid


def test_merged_meta_writes_and_reparses_cleanly():
    """End-to-end: produce a merged meta, serialize it, parse it back — fields
    survive the round-trip."""
    sd_mod = next((FIXTURES / "ShadowDance").glob("Mods/*/meta.lsx"))
    sdancer_mod = next((FIXTURES / "Shadowdancer").glob("Mods/*/meta.lsx"))
    a = meta.parse_mod_meta_file(sd_mod)
    b = meta.parse_mod_meta_file(sdancer_mod)

    new_uuid = meta.generate_uuid()
    merged = meta.merge_mod_meta(
        a, b,
        new_uuid=new_uuid,
        new_folder="Merged_Test",
        new_name="Merged Test",
    )

    rebuilt_bytes = meta.build_mod_meta_doc(merged).to_xml_bytes()
    reparsed = meta.parse_mod_meta(lsx.parse_bytes(rebuilt_bytes))

    assert reparsed.uuid == new_uuid
    assert reparsed.name == "Merged Test"
    assert reparsed.folder == "Merged_Test"
    assert len(reparsed.dependencies) == 1
    assert len(reparsed.scripts) == 2
