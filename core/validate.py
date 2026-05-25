"""Post-merge validation.

After ``core.merger`` writes the merged project, this module re-reads it
and produces a structured report of any issues:

- **Orphan references** — identifiers referenced but never defined.
  Most are benign (base-game refs, refs to other-mod content) but they're
  worth surfacing so the user can spot a truly broken ref.
- **Definition collisions** — same identifier defined more than once in
  the *output*. This should never happen — if it does, the merger has a
  bug. We surface it loudly.
- **Missing dependencies** — UUIDs declared in meta.lsx Dependencies that
  aren't referenced anywhere in content. Not an error (deps can be
  forward-declared for runtime), just an observation.

The validator never modifies anything — it's pure inspection. The merger
calls it after every successful merge and attaches the report to
``MergeResult``.

We **warn and continue** in line with the design decision: validation
never blocks. The user sees the report and decides whether to ship.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import references
from .project import Project
from .references import IdentifierEntry, IdKind, ReferenceIndex


@dataclass
class ValidationReport:
    """Structured findings about a merged project.

    Each section is a list (possibly empty). The GUI renders them as
    expandable groups in a validation panel; CLI prints them grouped.
    """
    orphan_references: dict[str, list[IdentifierEntry]] = field(
        default_factory=dict
    )
    definition_collisions: dict[str, list[IdentifierEntry]] = field(
        default_factory=dict
    )
    unreferenced_dependencies: list[str] = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)

    def is_clean(self) -> bool:
        """True if no issues at all. The user can ship without review."""
        return (
            not self.orphan_references
            and not self.definition_collisions
            and not self.unreferenced_dependencies
        )

    def is_blocked(self) -> bool:
        """True only when something the merger should never have produced
        is present (definition collisions = merger bug). Validation does
        NOT block on orphans or unreferenced deps; those are warnings."""
        return bool(self.definition_collisions)

    def render(self) -> str:
        """Human-readable rendering for CLI display."""
        lines: list[str] = []
        lines.extend(self.summary_lines)
        if not (self.orphan_references or self.definition_collisions
                or self.unreferenced_dependencies):
            lines.append("  Validation: no issues found.")
            return "\n".join(lines)

        if self.definition_collisions:
            lines.append("")
            lines.append("Definition collisions in merged output "
                         "(this should not happen; please report):")
            for kind, entries in sorted(self.definition_collisions.items()):
                lines.append(f"  {kind}:")
                for entry in entries:
                    lines.append(
                        f"    {entry.value!r} defined "
                        f"{len(entry.definitions)} times"
                    )
                    for loc in entry.definitions:
                        lines.append(f"        - {loc.file}: {loc.hint}")

        if self.orphan_references:
            lines.append("")
            lines.append("Orphan references (referenced but not defined in this mod):")
            lines.append("  These are usually base-game or other-mod references.")
            for kind, entries in sorted(self.orphan_references.items()):
                lines.append(f"  {kind}: {len(entries)} orphan(s)")
                for entry in entries[:5]:  # truncate for readability
                    lines.append(f"    - {entry.value!r}")
                if len(entries) > 5:
                    lines.append(f"    ... and {len(entries) - 5} more")

        if self.unreferenced_dependencies:
            lines.append("")
            lines.append("Dependencies declared but not referenced in content:")
            for u in self.unreferenced_dependencies:
                lines.append(f"  - {u}")

        return "\n".join(lines)


def _is_real_collision(kind: IdKind, entry: IdentifierEntry) -> bool:
    """Decide whether multiple definitions of the same identifier represent
    a genuine bug or a legitimate Toolkit duality.

    Specifically: a STAT_NAME can legitimately appear in BOTH the canonical
    Toolkit source (``Editor/Mods/.../Stats/<Type>/<file>.stats``) and the
    generated packed form (``Public/.../Stats/Generated/Data/<Type>.txt``)
    for the same logical stat. The Toolkit emits both forms in parallel.
    Counting that as a collision would spam every validation report.

    Real collisions: same identifier defined twice in files of the *same*
    representation (two .txt files, two .stats files, etc.).
    """
    if kind != IdKind.STAT_NAME:
        return True

    # Look at the source paths: how many are .stats files, how many are .txt?
    paths = [str(loc.file).replace("\\", "/") for loc in entry.definitions]
    stats_count = sum(1 for p in paths if p.endswith(".stats"))
    txt_count = sum(1 for p in paths if p.endswith(".txt"))

    # Legit Toolkit parallel form: exactly one .stats and one .txt.
    if len(entry.definitions) == 2 and stats_count == 1 and txt_count == 1:
        return False
    # Otherwise: if any single format has more than one definition, it's a
    # real collision.
    return stats_count > 1 or txt_count > 1 or (stats_count + txt_count) > 2


def validate(project: Project) -> ValidationReport:
    """Run the validation pass against a merged project and return the
    report. Builds its own ReferenceIndex internally so the caller doesn't
    have to."""
    report = ValidationReport()
    index = ReferenceIndex.build(project)

    report.summary_lines.append(
        f"Validation of {project.mod_meta.name!r} ({project.mod_meta.uuid}):"
    )
    report.summary_lines.append(f"  {len(project.files)} files cataloged")

    # 1. Definition collisions per kind.
    for kind in IdKind:
        colliders: list[IdentifierEntry] = []
        for entry in index.entries_by_kind(kind):
            if len(entry.definitions) > 1 and _is_real_collision(kind, entry):
                colliders.append(entry)
        if colliders:
            report.definition_collisions[kind.value] = colliders

    # 2. Orphan references per kind.
    for kind in IdKind:
        orphans = index.orphan_references(kind)
        if orphans:
            report.orphan_references[kind.value] = orphans

    # 3. Unreferenced dependencies.
    referenced_uuids = index.referenced_values(IdKind.UUID)
    for dep in project.mod_meta.dependencies:
        if dep.uuid.lower() not in referenced_uuids:
            report.unreferenced_dependencies.append(
                f"{dep.name!r} ({dep.uuid}) — not referenced anywhere in content"
            )

    return report
