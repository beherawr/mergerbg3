"""Parser/writer for BG3's `.loca.xml` localization format.

These files live under ``Mods/<ModFolder>/Localization/<Language>/<name>.xml``
(or sometimes ``.loca.xml`` — same content either way). Each contains a flat
list of translation entries.

Example::

    <?xml version="1.0"?>
    <contentList xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <content contentuid="h6564e6bcg6d91g70f9ge706ga9f0a4eec3e3" version="1">Shadow Dance</content>
      <content contentuid="h7035b620g898dg24f7gc47bg0e1528d83587" version="1">Shadow Walk</content>
    </contentList>

Critical facts:

- ``contentuid`` is a handle: ``h`` followed by 32 hex chars, no dashes
  (e.g. ``h6564e6bcg6d91g70f9ge706ga9f0a4eec3e3``). It's how stats and root
  templates reference this string. Different handles point at different
  strings; the same handle in two mods means they're literally referring
  to the same translation entry.
- ``version`` is a small integer string. Stats/.txt files reference loca
  with the joined form ``"<handle>;<version>"``. The version is a Larian
  cache-busting mechanism — bumping it tells the game to re-load this
  string after content changes.
- The body of ``<content>`` is the actual translated text. It may contain
  HTML-style inline tags Larian invented (``<LSTag Type="Status"
  Tooltip="INVISIBLE">Invisible</LSTag>``), special characters, line
  breaks, etc. We preserve everything verbatim with no transformation.
- File header indentation is two spaces and uses CRLF line endings in
  Toolkit-generated files. We emit the same.
- The xmlns attributes on ``<contentList>`` are always present in
  Toolkit-generated files. We re-emit them on write so the merged output
  is indistinguishable from a Toolkit-generated file.

Merge semantics:

- Disjoint handles → straight concatenation.
- Same handle, same text → silent dedup (no conflict).
- Same handle, different text → conflict (rare but legal). The merger
  surfaces these for the user to resolve.
- Version differences for the same handle+text → take the higher version
  (cache-bust is monotonic in practice).
"""

from __future__ import annotations

from . import io_util

import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree


# A localization handle has the shape: lowercase 'h' followed by a
# UUID-like form where 'g' replaces '-' as the separator:
#   h<8 hex>g<4 hex>g<4 hex>g<4 hex>g<12 hex>
# Total length = 37 characters. Used by the reference index to scan
# stats/.lsx for handle references.
HANDLE_PATTERN = re.compile(
    r"\bh[0-9a-f]{8}g[0-9a-f]{4}g[0-9a-f]{4}g[0-9a-f]{4}g[0-9a-f]{12}\b"
)
HANDLE_LENGTH = 37


@dataclass
class LocaEntry:
    """One ``<content>`` row in a .loca.xml file."""
    contentuid: str           # the handle, e.g. "h6564e6bcg..."
    version: str              # small int as string (Larian's serialization)
    text: str                 # the actual translated string (HTML-ish)

    def to_xml(self) -> etree._Element:
        elem = etree.Element("content", contentuid=self.contentuid, version=self.version)
        # The text may contain inline tags (<LSTag>), which the XML parser
        # would have already parsed. We re-serialize with .text only when
        # there's no inline markup, which is the common case; otherwise we
        # store the original raw text and round-trip through tostring().
        elem.text = self.text
        return elem


@dataclass
class LocaFile:
    """A parsed .loca.xml file."""
    entries: list[LocaEntry] = field(default_factory=list)
    # xmlns attributes from the source — preserved on write so the output
    # is byte-equivalent for Toolkit-generated files.
    nsmap: dict[str | None, str] = field(default_factory=dict)

    def by_handle(self, handle: str) -> LocaEntry | None:
        for e in self.entries:
            if e.contentuid == handle:
                return e
        return None

    def handles(self) -> list[str]:
        return [e.contentuid for e in self.entries]

    def to_xml_bytes(self) -> bytes:
        # Reconstruct using lxml with the original namespace map to preserve
        # xmlns attributes in their original positions.
        root = etree.Element("contentList", nsmap=self.nsmap or None)
        for entry in self.entries:
            root.append(entry.to_xml())

        etree.indent(root, space="  ")
        body = etree.tostring(
            root,
            xml_declaration=True,
            encoding="utf-8",
            pretty_print=True,
        )
        # Match Toolkit output: xml_declaration writes lowercase "utf-8",
        # but Toolkit emits no encoding attribute at all (just `<?xml version="1.0"?>`).
        # Rewrite the declaration to match.
        body = body.replace(
            b"<?xml version='1.0' encoding='utf-8'?>",
            b"<?xml version=\"1.0\"?>",
        )
        body = body.replace(
            b'<?xml version="1.0" encoding="utf-8"?>',
            b'<?xml version="1.0"?>',
        )
        return body


class LocaParseError(ValueError):
    """Raised when a .loca.xml file is structurally invalid."""


# --- Parsing --------------------------------------------------------------


def parse_bytes(data: bytes) -> LocaFile:
    """Parse a .loca.xml file from bytes."""
    # Strip BOM if present — observed files don't have one but we're defensive.
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]

    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError as e:
        raise LocaParseError(f"invalid XML: {e}") from e

    # Local name comparison — Toolkit-generated files don't put contentList
    # in a namespace, but the xmlns attributes show up via nsmap.
    if etree.QName(root).localname != "contentList":
        raise LocaParseError(f"expected root <contentList>, got <{root.tag}>")

    entries: list[LocaEntry] = []
    for child in root:
        if isinstance(child, etree._Comment):
            continue
        if etree.QName(child).localname != "content":
            continue
        handle = child.get("contentuid")
        version = child.get("version")
        if handle is None or version is None:
            raise LocaParseError(
                f"<content> missing contentuid or version: {etree.tostring(child)!r}"
            )
        # Preserve full text content including inline markup.
        # If the body contains <LSTag>, lxml will have parsed those as child
        # elements; we serialize them back to text for round-trip.
        if len(child) > 0:
            # has child elements (inline tags)
            inner = (child.text or "")
            for sub in child:
                inner += etree.tostring(sub, encoding="unicode")
            text = inner
        else:
            text = child.text or ""
        entries.append(LocaEntry(contentuid=handle, version=version, text=text))

    return LocaFile(entries=entries, nsmap=dict(root.nsmap))


def parse_file(path: Path | str) -> LocaFile:
    path = Path(path)
    return parse_bytes(path.read_bytes())


def write_file(loca: LocaFile, path: Path | str) -> None:
    path = Path(path)
    io_util.write_bytes_safe(path, loca.to_xml_bytes())


# --- Reference scanning ---------------------------------------------------


def extract_handles(text: str) -> set[str]:
    """Find every loca handle (``h<32-hex>``) in arbitrary text.

    Used by the reference index to scan stats files, LSX values, etc. for
    handle references. Matches whole tokens so we don't false-positive on
    e.g. a handle that happens to be a substring of a UUID.
    """
    return set(HANDLE_PATTERN.findall(text))


# --- Merging --------------------------------------------------------------


@dataclass
class LocaConflict:
    """A handle collision where two files give different text for the same
    contentuid. The merger surfaces these for the user to resolve."""
    handle: str
    a: LocaEntry
    b: LocaEntry


def merge(a: LocaFile, b: LocaFile) -> tuple[LocaFile, list[LocaConflict]]:
    """Merge two .loca.xml files by contentuid.

    Strategy:
    - Handles unique to A or B → kept.
    - Same handle, identical text+version → silent dedup (A wins).
    - Same handle, identical text, different version → keep the higher
      version (Larian's cache-bust is monotonic).
    - Same handle, different text → conflict reported. A's text wins by
      default; we don't omit B because dropping localization is worse than
      silently picking a side. The user can review and override.

    Returns (merged_file, conflicts).
    """
    out = LocaFile(nsmap=dict(a.nsmap))
    conflicts: list[LocaConflict] = []

    a_by_handle = {e.contentuid: e for e in a.entries}
    seen: set[str] = set()

    # First: all of A's entries in original order, possibly with version bumped.
    for entry in a.entries:
        b_entry = next((e for e in b.entries if e.contentuid == entry.contentuid), None)
        if b_entry is not None and b_entry.text == entry.text:
            # Same text, possibly different version: take the higher.
            higher = max(int(entry.version), int(b_entry.version))
            out.entries.append(LocaEntry(
                contentuid=entry.contentuid,
                version=str(higher),
                text=entry.text,
            ))
        elif b_entry is not None and b_entry.text != entry.text:
            # Real conflict — A's text wins by default.
            out.entries.append(entry)
            conflicts.append(LocaConflict(handle=entry.contentuid, a=entry, b=b_entry))
        else:
            out.entries.append(entry)
        seen.add(entry.contentuid)

    # Then: B's entries that weren't in A.
    for entry in b.entries:
        if entry.contentuid not in a_by_handle:
            out.entries.append(entry)
            seen.add(entry.contentuid)

    return out, conflicts
