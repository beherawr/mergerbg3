"""The ReferenceIndex: who *defines* and who *references* each identifier.

This is the keystone module. Every merge correctness check depends on it:

- "If we change this mod's folder name, what file paths must we rewrite?"
- "Does Mod A define a stat that Mod B silently overrides?"
- "After merge, are there any references with no defining target?"

The index is built by scanning a ``Project`` and walking its content. For
each identifier kind, it records two sets:

- **definitions**: where the identifier is introduced (with the file +
  location producing it). E.g. a stats `.txt` entry's ``new entry "FOO"``
  defines the stat name ``FOO``.
- **references**: where the identifier is *used*. E.g. another stats
  entry's ``using "FOO"`` references it; a loca handle inside an LSX
  attribute references the handle.

Five identifier kinds today (matching the plan's §4):

1. UUIDs — for root templates, banks, resources, dependencies
2. Stat names — the line-based identifiers in `.txt` and Name fields in `.stats`
3. Loca handles — ``h<32 chars>`` referencing translation entries
4. Icon names — referenced from stats' ``Icon`` field and root templates
5. File path strings — usually under SourceFile= attributes inside LSX

Performance: walking the two real fixture projects produces ~hundreds of
identifiers, not millions. We optimize for clarity over speed; if a future
mod is much larger we revisit.

The merger uses this index to:
- Detect conflicts (intersection of A.definitions and B.definitions)
- Build remap tables (every reference that needs a string substitution)
- Validate output (every reference resolves to a definition somewhere)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import lsx, stats_text, stats_xml, localization, meta as _meta
from .project import CatalogedFile, FileCategory, Project


# --- Identifier kinds ----------------------------------------------------


class IdKind(Enum):
    UUID = "uuid"
    STAT_NAME = "stat_name"
    LOCA_HANDLE = "loca_handle"
    ICON_NAME = "icon_name"
    PATH_STRING = "path_string"


@dataclass(frozen=True)
class Location:
    """Where in the project an identifier was found.

    ``file`` is relative to the project root for log-friendliness. ``hint``
    is a short human-readable description like ``"stats Target_BackstabK"``
    or ``"root template MapKey"`` — surfaced in the GUI's conflict view.
    """
    file: Path
    hint: str


@dataclass
class IdentifierEntry:
    """An identifier (e.g. a particular UUID) along with everywhere it
    was defined or referenced."""
    kind: IdKind
    value: str
    definitions: list[Location] = field(default_factory=list)
    references: list[Location] = field(default_factory=list)


# --- Patterns ------------------------------------------------------------


# RFC 4122 UUID with dashes, case-insensitive.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# Stat names (left-hand side of `new entry "..."` and right-hand side of
# `using "..."`) — we parse them via stats_text/stats_xml, not regex.


# Bareword identifier inside a stats data value (e.g. the "INVISIBLEKira"
# in ``ApplyStatus(INVISIBLEKira, 100, 1)``). Permissive: catches both
# real stat references and base-game tokens. Real references will resolve
# in the index; base-game tokens become benign orphan references.
_STAT_TOKEN_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{1,}\b")

# Subset of identifier-shaped words that are Larian DSL keywords / base
# game enums and should NOT be indexed as stat name references. This
# keeps the orphan-references list short and useful. Not exhaustive — we
# can grow it as patterns emerge; missing one is harmless.
_STAT_TOKEN_DENYLIST: set[str] = {
    # Damage types
    "Bludgeoning", "Piercing", "Slashing", "Acid", "Cold", "Fire", "Force",
    "Lightning", "Necrotic", "Poison", "Psychic", "Radiant", "Thunder",
    # Ability scores
    "Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma",
    # Common keywords / target qualifiers
    "Self", "Character", "Item", "MainHand", "OffHand", "MeleeWeaponAttack",
    "RangedWeaponAttack", "AttackType", "Attack", "true", "false", "None",
    # Weapon properties / boost names
    "Magical", "Finesse", "Light", "Thrown", "Melee", "Dippable", "Heavy",
    "Reach", "Versatile", "Loading", "Ammunition", "TwoHanded",
    "MainMeleeWeapon", "MainMeleeWeaponDamageType", "WeaponEnchantment",
    "WeaponProperty", "ActionResourceBlock", "ReactionActionPoint",
    "Advantage", "AttackRoll", "Invisibility", "IgnoreLeaveAttackRange",
    "DetectDisturbancesBlock", "DarkvisionRangeMin", "ActiveCharacterLight",
    "StatusImmunity", "IgnoreSurfaceCover", "BLINDED_DARKNESS",
    "SurfaceDarknessCloud", "JumpMaxDistanceMultiplier",
    # Sheathing / animation
    "PhysicalDamage", "Aggressive", "Defensive",
    # SpellFlags / StatusPropertyFlags
    "IsHarmful", "IgnoreSilence", "ImmediateCast", "IsSpell", "Stealth",
    "Invisible", "RangeIgnoreSourceBounds", "RangeIgnoreTargetBounds",
    "RangeIgnoreVerticalThreshold", "UnavailableInActiveRoll",
    "ExcludeFromPortraitRendering", "DisableOverhead", "DisableCombatlog",
    "DisablePortraitIndicator",
    # Status groups
    "SG_Invisible", "SG_RemoveOnRespec",
    # Events
    "OnSpellCast", "OnDamage", "OnEntityPickUp", "OnEntityDrop",
    "OnEntityDrag", "OnLockpickingFinished", "OnDisarmingFinished",
    # Conditions/funcs
    "IsStatusEvent", "StatusEvent", "HasSpellFlag", "SpellFlags",
    "TotalDamageDoneGreaterThan", "Dead", "Enemy", "GROUND",
    "TeleportSource", "ApplyStatus", "DealDamage", "ExecuteWeaponFunctors",
    "max", "Yes", "No", "Replacement", "INVISIBILITY", "White",
    "StartTurn", "OnTick", "Vocal_Component_Shadow_Monk",
    "Vocal_Component_Stop", "Cast", "ActionPoint", "Boosts", "Properties",
}


def _extract_stat_tokens(text: str) -> set[str]:
    """Permissive: identifier-shaped tokens, minus known non-stat keywords."""
    return {
        m for m in _STAT_TOKEN_RE.findall(text)
        if m not in _STAT_TOKEN_DENYLIST and not m.startswith("_")
    }


def _extract_uuids(text: str) -> set[str]:
    """All UUIDs in some chunk of text. Returns lowercased values for stable
    set membership (Larian always uses lowercase but we don't want to break
    if some upstream tool emits uppercase)."""
    return {m.lower() for m in _UUID_RE.findall(text)}


# --- The index -----------------------------------------------------------


class ReferenceIndex:
    """Builds and queries the cross-reference graph for one project.

    Construct via ``ReferenceIndex.build(project)``. Then ask questions:
    ``index.entries_by_kind(IdKind.UUID)``, ``index.definitions_of("foo")``,
    ``index.orphan_references()``, etc.

    The index is *project-scoped*. The merger creates one per input project,
    then compares them to find conflicts. After merge it builds an index for
    the output project and runs orphan detection.
    """

    def __init__(self) -> None:
        # Two-level map: kind -> value -> IdentifierEntry
        self._entries: dict[IdKind, dict[str, IdentifierEntry]] = {
            kind: {} for kind in IdKind
        }

    # --- mutation API used during build --------------------------------

    def _get_or_create(self, kind: IdKind, value: str) -> IdentifierEntry:
        bucket = self._entries[kind]
        entry = bucket.get(value)
        if entry is None:
            entry = IdentifierEntry(kind=kind, value=value)
            bucket[value] = entry
        return entry

    def add_definition(self, kind: IdKind, value: str, location: Location) -> None:
        self._get_or_create(kind, value).definitions.append(location)

    def add_reference(self, kind: IdKind, value: str, location: Location) -> None:
        self._get_or_create(kind, value).references.append(location)

    # --- query API ------------------------------------------------------

    def entries_by_kind(self, kind: IdKind) -> list[IdentifierEntry]:
        """All identifiers of one kind, in alphabetical order."""
        return sorted(self._entries[kind].values(), key=lambda e: e.value)

    def all_entries(self) -> list[IdentifierEntry]:
        """Every identifier across all kinds."""
        out: list[IdentifierEntry] = []
        for kind in IdKind:
            out.extend(self.entries_by_kind(kind))
        return out

    def get(self, kind: IdKind, value: str) -> IdentifierEntry | None:
        return self._entries[kind].get(value)

    def defined_values(self, kind: IdKind) -> set[str]:
        """Set of values of this kind that have at least one definition."""
        return {v for v, e in self._entries[kind].items() if e.definitions}

    def referenced_values(self, kind: IdKind) -> set[str]:
        """Set of values of this kind that are referenced anywhere."""
        return {v for v, e in self._entries[kind].items() if e.references}

    def orphan_references(self, kind: IdKind) -> list[IdentifierEntry]:
        """Identifiers that are referenced but never defined in this project.

        These are not necessarily errors — many will resolve against base
        game data (``WPN_Dagger``, the GustavX UUID, etc.) or against
        another mod loaded at runtime. The merger's validation pass
        surfaces them for the user to inspect, not to block.
        """
        return [
            e for e in self.entries_by_kind(kind)
            if e.references and not e.definitions
        ]

    def summary(self) -> str:
        """Human-readable digest. Used by the GUI's project-preview screen."""
        lines = []
        for kind in IdKind:
            entries = self.entries_by_kind(kind)
            defs = sum(1 for e in entries if e.definitions)
            refs = sum(1 for e in entries if e.references)
            orph = len(self.orphan_references(kind))
            lines.append(
                f"  {kind.value:<14} {len(entries):>4} identifiers  "
                f"({defs} defined, {refs} referenced, {orph} orphan refs)"
            )
        return "\n".join(lines)

    # --- factory --------------------------------------------------------

    @classmethod
    def build(cls, project: Project) -> "ReferenceIndex":
        """Walk a project and produce its ReferenceIndex.

        Each file category contributes definitions/references via a
        dedicated scanner below. Order doesn't matter — the index is a
        bag of identifier locations.
        """
        index = cls()

        # Top-level mod identity from meta.lsx.
        _scan_mod_meta(index, project)

        # Stats — both forms.
        for cf in project.files_by_category(FileCategory.STATS_TXT):
            _scan_stats_txt(index, cf)
        for cf in project.files_by_category(FileCategory.STATS_XML):
            _scan_stats_xml(index, cf)

        # Localization.
        for cf in project.files_by_category(FileCategory.LOCALIZATION):
            _scan_localization(index, cf)

        # LSX-format files that may hold UUID and path references.
        for cat in (
            FileCategory.ROOT_TEMPLATE_LSX,
            FileCategory.BANK_LSX,
            FileCategory.UI_MERGED,
            FileCategory.ICON_UV_LSX,
        ):
            for cf in project.files_by_category(cat):
                _scan_lsx(index, cf)

        # Story goal source files (Osiris) — references spells by name and
        # game UUIDs by string substitution. We do a regex scan rather than
        # a proper Osiris parse.
        for cf in project.files_by_category(FileCategory.STORY_GOAL):
            _scan_story_goal(index, cf)

        return index


# --- Scanners ------------------------------------------------------------


def _loc(cf: CatalogedFile, hint: str) -> Location:
    """Shorthand for building a Location from a cataloged file."""
    return Location(file=cf.rel_to_project_root, hint=hint)


def _meta_loc(project: Project, hint: str) -> Location:
    """Location helper for meta.lsx-derived definitions/references."""
    return Location(
        file=Path("Mods") / project.mod_folder_name / "meta.lsx",
        hint=hint,
    )


def _scan_mod_meta(index: ReferenceIndex, project: Project) -> None:
    """The mod's own UUID is a definition; dependency UUIDs are references."""
    index.add_definition(
        IdKind.UUID,
        project.mod_meta.uuid.lower(),
        _meta_loc(project, "this mod's identity"),
    )
    for dep in project.mod_meta.dependencies:
        if dep.uuid:
            index.add_reference(
                IdKind.UUID,
                dep.uuid.lower(),
                _meta_loc(project, f"dependency on {dep.name!r}"),
            )


def _scan_stats_txt(index: ReferenceIndex, cf: CatalogedFile) -> None:
    """For each stats .txt entry:

    - The entry's name is a STAT_NAME definition.
    - The ``using`` parent is a STAT_NAME reference.
    - Loca handles in data values are LOCA_HANDLE references.
    - UUIDs in data values are UUID references (RootTemplate, effect refs, etc.).
    - ``data "Icon"`` value is an ICON_NAME reference.
    """
    try:
        parsed = stats_text.parse_file(cf.path)
    except stats_text.StatsParseError:
        # Don't crash the whole index build on one bad file; the merger
        # will refuse to merge a project that can't be parsed.
        return

    for entry in parsed.entries:
        index.add_definition(
            IdKind.STAT_NAME, entry.name,
            _loc(cf, f"stats entry {entry.name!r} (type {entry.type})"),
        )
        if entry.using:
            index.add_reference(
                IdKind.STAT_NAME, entry.using,
                _loc(cf, f"using by {entry.name!r}"),
            )
        for key, value in entry.data:
            # Loca handles.
            for handle in localization.extract_handles(value):
                index.add_reference(
                    IdKind.LOCA_HANDLE, handle,
                    _loc(cf, f"loca handle in {entry.name!r}.{key}"),
                )
            # UUIDs.
            for u in _extract_uuids(value):
                index.add_reference(
                    IdKind.UUID, u,
                    _loc(cf, f"UUID in {entry.name!r}.{key}"),
                )
            # Stat name refs embedded in functor-list syntax like
            # ``ApplyStatus(INVISIBLEKira, 100, 1)``. Permissive — may
            # over-include but the denylist trims the obvious noise and
            # leftover orphans are harmless warnings.
            for token in _extract_stat_tokens(value):
                # Skip the entry's own name to avoid trivial self-refs.
                if token == entry.name:
                    continue
                index.add_reference(
                    IdKind.STAT_NAME, token,
                    _loc(cf, f"token in {entry.name!r}.{key}"),
                )
            # Icons.
            if key == "Icon" and value:
                index.add_reference(
                    IdKind.ICON_NAME, value,
                    _loc(cf, f"icon ref by {entry.name!r}"),
                )


def _scan_stats_xml(index: ReferenceIndex, cf: CatalogedFile) -> None:
    """The .stats XML format. The Name field is the STAT_NAME; the UUID field
    is a UUID definition; handle/version fields are loca references.

    NOTE: The .stats Name is the *unprefixed* form (e.g. ``BackstabK``),
    while the generated .txt has the *prefixed* form (e.g.
    ``Target_BackstabK``). Both forms are indexed as STAT_NAME so that
    references against either form resolve.
    """
    try:
        parsed = stats_xml.parse_file(cf.path)
    except stats_xml.StatsXmlParseError:
        return

    for obj in parsed.objects:
        if obj.uuid:
            index.add_definition(
                IdKind.UUID, obj.uuid.lower(),
                _loc(cf, f"stats UUID for {obj.name or '?'}"),
            )
        # Substats (is_substat=true) have a Name field, but that Name is a
        # *role/label* shared across multiple sibling substats with distinct
        # UUIDs. The classic case is treasure-table substats where four
        # entries named "X_substat" represent four drop-roll variants. Only
        # index the Name as a STAT_NAME definition for primary stats; for
        # substats, the UUID above is the identity.
        #
        # Also skip CanMerge=Yes primaries: their Name is a grouping label
        # meant to be shared with other mods that opt into runtime merging.
        # Indexing them as STAT_NAME would surface a false-positive
        # collision whenever two mods both contribute to a shared table.
        is_canmerge = False
        cm_field = obj.field_by_name("CanMerge")
        if cm_field and (cm_field.value or "").strip().lower() == "yes":
            is_canmerge = True
        if obj.name and not obj.is_substat and not is_canmerge:
            index.add_definition(
                IdKind.STAT_NAME, obj.name,
                _loc(cf, f".stats entry {obj.name!r}"),
            )
        for f in obj.fields:
            # TranslatedString fields carry loca handles in their handle attr.
            if f.handle:
                index.add_reference(
                    IdKind.LOCA_HANDLE, f.handle,
                    _loc(cf, f"loca handle in {obj.name or '?'}.{f.name}"),
                )
            # Value strings may contain UUIDs (e.g. RootTemplate field).
            if f.value:
                for u in _extract_uuids(f.value):
                    index.add_reference(
                        IdKind.UUID, u,
                        _loc(cf, f"UUID in {obj.name or '?'}.{f.name}"),
                    )
                # Icon field references an icon name.
                if f.name == "Icon" and f.value:
                    index.add_reference(
                        IdKind.ICON_NAME, f.value,
                        _loc(cf, f"icon ref by {obj.name or '?'}"),
                    )


def _scan_localization(index: ReferenceIndex, cf: CatalogedFile) -> None:
    """Every <content contentuid="h..."> is a LOCA_HANDLE definition.

    The text body may contain LSTag references to statuses/passives by
    name — those are STAT_NAME references. We don't currently parse the
    inline tags' attributes (rarely useful for merging), but the regex
    scanner picks up any embedded handles and UUIDs.
    """
    try:
        parsed = localization.parse_file(cf.path)
    except localization.LocaParseError:
        return

    for entry in parsed.entries:
        index.add_definition(
            IdKind.LOCA_HANDLE, entry.contentuid,
            _loc(cf, f"loca entry version={entry.version}"),
        )
        # Loca bodies sometimes reference statuses inside <LSTag>; harmless to scan.
        for handle in localization.extract_handles(entry.text):
            if handle != entry.contentuid:
                index.add_reference(
                    IdKind.LOCA_HANDLE, handle,
                    _loc(cf, f"loca cross-ref from {entry.contentuid}"),
                )


def _scan_lsx(index: ReferenceIndex, cf: CatalogedFile) -> None:
    """Walk an LSX file's tree and harvest references.

    - Every ``value=`` on a ``guid``/``FixedString`` attribute that looks like
      a UUID is a UUID reference.
    - Special-case: ``MapKey``, ``UUID``, ``Module`` (in some contexts) are
      *definitions* of their UUID, not references.
    - ``handle`` on a TranslatedString attribute is a loca reference.
    - ``SourceFile``, ``Path``, ``ResourcePath`` values are PATH_STRING
      references (so we can rewrite them on folder rename).
    """
    try:
        doc = lsx.parse_file(cf.path)
    except lsx.LsxParseError:
        return

    # Attribute ids that introduce a UUID rather than reference one.
    DEF_ATTRS = {"MapKey", "UUID"}

    # Attribute ids that hold file-path strings we may need to remap.
    PATH_ATTRS = {"SourceFile", "Path", "ResourcePath"}

    for region in doc.regions:
        for node in region.root_node.walk():
            for attr in node.attributes:
                # Loca handle on TranslatedString.
                if attr.type == "TranslatedString" and attr.handle:
                    index.add_reference(
                        IdKind.LOCA_HANDLE, attr.handle,
                        _loc(cf, f"LSX {node.id}.{attr.id}"),
                    )
                    continue

                val = attr.value
                if not val:
                    continue

                # File path strings.
                if attr.id in PATH_ATTRS and "/" in val:
                    index.add_reference(
                        IdKind.PATH_STRING, val,
                        _loc(cf, f"LSX {node.id}.{attr.id}"),
                    )

                # UUIDs in the value — either a definition or a reference,
                # depending on the attribute id.
                for u in _extract_uuids(val):
                    if attr.id in DEF_ATTRS:
                        index.add_definition(
                            IdKind.UUID, u,
                            _loc(cf, f"LSX {node.id}.{attr.id}"),
                        )
                    else:
                        index.add_reference(
                            IdKind.UUID, u,
                            _loc(cf, f"LSX {node.id}.{attr.id}"),
                        )


def _scan_story_goal(index: ReferenceIndex, cf: CatalogedFile) -> None:
    """Osiris goal files reference spells by string name and game entities
    by UUID. We can't fully parse Osiris syntax, but a regex/string scan
    over the file content catches the references that matter for merging.

    Examples of patterns we want to catch::

        AddSpell(_Player, "Shout_ShadowDanceK", 1, 0)
        ApplyStatus(BlockReactK, 100, 1)
        PlayEffect(_Caster, (EFFECTRESOURCE)ArmsOfHadar_PrepareEffect_80a4c9a2-...)
    """
    try:
        text = cf.path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    # Quoted strings → potential stat names.
    for match in re.finditer(r'"([A-Za-z_][A-Za-z0-9_]*)"', text):
        index.add_reference(
            IdKind.STAT_NAME, match.group(1),
            _loc(cf, "Osiris goal string ref"),
        )

    # UUIDs.
    for u in _extract_uuids(text):
        index.add_reference(
            IdKind.UUID, u,
            _loc(cf, "Osiris goal UUID ref"),
        )


# --- Cross-project comparison (used by the merger) -----------------------


@dataclass
class IdentifierClash:
    """Same identifier value is *defined* in both projects.

    Whether the clash is a real conflict (different content) or a benign
    duplicate (identical content) requires content-level diffs, which
    happen in the per-format merge functions. The reference index only
    detects that the same identifier name was claimed twice.
    """
    kind: IdKind
    value: str
    a_locations: list[Location]
    b_locations: list[Location]


def find_clashes(
    a: ReferenceIndex,
    b: ReferenceIndex,
) -> list[IdentifierClash]:
    """All definitions shared between two projects.

    The merger feeds this into the per-format diff to decide whether each
    clash needs user resolution. We exclude UUIDs that are dependency
    references (e.g. both projects depending on GustavX) — those aren't
    clashes, just shared deps.
    """
    clashes: list[IdentifierClash] = []
    for kind in IdKind:
        a_defs = a.defined_values(kind)
        b_defs = b.defined_values(kind)
        for value in sorted(a_defs & b_defs):
            a_entry = a.get(kind, value)
            b_entry = b.get(kind, value)
            assert a_entry is not None and b_entry is not None
            clashes.append(IdentifierClash(
                kind=kind,
                value=value,
                a_locations=list(a_entry.definitions),
                b_locations=list(b_entry.definitions),
            ))
    return clashes
