"""Tests for ``core.references`` against the real example projects."""

from __future__ import annotations

import pytest

from core import references
from core.project import Project
from core.references import IdKind, ReferenceIndex, find_clashes
from .helpers import FIXTURES


# --- ShadowDance: spell + Osiris mod indexing -------------------------------


@pytest.fixture(scope="module")
def shadow_dance_index() -> ReferenceIndex:
    p = Project.load(FIXTURES / "ShadowDance")
    return ReferenceIndex.build(p)


def test_shadow_dance_indexes_its_stat_names(shadow_dance_index):
    """Every stat entry in ShadowDance's .txt files appears as a definition."""
    defs = shadow_dance_index.defined_values(IdKind.STAT_NAME)
    # From .txt (prefixed forms):
    assert "Target_BackstabK" in defs
    assert "Shout_ShadowDanceK" in defs
    assert "BlockReactK" in defs
    assert "SeeInDaDarkK" in defs
    assert "INVISIBLEKira" in defs
    # From .stats (unprefixed forms: ShadowDance has BackstabK, ShadowDanceK):
    assert "BackstabK" in defs
    assert "ShadowDanceK" in defs


def test_shadow_dance_indexes_loca_handles(shadow_dance_index):
    """Localization handles defined by english.xml and referenced by stats."""
    defined = shadow_dance_index.defined_values(IdKind.LOCA_HANDLE)
    referenced = shadow_dance_index.referenced_values(IdKind.LOCA_HANDLE)

    # ShadowDance's english.xml defines 11 handles.
    assert len(defined) == 11

    # Each handle defined should also be referenced from at least one stats file
    # (since the mod's strings get used by the spells/statuses they're attached to).
    # Specifically, the spell display name handles are referenced from .txt.
    assert "h2db009beg91d6g310eg03c0g9e0885029fce" in defined
    assert "h2db009beg91d6g310eg03c0g9e0885029fce" in referenced


def test_shadow_dance_self_referential_stats_resolve(shadow_dance_index):
    """Shout_ShadowDanceK references INVISIBLEKira via ApplyStatus(). The
    reference should be both indexed AND resolve (no orphan)."""
    referenced = shadow_dance_index.referenced_values(IdKind.STAT_NAME)
    defined = shadow_dance_index.defined_values(IdKind.STAT_NAME)
    assert "INVISIBLEKira" in referenced
    assert "INVISIBLEKira" in defined  # resolves within the same project


def test_shadow_dance_dependency_uuid_is_a_reference(shadow_dance_index):
    """The GustavX dependency UUID is referenced (from meta.lsx) but
    not defined (it belongs to another mod). Shows up as an orphan ref,
    which is fine: orphans are warnings, not errors."""
    gustavx = "cb555efe-2d9e-131f-8195-a89329d218ea"
    assert gustavx in shadow_dance_index.referenced_values(IdKind.UUID)
    assert gustavx not in shadow_dance_index.defined_values(IdKind.UUID)

    orphans = {e.value for e in shadow_dance_index.orphan_references(IdKind.UUID)}
    assert gustavx in orphans


def test_shadow_dance_mod_uuid_is_a_definition(shadow_dance_index):
    """The mod's own UUID is defined by meta.lsx."""
    mod_uuid = "50deb7b5-8734-7111-cb00-6682390ee00c"
    assert mod_uuid in shadow_dance_index.defined_values(IdKind.UUID)


def test_shadow_dance_osiris_references_picked_up(shadow_dance_index):
    """The GiveShadSpellK goal calls AddSpell(_Player, "Shout_ShadowDanceK",...).
    The string ref must be indexed."""
    refs = shadow_dance_index.referenced_values(IdKind.STAT_NAME)
    assert "Shout_ShadowDanceK" in refs


def test_shadow_dance_summary_renders(shadow_dance_index):
    summary = shadow_dance_index.summary()
    assert "uuid" in summary
    assert "stat_name" in summary
    assert "loca_handle" in summary


# --- Shadowdancer: weapon + visual mod indexing -----------------------------


@pytest.fixture(scope="module")
def shadowdancer_index() -> ReferenceIndex:
    p = Project.load(FIXTURES / "Shadowdancer")
    return ReferenceIndex.build(p)


def test_shadowdancer_indexes_weapon_stat(shadowdancer_index):
    defs = shadowdancer_index.defined_values(IdKind.STAT_NAME)
    assert "SDancerBlade" in defs


def test_shadowdancer_references_wpn_dagger_as_orphan(shadowdancer_index):
    """The weapon inherits from WPN_Dagger, which belongs to the base game.
    It's an orphan reference (no definition within the mod): correct behavior."""
    orphans = {e.value for e in shadowdancer_index.orphan_references(IdKind.STAT_NAME)}
    assert "WPN_Dagger" in orphans


def test_shadowdancer_root_template_uuid_referenced_from_stats(shadowdancer_index):
    """Weapon.txt has data "RootTemplate" "d21296e6-...". The RT UUID is
    a reference; the RT file itself sits in RootTemplates/d21296e6-...lsf
    (binary, so we don't yet index that file's MapKey). For now, the
    reference is in the index but might be orphan-flagged: that's
    expected until we wire up the divine.exe path for LSF parsing."""
    rt_uuid = "d21296e6-898c-4072-8c24-4c5a26f249f0"
    assert rt_uuid in shadowdancer_index.referenced_values(IdKind.UUID)


# --- Clash detection across projects ----------------------------------------


def test_no_clashes_between_real_fixtures(shadow_dance_index, shadowdancer_index):
    """The two fixture projects were chosen to be a clean union. We expect
    zero identifier clashes (stat names, UUIDs, loca handles, icons, paths
    all disjoint). The shared GustavX *dependency* is a reference in both,
    NOT a definition in either: so it doesn't count as a clash."""
    clashes = find_clashes(shadow_dance_index, shadowdancer_index)
    assert clashes == [], (
        f"Expected zero clashes, got: "
        f"{[(c.kind.value, c.value) for c in clashes]}"
    )


def test_clash_detection_finds_shared_stat_name():
    """When two indexes both define the same stat name, the clash is reported."""
    a = ReferenceIndex()
    b = ReferenceIndex()
    loc_a = references.Location(file=None, hint="A")  # type: ignore[arg-type]
    loc_b = references.Location(file=None, hint="B")  # type: ignore[arg-type]
    a.add_definition(IdKind.STAT_NAME, "DuplicatedThing", loc_a)
    b.add_definition(IdKind.STAT_NAME, "DuplicatedThing", loc_b)
    a.add_definition(IdKind.STAT_NAME, "OnlyInA", loc_a)
    b.add_definition(IdKind.STAT_NAME, "OnlyInB", loc_b)

    clashes = find_clashes(a, b)
    assert len(clashes) == 1
    assert clashes[0].kind == IdKind.STAT_NAME
    assert clashes[0].value == "DuplicatedThing"


def test_clash_detection_ignores_shared_references():
    """Both projects referencing the same UUID (e.g. GustavX) but neither
    defining it isn't a clash: it's expected behavior for dependencies."""
    a = ReferenceIndex()
    b = ReferenceIndex()
    loc_a = references.Location(file=None, hint="A")  # type: ignore[arg-type]
    loc_b = references.Location(file=None, hint="B")  # type: ignore[arg-type]
    shared_uuid = "cb555efe-2d9e-131f-8195-a89329d218ea"
    a.add_reference(IdKind.UUID, shared_uuid, loc_a)
    b.add_reference(IdKind.UUID, shared_uuid, loc_b)

    clashes = find_clashes(a, b)
    assert clashes == []


# --- Orphan-reference reporting ---------------------------------------------


def test_orphans_are_pure_references_with_no_definitions(shadow_dance_index):
    """Orphan = referenced but never defined in this project. The merger
    treats orphans as warnings (they may be base-game refs or other-mod refs)."""
    for kind in IdKind:
        for entry in shadow_dance_index.orphan_references(kind):
            assert entry.references and not entry.definitions
