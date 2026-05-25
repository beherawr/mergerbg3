"""LSX (Larian Studios XML) wrapper.

LSX is the human-readable form of Larian's resource files. Every LSX file
follows the same outer skeleton::

    <save>
        <version major="N" minor="N" revision="N" build="N" [lslib_meta="..."]/>
        <region id="RegionName">
            <node id="NodeName">
                <attribute id="Key" type="TypeName" value="..."/>
                <children>
                    <node id="ChildNode">...</node>
                </children>
            </node>
        </region>
    </save>

The format details that bite you:

- Attributes carry a `type` (FixedString, LSString, guid, TranslatedString,
  uint64, int64, int32, bool, uint8, ...). Type-aware comparison is
  occasionally needed; we keep the type in the model.
- TranslatedString attributes use `handle` and `version` instead of `value`.
- Some files (e.g. _merged.lsf.lsx) carry an `lslib_meta` attribute on
  `<version>` that records LSLib serialization options (e.g. "v1,bswap_guids").
  We preserve this verbatim.
- Multiple `<region>` siblings inside `<save>` are legal — e.g. a banks file
  with VisualBank + MaterialBank + TextureBank as three regions.
- Some toolkit-generated files start with a UTF-8 BOM. We handle both with
  and without.

This module is intentionally low-level: it parses to a faithful tree, lets
callers walk it, and writes it back with stable formatting. Higher-level
modules (atlas, references, meta) sit on top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from lxml import etree


# --- Model ----------------------------------------------------------------

@dataclass
class Attribute:
    """One <attribute id="..." type="..." value="..."/> element.

    For TranslatedString attributes, ``value`` is None and ``handle``/``version``
    carry the localization reference instead. We keep these as separate fields
    rather than overloading ``value`` because the two are semantically different
    (a value is a literal; a handle is a pointer to the loca file).
    """
    id: str
    type: str
    value: str | None = None
    handle: str | None = None  # only set when type == "TranslatedString"
    version: str | None = None  # only set when type == "TranslatedString"

    def to_xml(self) -> etree._Element:
        elem = etree.Element("attribute", id=self.id, type=self.type)
        if self.type == "TranslatedString":
            if self.handle is not None:
                elem.set("handle", self.handle)
            if self.version is not None:
                elem.set("version", self.version)
        else:
            if self.value is not None:
                elem.set("value", self.value)
        return elem


@dataclass
class Node:
    """One <node id="..."> element, with attributes and child nodes.

    A node maps to a single "object" or "record" in Larian's model
    (a Resource in a TextureBank, a GameObject in RootTemplates, etc.).
    Order matters in serialization, so we use lists rather than dicts.
    """
    id: str
    attributes: list[Attribute] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)

    def attr(self, attr_id: str) -> Attribute | None:
        """Look up an attribute by id. None if absent."""
        for a in self.attributes:
            if a.id == attr_id:
                return a
        return None

    def attr_value(self, attr_id: str, default: str | None = None) -> str | None:
        """Convenience: get an attribute's value (or None) without unwrapping."""
        a = self.attr(attr_id)
        if a is None:
            return default
        return a.value

    def children_by_id(self, node_id: str) -> list["Node"]:
        """Direct children matching the given id."""
        return [c for c in self.children if c.id == node_id]

    def walk(self) -> Iterator["Node"]:
        """Pre-order traversal of self and all descendants."""
        yield self
        for child in self.children:
            yield from child.walk()

    def to_xml(self) -> etree._Element:
        elem = etree.Element("node", id=self.id)
        for a in self.attributes:
            elem.append(a.to_xml())
        if self.children:
            children_elem = etree.SubElement(elem, "children")
            for child in self.children:
                children_elem.append(child.to_xml())
        return elem


@dataclass
class Region:
    """One <region id="..."> top-level element."""
    id: str
    root_node: Node  # always exactly one direct child <node>

    def to_xml(self) -> etree._Element:
        elem = etree.Element("region", id=self.id)
        elem.append(self.root_node.to_xml())
        return elem


@dataclass
class Version:
    """The <version .../> element. Larian uses this for format compatibility.

    The ``extra`` dict catches any future attributes (Larian has added some
    over time, like ``lslib_meta``) so we preserve them on write.
    """
    major: str
    minor: str
    revision: str
    build: str
    extra: dict[str, str] = field(default_factory=dict)

    def to_xml(self) -> etree._Element:
        elem = etree.Element("version")
        elem.set("major", self.major)
        elem.set("minor", self.minor)
        elem.set("revision", self.revision)
        elem.set("build", self.build)
        for k, v in self.extra.items():
            elem.set(k, v)
        return elem


@dataclass
class LsxDocument:
    """A parsed LSX file: version + one-or-more regions.

    Preserves the order of regions as they appear in source — some Larian
    code may be order-dependent, so we don't try to canonicalize.
    """
    version: Version
    regions: list[Region]
    had_bom: bool = False  # remember so we can write it back if it was there

    def region(self, region_id: str) -> Region | None:
        """First region with the given id, or None."""
        for r in self.regions:
            if r.id == region_id:
                return r
        return None

    def to_xml_bytes(self) -> bytes:
        """Serialize to the on-disk byte representation.

        Larian writes UTF-8 with an XML declaration, indented with four spaces.
        We match that.
        """
        root = etree.Element("save")
        root.append(self.version.to_xml())
        for region in self.regions:
            root.append(region.to_xml())

        # lxml's pretty_print adds 2-space indent; Larian uses 4-space.
        # We do our own re-indentation pass after pretty-printing for byte-exact
        # round-trips where possible. (Where not, we still produce valid LSX.)
        etree.indent(root, space="    ")
        body = etree.tostring(
            root,
            xml_declaration=True,
            encoding="UTF-8",
            pretty_print=True,
        )
        if self.had_bom:
            body = b"\xef\xbb\xbf" + body
        return body


# --- Parsing --------------------------------------------------------------

class LsxParseError(ValueError):
    """Raised when an LSX file is structurally invalid."""


def _strip_bom(data: bytes) -> tuple[bytes, bool]:
    """Strip UTF-8 BOM if present. Return (data, had_bom)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:], True
    return data, False


def _parse_attribute(elem: etree._Element) -> Attribute:
    """Convert an <attribute> XML element to our model.

    Note: the TranslatedString case uses `handle`/`version` instead of `value`.
    We do NOT raise if `value` is present on a TranslatedString (defensive: some
    files in the wild have it); we just ignore it on read.
    """
    attr_id = elem.get("id")
    attr_type = elem.get("type")
    if attr_id is None or attr_type is None:
        raise LsxParseError(
            f"<attribute> missing id or type: {etree.tostring(elem, pretty_print=True)!r}"
        )

    if attr_type == "TranslatedString":
        return Attribute(
            id=attr_id,
            type=attr_type,
            value=None,
            handle=elem.get("handle"),
            version=elem.get("version"),
        )
    else:
        return Attribute(
            id=attr_id,
            type=attr_type,
            value=elem.get("value"),
        )


def _parse_node(elem: etree._Element) -> Node:
    """Convert a <node> XML element to our model.

    A node may have:
      - zero or more direct <attribute> children
      - at most one <children> wrapper, containing further <node> elements

    Anything else (comments, whitespace) is ignored.
    """
    node_id = elem.get("id")
    if node_id is None:
        raise LsxParseError(
            f"<node> missing id: {etree.tostring(elem, pretty_print=True)!r}"
        )

    attributes: list[Attribute] = []
    children: list[Node] = []

    for child in elem:
        if isinstance(child, etree._Comment):
            continue
        tag = child.tag
        if tag == "attribute":
            attributes.append(_parse_attribute(child))
        elif tag == "children":
            for grandchild in child:
                if isinstance(grandchild, etree._Comment):
                    continue
                if grandchild.tag != "node":
                    raise LsxParseError(
                        f"unexpected <{grandchild.tag}> inside <children> "
                        f"under node id={node_id!r}"
                    )
                children.append(_parse_node(grandchild))
        else:
            # Forward-compat: unknown elements are skipped with no error.
            # Larian has historically added new structural elements; we'd rather
            # the merger keep working than crash on a future addition.
            continue

    return Node(id=node_id, attributes=attributes, children=children)


def _parse_region(elem: etree._Element) -> Region:
    """A <region> contains exactly one root <node>."""
    region_id = elem.get("id")
    if region_id is None:
        raise LsxParseError("<region> missing id")

    root_node: Node | None = None
    for child in elem:
        if isinstance(child, etree._Comment):
            continue
        if child.tag != "node":
            raise LsxParseError(
                f"unexpected <{child.tag}> inside <region id={region_id!r}>"
            )
        if root_node is not None:
            raise LsxParseError(
                f"region id={region_id!r} has more than one root <node>"
            )
        root_node = _parse_node(child)

    if root_node is None:
        raise LsxParseError(f"region id={region_id!r} has no root <node>")
    return Region(id=region_id, root_node=root_node)


def parse_bytes(data: bytes) -> LsxDocument:
    """Parse an LSX file from bytes.

    Accepts BOM or no BOM, any whitespace style. Raises LsxParseError on
    structural problems.
    """
    data, had_bom = _strip_bom(data)

    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError as e:
        raise LsxParseError(f"invalid XML: {e}") from e

    if root.tag != "save":
        raise LsxParseError(f"expected root <save>, got <{root.tag}>")

    version: Version | None = None
    regions: list[Region] = []
    for child in root:
        if isinstance(child, etree._Comment):
            continue
        if child.tag == "version":
            known = {"major", "minor", "revision", "build"}
            extra = {k: v for k, v in child.attrib.items() if k not in known}
            version = Version(
                major=child.get("major", "0"),
                minor=child.get("minor", "0"),
                revision=child.get("revision", "0"),
                build=child.get("build", "0"),
                extra=extra,
            )
        elif child.tag == "region":
            regions.append(_parse_region(child))

    if version is None:
        # Defensive default — every Larian file has <version>, but if one
        # didn't we'd rather assume a sane modern build than fail outright.
        version = Version(major="4", minor="0", revision="0", build="0")

    return LsxDocument(version=version, regions=regions, had_bom=had_bom)


def parse_file(path: Path | str) -> LsxDocument:
    """Convenience: load and parse an LSX file from disk."""
    path = Path(path)
    return parse_bytes(path.read_bytes())


def write_file(doc: LsxDocument, path: Path | str) -> None:
    """Write an LSX document to disk, preserving BOM presence."""
    from . import io_util
    io_util.write_bytes_safe(path, doc.to_xml_bytes())
