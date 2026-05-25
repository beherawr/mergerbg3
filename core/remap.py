"""Remap tables: identifier rewrites that get applied to one input's content
when it goes into the merged output.

When the merger combines two projects, it sometimes has to rewrite
identifiers in one project's content so the output is consistent:

- **Folder rename**: every input's mod folder gets replaced by the new
  merged folder name. Any file path string mentioning the old folder
  (e.g. ``SourceFile="Public/Shadowdancer_f4ef.../Banks/foo.lsf"``) must
  be rewritten to use the new folder. → PathRemap.
- **Stat-name conflicts**: when the user chooses the "prefix" policy and
  both inputs define ``"Backstab"``, mod B's becomes ``"ModB_Backstab"``.
  Every reference to ``"Backstab"`` inside mod B's other files
  (stats data values, Osiris goals, etc.) must follow. → StatRemap.
- **Loca handle conflicts**: same idea, much rarer in practice because
  handles are random hex. → HandleRemap.
- **Icon conflicts** when two atlases reuse the same icon name. → IconRemap.
- **UUID rewrites**: the mod's own UUID changes to the merged UUID,
  but root-template UUIDs and other resource UUIDs stay the same
  (Larian's UUIDs are content-addressable; reusing them across mods
  isn't a real-world concern). → UuidRemap.

Each remap kind is a thin dataclass wrapping a ``dict[str, str]``. The
bulk rewriters at the bottom of this module take a parsed file (Stats,
StatsXml, Loca, Lsx) and apply the remaps in place. The merger calls
these rewriters on mod B's content before appending to mod A's.

Design choices worth noting:

- Remaps are *one-way* (old -> new). We never need the inverse.
- Remaps don't conflict-detect themselves — the planning phase ensures
  each before-value has only one after-value. If we ever see "x -> y"
  and "x -> z" we raise immediately rather than silently dropping one.
- Empty remap tables are common and cheap (no-op). The merger always
  constructs one PathRemap/UuidRemap/etc. even when no entries exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from . import stats_text, stats_xml, localization, lsx


# --- Per-kind remap tables ------------------------------------------------


@dataclass
class _BaseRemap:
    """Common dict-backed mapping with safety checks. Not used directly —
    the per-kind subclasses give us type safety at call sites."""
    mapping: dict[str, str] = field(default_factory=dict)

    def add(self, before: str, after: str) -> None:
        """Add a mapping. Raises if ``before`` is already mapped to a
        different ``after`` (caller bug: ambiguous remap)."""
        existing = self.mapping.get(before)
        if existing is not None and existing != after:
            raise ValueError(
                f"conflicting remap: {before!r} already maps to {existing!r}, "
                f"can't also map to {after!r}"
            )
        self.mapping[before] = after

    def get(self, before: str) -> str | None:
        """Return the mapped value, or None if not remapped."""
        return self.mapping.get(before)

    def apply(self, value: str) -> str:
        """Return the remapped value, or the original if not in the table."""
        return self.mapping.get(value, value)

    def is_empty(self) -> bool:
        return not self.mapping

    def __bool__(self) -> bool:
        return bool(self.mapping)


@dataclass
class PathRemap(_BaseRemap):
    """Remaps file path strings used inside LSX attribute values.

    Folder-rename: ``"Public/<OldFolder>/..."`` -> ``"Public/<NewFolder>/..."``.
    These are string substitutions, not path normalizations — we match
    forward-slash POSIX form because that's what Larian uses on disk
    inside string values, regardless of OS.
    """
    # Substring substitutions, applied longest-match-first in apply_to_text().
    substring_substitutions: dict[str, str] = field(default_factory=dict)

    def add_substring(self, before: str, after: str) -> None:
        existing = self.substring_substitutions.get(before)
        if existing is not None and existing != after:
            raise ValueError(
                f"conflicting path substring remap: {before!r} already maps to "
                f"{existing!r}, can't also map to {after!r}"
            )
        self.substring_substitutions[before] = after

    def apply_to_text(self, text: str) -> str:
        """Substring-substitute every entry, longest-key-first to avoid
        partial-prefix conflicts."""
        if not self.substring_substitutions:
            return text
        for before in sorted(self.substring_substitutions, key=len, reverse=True):
            after = self.substring_substitutions[before]
            text = text.replace(before, after)
        return text


@dataclass
class UuidRemap(_BaseRemap):
    """Remaps UUIDs (e.g. the mod's own UUID -> the merged UUID)."""
    pass


@dataclass
class StatRemap(_BaseRemap):
    """Remaps stat names. Applied to:
    - ``new entry`` and ``using`` keywords in stats .txt
    - ``<field name="Name">`` value and unprefixed lookups in .stats XML
    - Stat name tokens inside data values (e.g. ``ApplyStatus(FOO,...)``)
    - Osiris goal source files (string literal references)
    """
    pass


@dataclass
class HandleRemap(_BaseRemap):
    """Remaps loca handles. Applied to:
    - ``contentuid`` attributes in .loca.xml
    - ``handle`` attributes on TranslatedString LSX elements
    - The joined ``<handle>;<version>`` form used inside stats data values
    """
    pass


@dataclass
class IconRemap(_BaseRemap):
    """Remaps icon names. Applied to:
    - ``data "Icon" "<name>"`` rows in stats .txt
    - ``<field name="Icon" value="<name>"/>`` in .stats XML
    - Icon MapKey attributes inside Icons_*.lsx UV-coord files
    """
    pass


# --- A collected set the merger threads through everywhere ---------------


@dataclass
class RemapSet:
    """All five remaps the merger needs in one place. Passed to every
    bulk-rewriter below."""
    paths: PathRemap = field(default_factory=PathRemap)
    uuids: UuidRemap = field(default_factory=UuidRemap)
    stats: StatRemap = field(default_factory=StatRemap)
    handles: HandleRemap = field(default_factory=HandleRemap)
    icons: IconRemap = field(default_factory=IconRemap)

    def is_empty(self) -> bool:
        return (
            self.paths.is_empty() and self.uuids.is_empty()
            and self.stats.is_empty() and self.handles.is_empty()
            and self.icons.is_empty()
        )


# --- Helpers for value-level rewriting ------------------------------------


# A stats data value can contain stat-name-shaped tokens that need
# remapping. Pattern matches bareword identifiers; we apply the StatRemap
# to each whole-word match individually so we don't accidentally rewrite
# substrings inside larger words.
_BAREWORD_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]+\b")


def _rewrite_stat_tokens(text: str, stats: StatRemap) -> str:
    """Replace every standalone identifier in ``text`` that appears in
    the StatRemap. Leaves everything else (operators, punctuation,
    numbers, quoted strings, function names) untouched."""
    if stats.is_empty():
        return text
    def sub(match: re.Match[str]) -> str:
        token = match.group(0)
        return stats.mapping.get(token, token)
    return _BAREWORD_RE.sub(sub, text)


# The joined "handle;version" form (used by stats data values for
# TranslatedString refs) splits as h<...>;<version>. We rewrite the
# handle portion only.
_HANDLE_WITH_VERSION_RE = re.compile(
    r"\b(h[0-9a-f]{8}g[0-9a-f]{4}g[0-9a-f]{4}g[0-9a-f]{4}g[0-9a-f]{12})(;\d+)\b"
)


def _rewrite_joined_handles(text: str, handles: HandleRemap) -> str:
    """Rewrite the handle portion of ``h...;<ver>`` joined forms."""
    if handles.is_empty():
        return text
    def sub(match: re.Match[str]) -> str:
        handle = match.group(1)
        version = match.group(2)
        return handles.apply(handle) + version
    return _HANDLE_WITH_VERSION_RE.sub(sub, text)


# Plain handle (without ;version) — appears inside LSX TranslatedString
# attributes, loca contentuid, occasionally raw in Osiris goals.
_HANDLE_RE = re.compile(
    r"\bh[0-9a-f]{8}g[0-9a-f]{4}g[0-9a-f]{4}g[0-9a-f]{4}g[0-9a-f]{12}\b"
)


def _rewrite_bare_handles(text: str, handles: HandleRemap) -> str:
    if handles.is_empty():
        return text
    def sub(match: re.Match[str]) -> str:
        return handles.apply(match.group(0))
    return _HANDLE_RE.sub(sub, text)


# UUIDs in text (case-insensitive for the input, lowercase for output).
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _rewrite_uuids(text: str, uuids: UuidRemap) -> str:
    if uuids.is_empty():
        return text
    def sub(match: re.Match[str]) -> str:
        return uuids.apply(match.group(0).lower())
    return _UUID_RE.sub(sub, text)


def rewrite_value(value: str, remaps: RemapSet) -> str:
    """Apply every remap kind in sequence to a single value string.

    The order matters: paths first (longest-substring), then joined
    handles (which may contain UUIDs), then bare handles, then UUIDs,
    then stat tokens. Each pass is a no-op when its remap table is empty,
    so on the common no-conflict case this is essentially a string copy.
    """
    out = value
    out = remaps.paths.apply_to_text(out)
    out = _rewrite_joined_handles(out, remaps.handles)
    out = _rewrite_bare_handles(out, remaps.handles)
    out = _rewrite_uuids(out, remaps.uuids)
    out = _rewrite_stat_tokens(out, remaps.stats)
    return out


# --- Bulk rewriters for parsed file types --------------------------------


def rewrite_stats_text(stats: stats_text.StatsFile, remaps: RemapSet) -> None:
    """In-place rewrite of a parsed stats .txt file.

    For each entry:
    - entry.name -> StatRemap
    - entry.using -> StatRemap (parent stat refs follow)
    - each data value -> rewrite_value() for all the embedded refs
    """
    for entry in stats.entries:
        entry.name = remaps.stats.apply(entry.name)
        if entry.using is not None:
            entry.using = remaps.stats.apply(entry.using)
        # Special-case Icon — single-token icon names get the icon remap.
        new_data: list[tuple[str, str]] = []
        for key, value in entry.data:
            if key == "Icon":
                new_value = remaps.icons.apply(value)
            else:
                new_value = rewrite_value(value, remaps)
            new_data.append((key, new_value))
        entry.data = new_data


def rewrite_stats_xml(stats: stats_xml.StatsXmlFile, remaps: RemapSet) -> None:
    """In-place rewrite of a parsed .stats XML file."""
    for obj in stats.objects:
        for fld in obj.fields:
            # The Name field is the stat identifier.
            if fld.name == "Name":
                if v := fld.extra.get("value"):
                    fld.extra["value"] = remaps.stats.apply(v)
                continue
            # The UUID field is the toolkit identity.
            if fld.name == "UUID":
                if v := fld.extra.get("value"):
                    fld.extra["value"] = remaps.uuids.apply(v.lower())
                continue
            # The Icon field — icon name (single token).
            if fld.name == "Icon":
                if v := fld.extra.get("value"):
                    fld.extra["value"] = remaps.icons.apply(v)
                continue
            # TranslatedString fields carry handles separately.
            if fld.handle is not None:
                fld.extra["handle"] = remaps.handles.apply(fld.handle)
            # General-purpose value rewrite for everything else.
            if v := fld.extra.get("value"):
                fld.extra["value"] = rewrite_value(v, remaps)


def rewrite_localization(loca: localization.LocaFile, remaps: RemapSet) -> None:
    """In-place rewrite of a parsed .loca.xml file.

    Loca handles are the contentuid; the body text may reference other
    handles via inline tags or stat names (for status tooltips), so we
    rewrite it through the value-level pipeline too."""
    for entry in loca.entries:
        entry.contentuid = remaps.handles.apply(entry.contentuid)
        entry.text = rewrite_value(entry.text, remaps)


def rewrite_lsx(doc: lsx.LsxDocument, remaps: RemapSet) -> None:
    """In-place rewrite of a parsed LsxDocument.

    Walks every node in every region and rewrites attribute values:
    - TranslatedString handle attr -> HandleRemap
    - guid/FixedString value attr -> UuidRemap (if value looks like a UUID)
    - SourceFile/Path/ResourcePath value -> PathRemap
    - Everything else -> rewrite_value() for embedded refs
    """
    PATH_ATTRS = {"SourceFile", "Path", "ResourcePath"}
    for region in doc.regions:
        for node in region.root_node.walk():
            for attr in node.attributes:
                if attr.type == "TranslatedString":
                    if attr.handle is not None:
                        attr.handle = remaps.handles.apply(attr.handle)
                    continue
                if attr.value is None:
                    continue
                if attr.id in PATH_ATTRS:
                    attr.value = remaps.paths.apply_to_text(attr.value)
                    continue
                # Best-effort full pipeline for anything else.
                attr.value = rewrite_value(attr.value, remaps)


def rewrite_text_file(text: str, remaps: RemapSet) -> str:
    """Generic text-file rewriter for files we don't parse structurally
    (Osiris goals, SE Lua, asset-import settings XMLs).

    Applies the full value-level pipeline plus the substring path remap.
    """
    return rewrite_value(text, remaps)
