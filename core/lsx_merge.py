"""Generic LSX union merge.

Many BG3 LSF metadata files have the same shape: a single ``<region>``
with one root ``<node>`` whose ``<children>`` are a list of registrations
(UI widgets, root templates, content list entries, etc.). Merging two of
these means unioning those child lists — keeping every entry from both
sides, deduping by identity, and treating real conflicts (same identity
but different content) per the user's conflict policy.

This module is format-agnostic: it operates on the parsed
:class:`core.lsx.LsxDocument` tree without knowing whether the source
file was ``GUI/metadata.lsf``, ``RootTemplates/_merged.lsf``, or
anything else. The caller is responsible for the LSF↔LSX conversion
via ``divine.exe`` if the on-disk format is binary.

Identity resolution:
    1. If a child node has a ``UUID`` attribute, that's its identity.
    2. Otherwise, the full serialized form of the node (id + attribute
       set + children) is its identity — so byte-identical entries
       silently dedupe, and any other difference surfaces as a conflict.

Conflict policy:
    "a_wins" (default) — keep A's version, record a conflict.
    "b_wins"           — overwrite with B's version, record a conflict.
    "fail"             — raise on the first content-differing collision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from . import lsx as _lsx


ConflictPolicy = Literal["a_wins", "b_wins", "fail"]


@dataclass
class UnionConflict:
    """One identity-level collision: same UUID (or same byte-identical
    body) in both A and B with different content."""
    region_id: str
    node_id: str
    identity: str  # UUID, or a short fingerprint for UUID-less nodes
    resolution: str  # "kept_a" | "kept_b"


@dataclass
class UnionResult:
    """Outcome of unioning two LsxDocuments."""
    document: _lsx.LsxDocument
    conflicts: list[UnionConflict] = field(default_factory=list)
    added_from_b: int = 0    # how many B-only entries we appended
    deduped: int = 0          # how many byte-identical entries we collapsed


class UnionError(ValueError):
    """Raised by union_documents when the structure isn't union-able
    (e.g. regions with different IDs in one of the two inputs but no
    matching counterpart in the other), or when policy=fail trips on a
    content-differing collision."""


def union_documents(
    a: _lsx.LsxDocument,
    b: _lsx.LsxDocument,
    *,
    conflict_policy: ConflictPolicy = "a_wins",
) -> UnionResult:
    """Union two parsed LSX documents.

    Returns a new :class:`UnionResult` whose ``document`` is the merged
    output. Inputs are not mutated. The output's version block is
    inherited from ``a`` (assumption: both docs were converted from
    files by the same divine.exe so versions are compatible).

    Algorithm:

    - Index every region in A by ``region.id``.
    - For each region in B, look for a matching region in A. If none,
      append the B-only region whole. If found, union the root node's
      ``children`` lists.
    - Within a shared region: A's children come first, in their
      original order. Then for each B child, look up its identity:
        - not seen yet → append (counts as ``added_from_b``)
        - seen, body byte-identical → silently skip (``deduped``)
        - seen, body differs → conflict policy decides

    Stable, deterministic: same inputs → same output.
    """
    # Deep-copy A's regions so we can mutate the merged document freely
    # without leaking changes back to the caller's input objects.
    merged_regions: list[_lsx.Region] = [_copy_region(r) for r in a.regions]
    by_id: dict[str, _lsx.Region] = {r.id: r for r in merged_regions}

    conflicts: list[UnionConflict] = []
    added_from_b = 0
    deduped = 0

    for b_region in b.regions:
        existing = by_id.get(b_region.id)
        if existing is None:
            # B introduces a region A doesn't have. Append whole.
            new_region = _copy_region(b_region)
            merged_regions.append(new_region)
            by_id[new_region.id] = new_region
            # Count every leaf child as an addition for the metric.
            added_from_b += len(new_region.root_node.children)
            continue

        # Both sides have this region. Union the root node's children.
        a_root = existing.root_node
        b_root = b_region.root_node
        if a_root.id != b_root.id:
            raise UnionError(
                f"region {b_region.id!r} has different root node ids in A "
                f"({a_root.id!r}) vs B ({b_root.id!r}); refusing to union"
            )

        # Build an index of A's existing children by identity.
        a_index: dict[str, _lsx.Node] = {}
        for child in a_root.children:
            ident = _node_identity(child)
            # If A itself has duplicates, the first wins for indexing —
            # we don't try to clean up A's pre-existing dupes.
            a_index.setdefault(ident, child)

        for b_child in b_root.children:
            ident = _node_identity(b_child)
            a_match = a_index.get(ident)
            if a_match is None:
                # B-only entry — append (deep-copied so future edits don't
                # alias back into b's input tree).
                a_root.children.append(_copy_node(b_child))
                added_from_b += 1
                continue

            if _nodes_equal(a_match, b_child):
                # Byte-identical entry from both sides — silent dedup.
                deduped += 1
                continue

            # Same identity, different content → real collision.
            if conflict_policy == "fail":
                raise UnionError(
                    f"content-differing collision in region {b_region.id!r}, "
                    f"node {b_child.id!r}, identity {ident!r}"
                )
            if conflict_policy == "b_wins":
                # Replace A's entry with B's. Keep position stable so
                # downstream serialization is deterministic.
                idx = a_root.children.index(a_match)
                a_root.children[idx] = _copy_node(b_child)
                conflicts.append(UnionConflict(
                    region_id=b_region.id, node_id=b_child.id,
                    identity=ident, resolution="kept_b",
                ))
            else:
                # "a_wins" — leave A's entry. Just record the conflict.
                conflicts.append(UnionConflict(
                    region_id=b_region.id, node_id=b_child.id,
                    identity=ident, resolution="kept_a",
                ))

    merged_doc = _lsx.LsxDocument(
        version=a.version,
        regions=merged_regions,
        had_bom=a.had_bom,
    )
    return UnionResult(
        document=merged_doc,
        conflicts=conflicts,
        added_from_b=added_from_b,
        deduped=deduped,
    )


# --- Identity & equality ----------------------------------------------------


def _node_identity(node: _lsx.Node) -> str:
    """Stable identity string for a child node.

    UUID wins when present (the BG3 convention for any registered
    entity). Otherwise we fall back to a hash of the node's full
    serialized form so byte-identical entries collide and any other
    difference is treated as a unique node (i.e. always appended).
    """
    uuid_attr = node.attr("UUID")
    if uuid_attr is not None and uuid_attr.value:
        return f"UUID:{uuid_attr.value}"
    # MapKey is the convention some LSX node types use instead of UUID
    # (e.g. some content list entries). Try it next.
    mk = node.attr("MapKey")
    if mk is not None and mk.value:
        return f"MapKey:{mk.value}"
    # Fall back to a fingerprint of the entire node's attribute set.
    # That way duplicate UUID-less nodes with identical bodies dedupe
    # silently, while differing-content UUID-less nodes are treated as
    # distinct (each appended).
    return f"fingerprint:{_fingerprint(node)}"


def _fingerprint(node: _lsx.Node) -> str:
    """Order-sensitive fingerprint of a node's content. Used only as a
    fallback identity when no UUID/MapKey is present. Doesn't need to
    be cryptographically strong — just deterministic and order-aware."""
    parts: list[str] = [f"id={node.id}"]
    for attr in node.attributes:
        parts.append(f"{attr.id}|{attr.type}|{attr.value or ''}|"
                     f"{attr.handle or ''}|{attr.version or ''}")
    for child in node.children:
        parts.append(f"child[{_fingerprint(child)}]")
    return "/".join(parts)


def _nodes_equal(a: _lsx.Node, b: _lsx.Node) -> bool:
    """Deep equality. The fingerprint is order-aware so this catches
    attribute-reorder cases as 'different'; in practice divine.exe is
    deterministic about attribute ordering so that's not an issue."""
    return _fingerprint(a) == _fingerprint(b)


# --- Deep copies (so the merge doesn't alias caller inputs) -----------------


def _copy_attribute(a: _lsx.Attribute) -> _lsx.Attribute:
    return _lsx.Attribute(
        id=a.id, type=a.type, value=a.value, handle=a.handle, version=a.version,
    )


def _copy_node(node: _lsx.Node) -> _lsx.Node:
    return _lsx.Node(
        id=node.id,
        attributes=[_copy_attribute(a) for a in node.attributes],
        children=[_copy_node(c) for c in node.children],
    )


def _copy_region(region: _lsx.Region) -> _lsx.Region:
    return _lsx.Region(
        id=region.id,
        root_node=_copy_node(region.root_node),
    )
