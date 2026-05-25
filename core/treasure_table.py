"""Parser/writer for the ``TreasureTable.txt`` line-based format.

Lives at ``Public/<ModFolder>/Stats/Generated/TreasureTable.txt`` (one level
shallower than the regular stats .txt files under ``Generated/Data/``).
The format is NOT the same as ``new entry "X"`` style — it has its own
grammar that the game's loot system parses.

Example::

    treasure itemtypes "Common","Uncommon","Rare","Epic","Legendary","Divine","Unique"
    new treasuretable "TUT_Chest_Potions"
    CanMerge 1
    new subtable "1,1"
    object category "I_BloodFang_Helmet2",1,0,0,0,0,0,0,0
    new subtable "1,1"
    object category "I_BloodFang_Armor2",1,0,0,0,0,0,0,0

    new treasuretable "MysticFountain"
    new subtable "1,1"
    object category "I_CONS_DRINK_Water_Bottle",1,0,0,0,0,0,0,0

Grammar (informal):

    file       := header table*
    header     := 'treasure itemtypes' QUOTED ("," QUOTED)*
    table      := 'new treasuretable' QUOTED (flag-line | subtable)*
    flag-line  := KEYWORD ARG              e.g. ``CanMerge 1``
    subtable   := 'new subtable' QUOTED object*
    object     := 'object category' QUOTED ("," NUMBER)+

Notes from real fixtures:

- The ``treasure itemtypes`` header is global to the file and (as far as
  observed) identical across all real mods because it's the game's
  canonical column ordering. We require it to match when merging two
  files; mismatches indicate the game changed its rarity tiers and we
  should not silently mix.
- ``CanMerge 1`` is the game's RUNTIME merge flag: at load time, BG3 will
  combine same-named tables across mods if both opt in. Our static merge
  is independent of this — when two mods statically merge, we have to
  pick one set of subtables OR concatenate them. We choose concatenation
  for identical-name tables both carrying ``CanMerge 1``; for mismatched
  flags we fall back to the conflict policy.
- Files use CRLF and we preserve that on write.
- Sub-table weight rows can have any number of numeric columns matching
  the header length. We treat the row body as an opaque string after the
  quoted object category.
"""

from __future__ import annotations

from . import io_util

import re
from dataclasses import dataclass, field
from pathlib import Path


# Single quoted string with no internal escape handling — matches stats_text.
_QUOTED = re.compile(r'"([^"]*)"')


@dataclass
class TreasureSubtable:
    """One ``new subtable`` block: a drop-count range plus a list of object
    rows. Each row is ``(category, weights_csv)`` where ``weights_csv`` is
    the raw comma-separated weight string preserved verbatim."""
    drop_count: str  # e.g. "1,1" — min,max as a string (preserved verbatim)
    objects: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class TreasureTable:
    """One ``new treasuretable`` block."""
    name: str
    flags: list[tuple[str, str]] = field(default_factory=list)  # (key, value)
    subtables: list[TreasureSubtable] = field(default_factory=list)

    def flag_value(self, key: str) -> str | None:
        for k, v in self.flags:
            if k == key:
                return v
        return None

    @property
    def can_merge(self) -> bool:
        """True if the table opts into the game's runtime merge behavior."""
        v = self.flag_value("CanMerge")
        return v is not None and v.strip() == "1"


@dataclass
class TreasureTableFile:
    """A parsed TreasureTable.txt."""
    itemtypes: list[str] = field(default_factory=list)  # the header column names
    tables: list[TreasureTable] = field(default_factory=list)
    line_ending: str = "\r\n"
    trailing_newlines: int = 1

    def by_name(self, name: str) -> TreasureTable | None:
        for t in self.tables:
            if t.name == name:
                return t
        return None


class TreasureParseError(ValueError):
    """Raised when a TreasureTable.txt file is structurally invalid."""


# --- Parsing -----------------------------------------------------------------


def _detect_line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def parse_text(text: str) -> TreasureTableFile:
    line_ending = _detect_line_ending(text)
    lines = text.replace("\r\n", "\n").split("\n")

    out = TreasureTableFile(line_ending=line_ending)
    current_table: TreasureTable | None = None
    current_subtable: TreasureSubtable | None = None

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue

        if stripped.startswith("treasure itemtypes"):
            rest = stripped[len("treasure itemtypes"):]
            out.itemtypes = _QUOTED.findall(rest)
            if not out.itemtypes:
                raise TreasureParseError(
                    f"treasure itemtypes line has no quoted columns: {raw_line!r}"
                )
            continue

        if stripped.startswith("new treasuretable"):
            quoted = _QUOTED.findall(stripped[len("new treasuretable"):])
            if len(quoted) != 1:
                raise TreasureParseError(
                    f"new treasuretable expects exactly 1 quoted arg: {raw_line!r}"
                )
            current_table = TreasureTable(name=quoted[0])
            current_subtable = None
            out.tables.append(current_table)
            continue

        if stripped.startswith("new subtable"):
            if current_table is None:
                raise TreasureParseError(
                    f"new subtable outside any treasuretable: {raw_line!r}"
                )
            quoted = _QUOTED.findall(stripped[len("new subtable"):])
            if len(quoted) != 1:
                raise TreasureParseError(
                    f"new subtable expects exactly 1 quoted arg: {raw_line!r}"
                )
            current_subtable = TreasureSubtable(drop_count=quoted[0])
            current_table.subtables.append(current_subtable)
            continue

        if stripped.startswith("object category"):
            if current_subtable is None:
                raise TreasureParseError(
                    f"object category line outside any subtable: {raw_line!r}"
                )
            # Body is: "QUOTED",w1,w2,w3,...
            body = stripped[len("object category"):].lstrip()
            m = _QUOTED.match(body)
            if not m:
                raise TreasureParseError(
                    f"object category line missing quoted category: {raw_line!r}"
                )
            category = m.group(1)
            rest = body[m.end():]
            # Should start with a comma followed by the weights.
            weights_csv = rest.lstrip(",").strip()
            current_subtable.objects.append((category, weights_csv))
            continue

        # Otherwise: a flag line on the current table. Format is "Key Value".
        if current_table is not None and current_subtable is None:
            parts = stripped.split(maxsplit=1)
            if len(parts) == 2:
                current_table.flags.append((parts[0], parts[1]))
            else:
                current_table.flags.append((parts[0], ""))
            continue

        raise TreasureParseError(
            f"unrecognized line in TreasureTable.txt: {raw_line!r}"
        )

    # Count trailing blank lines for round-trip fidelity.
    trailing = 0
    for raw_line in reversed(lines):
        if raw_line.strip() == "":
            trailing += 1
        else:
            break
    out.trailing_newlines = max(trailing, 1)
    return out


def parse_file(path: Path | str) -> TreasureTableFile:
    path = Path(path)
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return parse_text(data.decode("utf-8"))


# --- Writing -----------------------------------------------------------------


def _quote(s: str) -> str:
    if '"' in s:
        raise ValueError(f"treasure table value contains a literal quote: {s!r}")
    return f'"{s}"'


def serialize(f: TreasureTableFile) -> str:
    le = f.line_ending
    out: list[str] = []
    if f.itemtypes:
        out.append(
            "treasure itemtypes " + ",".join(_quote(s) for s in f.itemtypes)
        )
    for table in f.tables:
        out.append(f"new treasuretable {_quote(table.name)}")
        for key, value in table.flags:
            if value:
                out.append(f"{key} {value}")
            else:
                out.append(key)
        for sub in table.subtables:
            out.append(f"new subtable {_quote(sub.drop_count)}")
            for category, weights_csv in sub.objects:
                out.append(f"object category {_quote(category)},{weights_csv}")
    body = le.join(out) + le * f.trailing_newlines
    return body


def write_file(f: TreasureTableFile, path: Path | str) -> None:
    io_util.write_bytes_safe(path, serialize(f).encode("utf-8"))


# --- Merging ----------------------------------------------------------------


@dataclass
class TreasureConflict:
    name: str
    a: TreasureTable
    b: TreasureTable


def _tables_equal(a: TreasureTable, b: TreasureTable) -> bool:
    """Structural equality. Order of subtables matters; weights and
    categories must match exactly."""
    return (
        a.name == b.name
        and a.flags == b.flags
        and len(a.subtables) == len(b.subtables)
        and all(
            sa.drop_count == sb.drop_count and sa.objects == sb.objects
            for sa, sb in zip(a.subtables, b.subtables)
        )
    )


def merge(
    a: TreasureTableFile,
    b: TreasureTableFile,
    *,
    prefix_b_on_conflict: str | None = None,
) -> tuple[TreasureTableFile, list[TreasureConflict]]:
    """Merge two TreasureTable files by table name.

    Strategy:
    - The itemtypes header must match. (If it doesn't, Larian has changed
      the rarity column ordering between the two source mods — refuse to
      silently mix because weight columns wouldn't line up.)
    - Tables unique to either input → kept.
    - Same table name in both:
        * identical content → silent dedup
        * **both have CanMerge=1** → concatenate B's subtables onto A's. This
          mirrors the game's own runtime behavior for CanMerge tables —
          the merged loot pool draws from both authors' subtables. No
          conflict is recorded since this is the documented intent.
        * different content + ``prefix_b_on_conflict`` set → rename B's
          name with the prefix, keep both as distinct tables.
        * different content + no prefix → conflict recorded, B's omitted.
    """
    if a.itemtypes and b.itemtypes and a.itemtypes != b.itemtypes:
        raise ValueError(
            f"cannot merge TreasureTable files with different itemtypes columns: "
            f"{a.itemtypes!r} vs {b.itemtypes!r}"
        )

    out = TreasureTableFile(
        itemtypes=list(a.itemtypes or b.itemtypes),
        line_ending=a.line_ending,
        trailing_newlines=a.trailing_newlines,
    )
    conflicts: list[TreasureConflict] = []

    a_by_name = {t.name: t for t in a.tables}

    # First: every table from A in original order, possibly with B's
    # CanMerge subtables concatenated when both opt in.
    for t in a.tables:
        b_table = next((bt for bt in b.tables if bt.name == t.name), None)
        if b_table is not None and _tables_equal(t, b_table):
            # identical — dedup
            out.tables.append(t)
            continue
        if b_table is not None and t.can_merge and b_table.can_merge:
            # CanMerge concatenation — combine subtables.
            combined = TreasureTable(
                name=t.name, flags=list(t.flags),
                subtables=list(t.subtables) + list(b_table.subtables),
            )
            out.tables.append(combined)
            continue
        # Otherwise, A wins for now; the conflict (if any) is handled below.
        out.tables.append(t)

    # Then: B's tables, with conflict handling for those that overlap A.
    for t in b.tables:
        if t.name not in a_by_name:
            out.tables.append(t)
            continue
        a_t = a_by_name[t.name]
        if _tables_equal(a_t, t):
            continue  # already deduped above
        if a_t.can_merge and t.can_merge:
            continue  # already concatenated above
        # Real conflict.
        if prefix_b_on_conflict is not None:
            renamed = TreasureTable(
                name=f"{prefix_b_on_conflict}{t.name}",
                flags=list(t.flags),
                subtables=list(t.subtables),
            )
            out.tables.append(renamed)
        conflicts.append(TreasureConflict(name=t.name, a=a_t, b=t))

    return out, conflicts
