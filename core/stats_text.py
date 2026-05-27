"""Parser/writer for the line-based stats `.txt` format.

This is the format the game actually loads at runtime from packed mods,
found at ``Public/<ModFolder>/Stats/Generated/Data/<TypeName>.txt``. Larian
also uses the same format for the base game's `Shared` and `Gustav` pak
contents.

Example::

    new entry "Target_BackstabK"
    type "SpellData"
    data "SpellType" "Target"
    data "SpellProperties" "GROUND:TeleportSource(true,true);"
    data "TargetRadius" "18"

    new entry "Shout_ShadowDanceK"
    type "SpellData"
    using "_BaseSpell"
    data "DisplayName" "h6564e6bcg6d91g70f9ge706ga9f0a4eec3e3;1"

Grammar (informal):

    file        := (entry | blank-line | comment)*
    entry       := 'new entry' STRING (type-line | using-line | data-line)*
    type-line   := 'type' STRING
    using-line  := 'using' STRING
    data-line   := 'data' STRING STRING

Notes from the wild:
- Files use CRLF on Windows. We preserve that on write to match exactly.
- Toolkit-generated files end every entry with a blank line. We do too.
- Values can contain spaces, semicolons, parentheses, commas: anything
  except a literal " inside the quoted string. We have not seen escaping
  for embedded quotes in any official or community mod, so the parser
  treats `"..."` as "everything up to the next unescaped quote." If we
  encounter escaping in the wild, we'll add it then.
- The TranslatedString encoding is ``"<handle>;<version>"`` (e.g.
  ``"h6564e6bcg...;1"``). The parser preserves it as a plain string; the
  reference index walks data values to extract handle refs separately.
- Order of `data` lines within an entry matters for human review but not
  for the game. We preserve order on round-trip.
- Comments (``// ...``) appear rarely in user mods but are present in
  some base-game files. We preserve them verbatim if attached to an entry.
"""

from __future__ import annotations

from . import io_util

import re
from dataclasses import dataclass, field
from pathlib import Path


# Match a quoted string. Non-greedy, doesn't handle backslash-escapes (because
# the format doesn't use them). Captures the contents without the quotes.
_QUOTED = re.compile(r'"([^"]*)"')


@dataclass
class StatsEntry:
    """One ``new entry ...`` block in a stats .txt file.

    The ``data`` list is ordered and may contain duplicate keys: the
    format permits a key to appear multiple times (e.g. the same boost
    repeated), and we preserve that faithfully.

    ``using_index`` records the position of the ``using`` line relative to
    the ``data`` lines in the source file. Larian's convention puts
    ``using`` right after ``type``, but some authors interleave it with
    ``data`` lines (Bloodfang's Spell_Shout.txt is a real example). The
    game doesn't care; we preserve order for byte-exact round-trip.

    ``using_index`` semantics:
      - None or 0: ``using`` line comes BEFORE all ``data`` lines (the
        conventional spot, immediately after ``type``).
      - N (1..len(data)): ``using`` comes between ``data[N-1]`` and
        ``data[N]`` in the source order.
    """
    name: str
    type: str | None = None
    using: str | None = None
    data: list[tuple[str, str]] = field(default_factory=list)
    using_index: int | None = None
    # Original leading-blank-and-comment lines, preserved on round-trip.
    # Typically empty; only set when the source file has comments above an entry.
    leading_comments: list[str] = field(default_factory=list)

    def data_value(self, key: str) -> str | None:
        """First data value for the given key, or None."""
        for k, v in self.data:
            if k == key:
                return v
        return None

    def all_data_values(self, key: str) -> list[str]:
        """All data values for the given key (in source order)."""
        return [v for k, v in self.data if k == key]

    def set_data(self, key: str, value: str) -> None:
        """Replace all existing data lines for ``key`` with a single one.

        If the key didn't exist, append it. Useful when remapping references.
        """
        new_data = [(k, v) for k, v in self.data if k != key]
        new_data.append((key, value))
        self.data = new_data


@dataclass
class StatsFile:
    """A parsed stats .txt file.

    ``line_ending`` is one of ``"\\r\\n"`` or ``"\\n"`` and is detected from
    the source so we can round-trip without changing it.
    """
    entries: list[StatsEntry] = field(default_factory=list)
    line_ending: str = "\r\n"  # default to CRLF since all observed files use it
    trailing_blank_lines: int = 0  # preserve exact trailing whitespace

    def by_name(self, name: str) -> StatsEntry | None:
        """First entry matching the given name, or None."""
        for e in self.entries:
            if e.name == name:
                return e
        return None

    def names(self) -> list[str]:
        """All entry names in source order."""
        return [e.name for e in self.entries]


class StatsParseError(ValueError):
    """Raised when a stats .txt file is structurally invalid."""


# --- Parsing --------------------------------------------------------------

def _detect_line_ending(text: str) -> str:
    """Detect whether the file uses CRLF or LF. CRLF wins on ambiguity."""
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def _parse_quoted_args(rest: str, expected: int) -> list[str]:
    """Parse `expected` quoted-string args from `rest` (the part after the keyword).

    Returns the unwrapped string contents. Raises StatsParseError if the
    expected number aren't present or aren't quoted.
    """
    matches = _QUOTED.findall(rest)
    if len(matches) != expected:
        raise StatsParseError(
            f"expected {expected} quoted args, got {len(matches)} in: {rest!r}"
        )
    return matches


def parse_text(text: str) -> StatsFile:
    """Parse stats .txt content from a string.

    Strict on entry structure (an entry must start with ``new entry``) but
    tolerant of comments, blank lines, and trailing whitespace.
    """
    line_ending = _detect_line_ending(text)
    # Normalize to LF for parsing; we'll write out with the original ending.
    lines = text.replace("\r\n", "\n").split("\n")

    entries: list[StatsEntry] = []
    current: StatsEntry | None = None
    pending_comments: list[str] = []

    for raw_line in lines:
        stripped = raw_line.strip()

        if not stripped:
            # Blank line: separates entries. The current entry is "closed"
            # but we don't push to `entries` yet (we already did so when it
            # was created). Just flush any accumulated comments downstream.
            if current is None:
                # blank line before any entry: preserve only if comments came with it
                pending_comments = []
            continue

        if stripped.startswith("//"):
            # Line comment: attach to the next entry, or trail the previous.
            pending_comments.append(raw_line)
            continue

        # Keyword line.
        if stripped.startswith("new entry"):
            rest = stripped[len("new entry"):]
            (name,) = _parse_quoted_args(rest, 1)
            current = StatsEntry(
                name=name,
                leading_comments=pending_comments,
            )
            entries.append(current)
            pending_comments = []
        elif stripped.startswith("type"):
            if current is None:
                raise StatsParseError("'type' line outside of any entry")
            (type_name,) = _parse_quoted_args(stripped[len("type"):], 1)
            current.type = type_name
        elif stripped.startswith("using"):
            if current is None:
                raise StatsParseError("'using' line outside of any entry")
            (parent,) = _parse_quoted_args(stripped[len("using"):], 1)
            current.using = parent
            # Record position so we can re-emit using at the same spot
            # if the source has it interleaved with data lines.
            current.using_index = len(current.data)
        elif stripped.startswith("data"):
            if current is None:
                raise StatsParseError("'data' line outside of any entry")
            key, value = _parse_quoted_args(stripped[len("data"):], 2)
            current.data.append((key, value))
        else:
            # Unknown keyword: be strict so we notice format changes early.
            raise StatsParseError(
                f"unrecognized line in stats .txt: {stripped!r}"
            )

    # Count trailing blank lines to round-trip exactly. Walk backwards.
    trailing = 0
    for raw_line in reversed(lines):
        if raw_line.strip() == "":
            trailing += 1
        else:
            break

    return StatsFile(
        entries=entries,
        line_ending=line_ending,
        trailing_blank_lines=trailing,
    )


def parse_file(path: Path | str) -> StatsFile:
    """Load and parse a stats .txt file from disk.

    Reads as bytes and decodes UTF-8; some files have a BOM, we strip it.
    """
    path = Path(path)
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return parse_text(data.decode("utf-8"))


# --- Writing --------------------------------------------------------------

def _quote(s: str) -> str:
    """Wrap a string in double quotes. We do NOT escape internal quotes:
    the format doesn't, and inserting them would corrupt the file. If the
    value contains a literal ``"``, raise: that's a bug in upstream data."""
    if '"' in s:
        raise ValueError(
            f"stats value contains an embedded double-quote, "
            f"which the format cannot represent: {s!r}"
        )
    return f'"{s}"'


def serialize_entry(entry: StatsEntry, line_ending: str = "\r\n") -> str:
    """Render a single entry to its stats-.txt form.

    Output shape (conventional)::

        new entry "Name"
        type "Type"
        using "Parent"           ← only if set
        data "Key" "Value"
        data "Key" "Value"

    If ``entry.using_index`` is set (recorded by the parser when the source
    interleaved ``using`` between data lines), we emit ``using`` at that
    position instead of right after ``type``.

    Leading comments (if any) precede the ``new entry`` line.
    """
    lines: list[str] = []
    lines.extend(entry.leading_comments)
    lines.append(f"new entry {_quote(entry.name)}")
    if entry.type is not None:
        lines.append(f"type {_quote(entry.type)}")

    # Choose where 'using' goes: at using_index (0 == before all data) or,
    # if no index recorded, at the conventional spot (before all data).
    using_pos = entry.using_index if entry.using_index is not None else 0

    if entry.using is not None and using_pos == 0:
        lines.append(f"using {_quote(entry.using)}")

    for i, (key, value) in enumerate(entry.data):
        lines.append(f"data {_quote(key)} {_quote(value)}")
        # Emit 'using' immediately after this data line if its index says so.
        if entry.using is not None and using_pos == i + 1:
            lines.append(f"using {_quote(entry.using)}")

    return line_ending.join(lines)


def serialize(stats: StatsFile) -> str:
    """Render the whole file. Entries are separated by a single blank line."""
    le = stats.line_ending
    # Each entry's lines joined by le; entries joined by le+le (blank line between).
    body = (le + le).join(serialize_entry(e, le) for e in stats.entries)

    # Original files end with one or two trailing line endings.
    # We preserve the original count for byte-stable round-trips.
    if stats.entries:
        body += le * max(stats.trailing_blank_lines, 1)
    return body


def write_file(stats: StatsFile, path: Path | str) -> None:
    """Write a stats file to disk."""
    path = Path(path)
    io_util.write_bytes_safe(path, serialize(stats).encode("utf-8"))


# --- Merging --------------------------------------------------------------

@dataclass
class StatsConflict:
    """Surfaced when two entries share a name but differ in content.

    The merger UI presents these to the user. The merger itself never
    silently picks a side: it raises this for the caller to resolve.
    """
    name: str
    a: StatsEntry
    b: StatsEntry


def diff_entries(a: StatsEntry, b: StatsEntry) -> list[str]:
    """Compare two entries field-by-field; return a list of human-readable
    diffs. Empty list = identical content.

    Used to decide whether a name collision is a real conflict (different
    bodies) or a harmless one (someone duplicated the same entry).
    """
    diffs: list[str] = []
    if a.type != b.type:
        diffs.append(f"type: {a.type!r} vs {b.type!r}")
    if a.using != b.using:
        diffs.append(f"using: {a.using!r} vs {b.using!r}")
    if a.data != b.data:
        # Itemize the actual key/value differences.
        a_set = list(a.data)
        b_set = list(b.data)
        only_a = [kv for kv in a_set if kv not in b_set]
        only_b = [kv for kv in b_set if kv not in a_set]
        for k, v in only_a:
            diffs.append(f"data only in A: {k!r}={v!r}")
        for k, v in only_b:
            diffs.append(f"data only in B: {k!r}={v!r}")
    return diffs


def merge(
    a: StatsFile,
    b: StatsFile,
    *,
    prefix_b_on_conflict: str | None = None,
) -> tuple[StatsFile, list[StatsConflict]]:
    """Merge two stats files by name.

    Strategy:
    - Entries unique to A → kept as-is.
    - Entries unique to B → appended after A's entries.
    - Names in both:
        * If content identical → keep A's copy (silent dedup).
        * If content differs:
          - If ``prefix_b_on_conflict`` is set, B's entry is renamed by
            prepending the prefix (e.g. ``"MyMod_"``) and included. The
            original conflict is recorded so the user can review.
          - If ``prefix_b_on_conflict`` is None, B's entry is omitted and
            a StatsConflict is returned for the user to resolve.

    The line_ending is taken from ``a``; B's entries get rewritten with A's
    convention so the output file is consistent.

    Returns (merged_file, conflicts).
    """
    out = StatsFile(line_ending=a.line_ending,
                    trailing_blank_lines=a.trailing_blank_lines)
    conflicts: list[StatsConflict] = []

    a_by_name = {e.name: e for e in a.entries}
    b_by_name = {e.name: e for e in b.entries}

    # First: all of A's entries in original order.
    for entry in a.entries:
        out.entries.append(entry)

    # Then: B's entries, with conflict handling.
    for entry in b.entries:
        if entry.name not in a_by_name:
            out.entries.append(entry)
            continue

        a_entry = a_by_name[entry.name]
        if not diff_entries(a_entry, entry):
            # Identical: silent dedup. (We keep A's copy already in out.)
            continue

        # Real conflict.
        if prefix_b_on_conflict is not None:
            renamed = StatsEntry(
                name=f"{prefix_b_on_conflict}{entry.name}",
                type=entry.type,
                using=entry.using,
                data=list(entry.data),
                using_index=entry.using_index,
                leading_comments=list(entry.leading_comments),
            )
            out.entries.append(renamed)

        conflicts.append(StatsConflict(name=entry.name, a=a_entry, b=entry))

    return out, conflicts
