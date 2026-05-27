"""Parser/writer for the Toolkit's `.stats` XML format.

These files live under ``Editor/Mods/<ModFolder>/Stats/<Category>/`` (where
``<Category>`` is ``SpellData``, ``StatusData``, ``Stats``, etc.) and are
the Toolkit's *canonical editing format* for stats. The Toolkit generates
the line-based ``.txt`` files under ``Public/<ModFolder>/Stats/Generated/Data/``
from these on save.

Example::

    <?xml version="1.0" encoding="utf-8"?>
    <stats stat_object_definition_id="e988a674-28fe-49d2-a6ce-c5c1e0141f4c">
      <stat_objects>
        <stat_object is_substat="false">
          <fields>
            <field name="UUID" type="IdTableFieldDefinition" value="fe46907b-..." />
            <field name="Name" type="NameTableFieldDefinition" value="BackstabK" />
            <field name="DisplayName" type="TranslatedStringTableFieldDefinition"
                   handle="h2db009be..." version="2" />
            <field name="VerbalIntent" type="EnumerationTableFieldDefinition"
                   value="Damage" enumeration_type_name="VerbalIntent" version="1" />
          </fields>
        </stat_object>
      </stat_objects>
    </stats>

Key facts that diverge from the line-based ``.txt`` format:

- Each ``<stat_object>`` carries its own UUID (the ``UUID`` field): this
  is the toolkit's identity for the entry, separate from the human-readable
  ``Name``. The ``.txt`` form drops the UUID entirely; only the Name survives.
- The ``Name`` field in ``.stats`` is the *unprefixed* form. The Toolkit
  prefixes the entry name with a subtype when generating the ``.txt``:
  e.g. a SpellData entry with SpellType=Target gets ``new entry "Target_<Name>"``.
  This means parallel-merging .stats and .txt requires us to know about both
  forms; the reference index (later module) reconciles them.
- Field types carry richer info: ``EnumerationTableFieldDefinition`` knows the
  ``enumeration_type_name``, ``IntegerTableFieldDefinition`` is typed int, etc.
  We preserve all of this on round-trip.
- The ``stat_object_definition_id`` attribute on ``<stats>`` is a UUID
  identifying the stat type itself (SpellData has its own UUID, StatusData
  has another, etc.). All entries in a single ``.stats`` file share this.
- Files begin with a UTF-8 BOM. We preserve that.

Notes on parsing strategy: we use a richer dict-of-attributes model for
fields (rather than a fixed-schema dataclass) because the Toolkit may add
new field types at any patch and we don't want to crash. Unknown fields
round-trip verbatim.
"""

from __future__ import annotations

from . import io_util

from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree


# --- Model ----------------------------------------------------------------

@dataclass
class StatsXmlField:
    """One ``<field name="..." type="..." .../>`` element.

    All attributes other than ``name`` and ``type`` are kept in ``extra``
    verbatim. This includes ``value``, ``handle``, ``version``,
    ``enumeration_type_name``, and any future additions.
    """
    name: str
    type: str
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def value(self) -> str | None:
        return self.extra.get("value")

    @property
    def handle(self) -> str | None:
        return self.extra.get("handle")

    @property
    def version(self) -> str | None:
        return self.extra.get("version")

    def to_xml(self) -> etree._Element:
        elem = etree.Element("field", name=self.name, type=self.type)
        for k, v in self.extra.items():
            elem.set(k, v)
        return elem


@dataclass
class StatsXmlObject:
    """One ``<stat_object>`` block: one stats entry.

    Field lookup is provided by ``field_by_name`` because the format is
    name-keyed within an object.
    """
    is_substat: bool = False
    fields: list[StatsXmlField] = field(default_factory=list)

    def field_by_name(self, name: str) -> StatsXmlField | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    @property
    def name(self) -> str | None:
        """The entry's Name field value (the human-readable identifier)."""
        f = self.field_by_name("Name")
        return f.value if f else None

    @property
    def uuid(self) -> str | None:
        """The entry's UUID field value (toolkit identity)."""
        f = self.field_by_name("UUID")
        return f.value if f else None

    def to_xml(self) -> etree._Element:
        elem = etree.Element(
            "stat_object",
            is_substat=("true" if self.is_substat else "false"),
        )
        fields_elem = etree.SubElement(elem, "fields")
        for f in self.fields:
            fields_elem.append(f.to_xml())
        return elem


@dataclass
class StatsXmlFile:
    """A parsed .stats file.

    ``stat_object_definition_id`` is the UUID identifying the stat type
    (SpellData, StatusData, Weapon, etc.). All objects in this file share
    this type. When merging two files, this must match.
    """
    stat_object_definition_id: str
    objects: list[StatsXmlObject] = field(default_factory=list)
    had_bom: bool = True  # observed files always have one; default to keeping it

    def object_by_name(self, name: str) -> StatsXmlObject | None:
        for o in self.objects:
            if o.name == name:
                return o
        return None

    def names(self) -> list[str]:
        return [o.name for o in self.objects if o.name is not None]

    def to_xml_bytes(self) -> bytes:
        root = etree.Element(
            "stats",
            stat_object_definition_id=self.stat_object_definition_id,
        )
        objects_elem = etree.SubElement(root, "stat_objects")
        for o in self.objects:
            objects_elem.append(o.to_xml())

        etree.indent(root, space="  ")  # toolkit uses 2-space indent for .stats
        body = etree.tostring(
            root,
            xml_declaration=True,
            encoding="utf-8",
            pretty_print=True,
        )
        if self.had_bom:
            body = b"\xef\xbb\xbf" + body
        return body


# --- Parsing --------------------------------------------------------------

class StatsXmlParseError(ValueError):
    """Raised when a .stats XML file is structurally invalid."""


def _strip_bom(data: bytes) -> tuple[bytes, bool]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:], True
    return data, False


def _parse_field(elem: etree._Element) -> StatsXmlField:
    name = elem.get("name")
    field_type = elem.get("type")
    if name is None or field_type is None:
        raise StatsXmlParseError(
            f"<field> missing name or type: {etree.tostring(elem)!r}"
        )
    # Preserve all other attributes (value/handle/version/enumeration_type_name/etc.)
    extra = {k: v for k, v in elem.attrib.items() if k not in ("name", "type")}
    return StatsXmlField(name=name, type=field_type, extra=extra)


def _parse_object(elem: etree._Element) -> StatsXmlObject:
    is_substat = elem.get("is_substat", "false").lower() == "true"
    fields: list[StatsXmlField] = []

    for child in elem:
        if isinstance(child, etree._Comment):
            continue
        if child.tag != "fields":
            # Forward-compat: unknown children skipped.
            continue
        for grandchild in child:
            if isinstance(grandchild, etree._Comment):
                continue
            if grandchild.tag == "field":
                fields.append(_parse_field(grandchild))
            # else: forward-compat ignore

    return StatsXmlObject(is_substat=is_substat, fields=fields)


def parse_bytes(data: bytes) -> StatsXmlFile:
    """Parse a .stats file from bytes."""
    data, had_bom = _strip_bom(data)

    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError as e:
        raise StatsXmlParseError(f"invalid XML: {e}") from e

    if root.tag != "stats":
        raise StatsXmlParseError(f"expected root <stats>, got <{root.tag}>")

    sod_id = root.get("stat_object_definition_id")
    if sod_id is None:
        raise StatsXmlParseError(
            "<stats> missing stat_object_definition_id"
        )

    objects: list[StatsXmlObject] = []
    for child in root:
        if isinstance(child, etree._Comment):
            continue
        if child.tag != "stat_objects":
            continue
        for grandchild in child:
            if isinstance(grandchild, etree._Comment):
                continue
            if grandchild.tag == "stat_object":
                objects.append(_parse_object(grandchild))

    return StatsXmlFile(
        stat_object_definition_id=sod_id,
        objects=objects,
        had_bom=had_bom,
    )


def parse_file(path: Path | str) -> StatsXmlFile:
    path = Path(path)
    return parse_bytes(path.read_bytes())


def write_file(stats: StatsXmlFile, path: Path | str) -> None:
    path = Path(path)
    io_util.write_bytes_safe(path, stats.to_xml_bytes())


# --- Merging --------------------------------------------------------------

@dataclass
class StatsXmlConflict:
    """A name collision between two .stats objects with differing content."""
    name: str
    a: StatsXmlObject
    b: StatsXmlObject


def _is_canmerge(obj: StatsXmlObject) -> bool:
    """True if the stat object opts into the game's runtime CanMerge
    behavior (a ``<field name="CanMerge" value="Yes"/>`` is present).

    Used by the merger to decide whether two primaries with the same Name
    should both be kept (matching the game's runtime intent and the .txt
    side's concatenation behavior) or treated as a name conflict.
    """
    f = obj.field_by_name("CanMerge")
    if f is None:
        return False
    return (f.value or "").strip().lower() == "yes"


def diff_objects(a: StatsXmlObject, b: StatsXmlObject) -> list[str]:
    """Field-by-field diff. Empty list = identical."""
    diffs: list[str] = []
    if a.is_substat != b.is_substat:
        diffs.append(f"is_substat: {a.is_substat} vs {b.is_substat}")

    # Compare field sets (name+type+extra).
    a_by_name = {f.name: f for f in a.fields}
    b_by_name = {f.name: f for f in b.fields}

    for name in sorted(set(a_by_name) | set(b_by_name)):
        af = a_by_name.get(name)
        bf = b_by_name.get(name)
        if af is None:
            diffs.append(f"field only in B: {name}")
        elif bf is None:
            diffs.append(f"field only in A: {name}")
        elif af.type != bf.type:
            diffs.append(f"field {name}: type {af.type} vs {bf.type}")
        elif af.extra != bf.extra:
            diffs.append(f"field {name}: extra {af.extra} vs {bf.extra}")
    return diffs


def merge(
    a: StatsXmlFile,
    b: StatsXmlFile,
    *,
    prefix_b_on_conflict: str | None = None,
) -> tuple[StatsXmlFile, list[StatsXmlConflict]]:
    """Merge two .stats files. Both must have the same stat_object_definition_id.

    Strategy mirrors stats_text.merge:
    - Objects unique to A → kept.
    - Objects unique to B → appended.
    - Same Name in both (primary stats only):
        * identical → silent dedup
        * different + prefix → rename B's Name with the prefix
        * different + no prefix → conflict recorded, B's omitted
    - Substats (``is_substat=true``) are identified by UUID, NOT by Name:
      multiple substats can legitimately share a Name (treasure-table
      sub-rolls are the classic case). Same UUID → silent dedup; different
      UUIDs → all kept.
    """
    if a.stat_object_definition_id != b.stat_object_definition_id:
        raise ValueError(
            "cannot merge .stats files with different stat_object_definition_id "
            f"({a.stat_object_definition_id!r} vs {b.stat_object_definition_id!r}); "
            "this would mean merging e.g. SpellData with StatusData"
        )

    out = StatsXmlFile(
        stat_object_definition_id=a.stat_object_definition_id,
        had_bom=a.had_bom,
    )
    conflicts: list[StatsXmlConflict] = []

    # Build A's lookup tables. Primary stats keyed by Name; substats keyed
    # by UUID. Both keys can coexist without collision in normal data.
    a_primary_by_name: dict[str, StatsXmlObject] = {}
    a_substat_by_uuid: dict[str, StatsXmlObject] = {}
    for obj in a.objects:
        if obj.is_substat:
            if obj.uuid:
                a_substat_by_uuid[obj.uuid.lower()] = obj
        elif obj.name is not None:
            a_primary_by_name[obj.name] = obj

    # First: A's objects in original order.
    for obj in a.objects:
        out.objects.append(obj)

    # Then: B's objects.
    for obj in b.objects:
        # Substat path: dedup by UUID.
        if obj.is_substat:
            ukey = obj.uuid.lower() if obj.uuid else None
            if ukey is None or ukey not in a_substat_by_uuid:
                out.objects.append(obj)
                continue
            # Same UUID in both: silent dedup (A wins).
            if not diff_objects(a_substat_by_uuid[ukey], obj):
                continue
            # Same UUID, different content: treat as a UUID conflict.
            # Don't try to prefix-rename a UUID; just record and skip B's.
            conflicts.append(StatsXmlConflict(
                name=obj.name or ukey,
                a=a_substat_by_uuid[ukey], b=obj,
            ))
            continue

        # Primary stat path: dedup by Name.
        name = obj.name
        if name is None or name not in a_primary_by_name:
            out.objects.append(obj)
            continue

        a_obj = a_primary_by_name[name]
        if not diff_objects(a_obj, obj):
            # Identical: silent dedup; A's copy is already in out.
            continue

        # Two primaries with the same Name AND both opting into the game's
        # runtime merge (CanMerge=Yes) → mirror the txt-side concatenation:
        # keep both, identifying them by their distinct UUIDs. This keeps
        # any substats pointing at B's primary UUID resolvable and matches
        # what the regenerated .txt would express.
        if _is_canmerge(a_obj) and _is_canmerge(obj):
            out.objects.append(obj)
            continue

        # Real conflict.
        if prefix_b_on_conflict is not None:
            # Clone B's object with the Name field rewritten.
            new_fields: list[StatsXmlField] = []
            for f in obj.fields:
                if f.name == "Name":
                    new_extra = dict(f.extra)
                    new_extra["value"] = f"{prefix_b_on_conflict}{f.value or ''}"
                    new_fields.append(StatsXmlField(
                        name=f.name, type=f.type, extra=new_extra,
                    ))
                else:
                    new_fields.append(StatsXmlField(
                        name=f.name, type=f.type, extra=dict(f.extra),
                    ))
            out.objects.append(StatsXmlObject(
                is_substat=obj.is_substat,
                fields=new_fields,
            ))

        conflicts.append(StatsXmlConflict(name=name, a=a_obj, b=obj))

    return out, conflicts
