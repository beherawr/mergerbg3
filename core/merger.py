"""The merge orchestrator.

Glues every parser, the reference index, and the remap tables into a single
end-to-end pipeline. Given a list of input ``Project``s plus a new identity,
the merger produces a complete Toolkit project directory at the requested
output path.

The pipeline is five phases:

1. **Discover**: caller-supplied. The merger receives already-loaded
   ``Project`` objects (so the GUI can show a preview before invoking).
2. **Detect**: build a ``ReferenceIndex`` for each input; find clashes.
3. **Plan**: translate clashes + user policy into ``RemapSet``s, one per
   input. Empty remap sets are the common case (clean union).
4. **Execute**: for each file in each input, decide its destination path
   in the output tree and either:
     - For *mergeable text formats* (stats .txt, .stats, .loca.xml,
       meta.lsx, story goals): parse, apply remap, then either merge with
       its peer from the other input or write standalone.
     - For *opaque files* (.lsf, .lsfx, .gr2, .tif, .dds, .lsx other than
       meta): copy verbatim with the folder name remapped.
     - For *discardable files* (story compiled outputs, log.txt): skip.
5. **Validate**: build a ``ReferenceIndex`` over the output and surface
   orphan references for user review. Doesn't block: warnings only.

The merger is deliberately single-threaded and writes files eagerly. For
the project sizes we target (dozens to low-hundreds of files per input),
this is fast enough and keeps the error story simple.

The merger never overwrites the inputs. The output directory is always
distinct. (In-place merging: selecting one input as the output target:
is implemented by the caller copying that input to the output dir first.)

Things the merger does NOT do (yet):
- Run divine.exe to merge LSF binary content. The clean-union case
  (banks/root templates per-mod, no overlap) works without it. Binary
  merging requires content-aware handling we'll add when we hit a real
  fixture that needs it.
- Re-compile Osiris goals. We copy source goals; the user re-opens the
  output in the Toolkit to compile.
- Repack to .pak. That's a separate "export" step.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from . import (
    divine as _divine, lsx, lsx_merge, localization, meta as _meta,
    remap, references, stats_text, stats_xml, treasure_table,
)
from .project import CatalogedFile, FileCategory, Project
from .references import IdKind, IdentifierClash, ReferenceIndex, find_clashes


# --- Windows long-path support ---------------------------------------------
#
# Windows defaults to a 260-character path limit (MAX_PATH) for many file
# APIs. The merger's destination paths can blow through this quickly:
# workspace prefix (~50 chars) + staging suffix + deep mod tree
# (Editor/Mods/<long folder>/Public/SharedDev/Assets/...) easily exceeds
# the limit on real user setups. We delegate the path-mangling and write
# helpers to ``core.io_util`` so this logic lives in one place.


def _copy_long(src: Path, dst: Path) -> None:
    """``shutil.copy2`` that survives long paths on Windows.

    Internally, ``shutil.copy2`` reads/writes through CopyFile2 which is
    subject to MAX_PATH unless paths use the ``\\\\?\\`` prefix. This
    helper applies the prefix on Windows so the merger doesn't fail
    halfway through emitting files for mods with deeply-nested assets.
    """
    from . import io_util
    shutil.copy2(io_util.to_long_path(src), io_util.to_long_path(dst))


def _mkdir_long(d: Path) -> None:
    """``Path.mkdir(parents=True, exist_ok=True)`` that survives long
    paths on Windows. For paths approaching MAX_PATH we walk up the
    tree creating each missing ancestor with the ``\\\\?\\``-prefixed
    name so CreateDirectoryW doesn't reject the call."""
    if sys.platform == "win32" and len(str(d)) > 240:
        import os
        from . import io_util
        parts: list[Path] = []
        cur = d
        while not cur.exists():
            parts.append(cur)
            if cur.parent == cur:
                break
            cur = cur.parent
        for p in reversed(parts):
            try:
                os.mkdir(io_util.to_long_path(p))
            except FileExistsError:
                pass
    else:
        d.mkdir(parents=True, exist_ok=True)


# --- Configuration -------------------------------------------------------


ConflictPolicy = Literal["prefix", "skip", "fail"]


@dataclass
class MergeConfig:
    """Inputs and parameters for one merge operation.

    ``inputs`` must contain at least two Projects. For three or more, the
    merge is left-fold: first merge inputs[0] + inputs[1], then add
    inputs[2] to that, and so on. Each step uses the same conflict policy.

    ``conflict_policy``:
      - ``"prefix"``: on a stat-name clash, mod B's entry is renamed with
        ``conflict_prefix`` (e.g. ``"ModB_"``) and references in mod B's
        content follow.
      - ``"skip"``: mod B's entry is dropped, mod A's wins. Conflict
        recorded for the user's report.
      - ``"fail"``: any clash raises and the merge aborts before writing.

    ``output_dir`` must not exist OR must be empty. The merger refuses to
    overwrite a non-empty directory by default.
    """
    inputs: list[Project]
    output_dir: Path
    new_uuid: str
    new_folder: str
    new_name: str
    new_author: str = ""
    new_description: str = ""
    conflict_policy: ConflictPolicy = "skip"
    conflict_prefix: str = ""  # required when policy == "prefix"
    allow_existing_output: bool = False
    # In-place mode: the merge OVERWRITES ``output_dir`` (which should be
    # one of the input projects' roots). Used by the GUI's "Combine B
    # into A" mode where mod A keeps its identity and gets B's content
    # folded in.
    #
    # Safety invariant: when ``in_place=True``, the merger writes to a
    # temp sibling directory first, then atomically swaps it in for the
    # target. If the merge crashes at any point, the original directory
    # is recoverable (it ends up either intact or renamed to a .backup
    # sibling that the user can rename back manually).
    #
    # When ``in_place=True``:
    #   - ``output_dir`` should equal one of ``inputs[i].project_root``
    #   - ``new_folder`` / ``new_uuid`` / ``new_name`` should equal that
    #     input's existing identity (otherwise the in-place "mod A" gets
    #     a different identity, defeating the purpose)
    # The GUI enforces both; passing inconsistent values isn't an error
    # at the engine level: we just write what we're told.
    in_place: bool = False
    # Optional bound Divine wrapper for LSF↔LSX round-tripping. When set,
    # the merger will structurally merge binary LSF files (currently:
    # GUI/metadata.lsf) instead of keeping just one side. When None, the
    # merger falls back to the "keep A, surface conflict" behavior.
    divine: "_divine.Divine | None" = None
    # Optional progress reporter for the UI. Called as
    # ``progress_callback(phase: str, current: int, total: int, detail: str)``
    # where phase is one of "detect", "plan", "emit", "validate" and
    # current/total form a 0..total progress fraction. ``detail`` is a
    # short human-readable string (the file being written, etc.).
    # The merger is internally single-threaded; the callback is invoked
    # on the caller's thread. The GUI runs the merge on a worker thread
    # and marshals updates back via Qt signals.
    progress_callback: "Callable[[str, int, int, str], None] | None" = None


# --- Result types --------------------------------------------------------


@dataclass
class MergeConflict:
    """A clash and what the merger did about it.

    Surfaced to the caller (GUI) so the user can review every decision
    the engine made on their behalf.
    """
    kind: str
    identifier: str
    where_a: str  # short human-readable location in input A
    where_b: str
    resolution: str  # "skipped" | "prefixed_<new_name>" | "kept_higher_version"


@dataclass
class FileEmission:
    """One file written to the output."""
    output_path: Path
    source: str  # "copied_from:<old>" | "merged" | "regenerated"
    note: str = ""


@dataclass
class MergeResult:
    output_dir: Path
    new_project: Project | None  # the loaded output (None on dry-run)
    emissions: list[FileEmission] = field(default_factory=list)
    conflicts: list[MergeConflict] = field(default_factory=list)
    skipped_files: list[Path] = field(default_factory=list)  # discarded by design
    orphan_warnings: dict[str, list[str]] = field(default_factory=dict)


class MergeError(RuntimeError):
    """Raised when the merge cannot proceed (e.g. policy=fail + clash found)."""


# --- The orchestrator ----------------------------------------------------


def merge(config: MergeConfig) -> MergeResult:
    """Run all five phases and return the result.

    The caller catches MergeError separately from other exceptions:
    a MergeError is a policy decision (e.g. unresolved conflict), not a
    parsing or filesystem failure.

    If ``config.in_place`` is set, the merge writes to a sibling temp
    directory first and atomically swaps it into ``config.output_dir``
    on success. The original directory at ``output_dir`` is replaced
    only after a clean merge. On any failure the original is intact.
    """
    if len(config.inputs) < 2:
        raise MergeError("need at least two input projects to merge")
    if config.conflict_policy == "prefix" and not config.conflict_prefix:
        raise MergeError("conflict_policy='prefix' requires a non-empty conflict_prefix")

    if config.in_place:
        return _merge_in_place(config)
    return _merge_direct(config)


def _merge_direct(config: MergeConfig) -> MergeResult:
    """The standard 'write to a fresh output directory' path."""
    _prepare_output_dir(config)
    if len(config.inputs) == 2:
        return _merge_pair(config.inputs[0], config.inputs[1], config)
    raise MergeError(
        "merging more than two inputs at once is not yet implemented; "
        "loop the caller over pairs instead"
    )


def _merge_in_place(config: MergeConfig) -> MergeResult:
    """Write the merge to a staging directory, then atomically swap the
    four mod-specific bucket subfolders into place.

    This works the same way for self-contained projects and canonical
    Toolkit workspaces. In both cases, a "mod" consists of up to four
    subfolders: one per bucket: and replacing the mod means replacing
    those subfolders. Other content in the output_dir (other mods in a
    canonical workspace, README files in a self-contained project) is
    left untouched.

    Sequence:
        write merge → staging/

        for each bucket b that staging wrote to:
            if target/b/<X>/ exists:
                rename target/b/<X>/ → target/b/<X>.backup_<stamp>/
        for each bucket:
            rename staging/b/<X>/ → target/b/<X>/
        cleanup: rmtree each .backup_<stamp> directory

    Crash recovery: on any OSError during the swap we undo the renames
    we already did, so the user's mod is restored to its original state.
    Worst case (a rename fails AND undo also fails) leaves the user with
    .backup_<stamp> subfolders alongside the real bucket subfolders:
    visible and recoverable manually.
    """
    import os
    import time
    from dataclasses import replace as _replace

    target_root = Path(config.output_dir).resolve()
    if not target_root.exists():
        raise MergeError(
            f"in_place=True but target directory {target_root} does not exist"
        )
    if not target_root.is_dir():
        raise MergeError(
            f"in_place=True but target {target_root} is not a directory"
        )

    # Short random staging suffix so we don't bust Windows' MAX_PATH
    # on deeply-nested files. 8 hex chars (32 bits) is plenty unique
    # for the brief lifetime of a staging directory.
    import secrets
    stamp = secrets.token_hex(4)
    # Staging goes as a sibling of the target root: same filesystem so
    # the per-bucket renames are atomic, but OUTSIDE the workspace so we
    # don't pollute it with temp content visible to the Toolkit.
    # Name is kept short ("$name.s_<8hex>") since Windows' CopyFile2
    # hits MAX_PATH=260 quickly on deep mod trees.
    staging = target_root.parent / f"{target_root.name}.s_{stamp}"
    if staging.exists():
        raise MergeError(f"staging directory already exists: {staging}")

    # Run the standard merge into staging. Use a copy of the config so
    # we don't mutate the caller's object.
    inner_config = _replace(
        config,
        output_dir=staging,
        in_place=False,                # already handled here
        allow_existing_output=False,   # staging must be fresh
    )
    try:
        result = _merge_direct(inner_config)
    except Exception:
        # Half-written staging: clean up. Target is intact.
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # Identify which bucket subfolders the merge actually wrote.
    bucket_subpaths = ("Editor/Mods", "Mods", "Public", "Projects")
    active: list[tuple[Path, Path, Path]] = []  # (staging, target, backup)
    for bucket in bucket_subpaths:
        sp = staging / bucket / config.new_folder
        if sp.is_dir():
            tp = target_root / bucket / config.new_folder
            bp = tp.with_name(tp.name + f".bak_{stamp}")
            active.append((sp, tp, bp))

    completed_backups: list[tuple[Path, Path]] = []  # (target, backup)
    completed_swaps: list[tuple[Path, Path]] = []    # (staging, target)
    try:
        # Phase A: move existing targets aside (those that exist; in
        # the "make new mod" path none will exist, but in-place implies
        # at least mod A's content is already there).
        for sp, tp, bp in active:
            if tp.exists():
                os.rename(tp, bp)
                completed_backups.append((tp, bp))

        # Phase B: move staging content into place. Create parent
        # directories as needed (the workspace may not yet have an
        # Editor/Mods/ tree if no mod with editor files has been
        # made yet).
        for sp, tp, bp in active:
            _mkdir_long(tp.parent)
            os.rename(sp, tp)
            completed_swaps.append((sp, tp))
    except OSError as e:
        # Best-effort recovery: undo what we did so the original mod
        # is restored to its pre-swap state.
        for sp, tp in reversed(completed_swaps):
            try:
                if tp.exists() and not sp.exists():
                    os.rename(tp, sp)
            except OSError:
                pass
        for tp, bp in reversed(completed_backups):
            try:
                if not tp.exists() and bp.exists():
                    os.rename(bp, tp)
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise MergeError(
            f"in-place merge swap failed mid-way ({e}); attempted to "
            f"restore the original state. Check {target_root} and look "
            f"for any *.bak_{stamp} or *.s_* leftovers."
        ) from e

    # All swaps OK: clean up backups and any leftover staging files
    # outside the four bucket subfolders (e.g. an empty staging shell).
    for _, bp in completed_backups:
        shutil.rmtree(bp, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)

    # Re-load the merged mod from its REAL location. The output_dir is
    # likely a canonical workspace with many mods, so we MUST pass
    # mod_folder_name (auto-detect would refuse on multiple meta.lsx).
    result.output_dir = target_root
    result.new_project = Project.load(
        target_root, mod_folder_name=config.new_folder
    )
    return result


def _prepare_output_dir(config: MergeConfig) -> None:
    """Ensure ``config.output_dir`` is ready to receive the merged mod.

    The merger writes to ``<output_dir>/<bucket>/<new_folder>/...`` for
    each of the four buckets. This function checks that none of those
    bucket subfolders already exist: which would mean we'd silently
    overwrite an existing mod with the same folder name. Other content
    in ``output_dir`` (e.g. other mods in a canonical Toolkit workspace)
    is left alone.

    Pass ``allow_existing_output=True`` to skip the collision check
    (used by in-place merge, which intentionally overwrites mod A).
    """
    od = Path(config.output_dir)
    if od.exists():
        if not od.is_dir():
            raise MergeError(f"output path {od} exists and isn't a directory")
        if not config.allow_existing_output:
            collisions = []
            for bucket in ("Editor/Mods", "Mods", "Public", "Projects"):
                target = od / bucket / config.new_folder
                if target.exists():
                    collisions.append(str(target))
            if collisions:
                raise MergeError(
                    f"the new mod folder {config.new_folder!r} already "
                    f"exists at: {'; '.join(collisions)}. Pick a different "
                    f"folder name or set allow_existing_output=True."
                )
    else:
        od.mkdir(parents=True)


# --- Pairwise merge ------------------------------------------------------


def _merge_pair(a: Project, b: Project, config: MergeConfig) -> MergeResult:
    result = MergeResult(output_dir=config.output_dir, new_project=None)
    report = config.progress_callback or (lambda *_, **__: None)

    # Phase 2: Detect.
    report("detect", 0, 3, "Building reference index for input A")
    index_a = ReferenceIndex.build(a)
    report("detect", 1, 3, "Building reference index for input B")
    index_b = ReferenceIndex.build(b)
    report("detect", 2, 3, "Cross-comparing identifiers")
    clashes = find_clashes(index_a, index_b)
    report("detect", 3, 3, f"Found {len(clashes)} identifier clashes")

    # Phase 3: Plan.
    report("plan", 0, 1, "Building remap tables from clashes + folder rename")
    remap_a, remap_b, conflict_records = _plan_remaps(
        a, b, clashes, config,
    )
    result.conflicts = conflict_records
    report("plan", 1, 1, f"{len(conflict_records)} conflicts to handle")

    if config.conflict_policy == "fail" and any(
        c.resolution == "would_fail" for c in conflict_records
    ):
        names = ", ".join(c.identifier for c in conflict_records
                          if c.resolution == "would_fail")
        raise MergeError(
            f"clash policy=fail; aborting with unresolved clashes: {names}"
        )

    # Phase 4: Execute.
    report("emit", 0, 0, "Writing meta files")
    _emit_meta(a, b, config, result)
    _emit_files(a, b, config, remap_a, remap_b, result)

    # Phase 5: Validate.
    report("validate", 0, 2, "Re-loading the merged project")
    # Pass mod_folder_name explicitly: the output_dir may be a shared
    # canonical workspace with many mods, in which case auto-detection
    # would refuse with "multiple mods found".
    output_project = Project.load(
        config.output_dir, mod_folder_name=config.new_folder
    )
    result.new_project = output_project
    report("validate", 1, 2, "Re-indexing for orphan-reference check")
    output_index = ReferenceIndex.build(output_project)
    for kind in IdKind:
        orphans = [e.value for e in output_index.orphan_references(kind)]
        if orphans:
            result.orphan_warnings[kind.value] = orphans
    report("validate", 2, 2, "Done")

    return result


# --- Planning phase ------------------------------------------------------


def _plan_remaps(
    a: Project,
    b: Project,
    clashes: list[IdentifierClash],
    config: MergeConfig,
) -> tuple[remap.RemapSet, remap.RemapSet, list[MergeConflict]]:
    """Build per-input RemapSets and the per-conflict record list.

    Mod A is canonical; only mod B's content gets rewritten. We never
    remap mod A's content.

    Folder rename ALWAYS happens (both inputs' folder names -> new folder).
    The path-substring remap covers any in-file path strings; the
    file-emission code handles actual on-disk paths.
    """
    remap_a = remap.RemapSet()
    remap_b = remap.RemapSet()
    records: list[MergeConflict] = []

    # Folder rename for both inputs.
    new_folder = config.new_folder
    if a.mod_folder_name != new_folder:
        # Cover the common path forms a Larian file uses:
        for prefix in ("Public/", "Mods/", "Editor/Mods/", "Projects/"):
            remap_a.paths.add_substring(
                f"{prefix}{a.mod_folder_name}",
                f"{prefix}{new_folder}",
            )
    if b.mod_folder_name != new_folder:
        for prefix in ("Public/", "Mods/", "Editor/Mods/", "Projects/"):
            remap_b.paths.add_substring(
                f"{prefix}{b.mod_folder_name}",
                f"{prefix}{new_folder}",
            )

    # The merged mod's own UUID replaces both inputs' mod UUIDs in any
    # content that references them. Resource/template UUIDs stay.
    if a.mod_meta.uuid != config.new_uuid:
        remap_a.uuids.add(a.mod_meta.uuid.lower(), config.new_uuid.lower())
    if b.mod_meta.uuid != config.new_uuid:
        remap_b.uuids.add(b.mod_meta.uuid.lower(), config.new_uuid.lower())

    # Now handle clashes per config.conflict_policy.
    for clash in clashes:
        location_a = "; ".join(loc.hint for loc in clash.a_locations[:1])
        location_b = "; ".join(loc.hint for loc in clash.b_locations[:1])

        if clash.kind == IdKind.UUID and clash.value in (
            a.mod_meta.uuid.lower(), b.mod_meta.uuid.lower(),
        ):
            # The mod-UUID itself colliding (extremely unlikely: would
            # mean both inputs share the same mod). Already handled by
            # the uuid remaps above.
            continue

        if config.conflict_policy == "skip":
            records.append(MergeConflict(
                kind=clash.kind.value,
                identifier=clash.value,
                where_a=location_a,
                where_b=location_b,
                resolution="skipped",
            ))
        elif config.conflict_policy == "fail":
            records.append(MergeConflict(
                kind=clash.kind.value,
                identifier=clash.value,
                where_a=location_a,
                where_b=location_b,
                resolution="would_fail",
            ))
        elif config.conflict_policy == "prefix":
            new_value = _prefixed(clash.value, clash.kind, config.conflict_prefix)
            _add_to_remap(remap_b, clash.kind, clash.value, new_value)
            records.append(MergeConflict(
                kind=clash.kind.value,
                identifier=clash.value,
                where_a=location_a,
                where_b=location_b,
                resolution=f"prefixed_{new_value}",
            ))
        else:
            raise MergeError(f"unknown conflict policy: {config.conflict_policy!r}")

    return remap_a, remap_b, records


def _prefixed(value: str, kind: IdKind, prefix: str) -> str:
    """Produce a prefixed form of an identifier appropriate to its kind."""
    if kind == IdKind.STAT_NAME or kind == IdKind.ICON_NAME:
        return f"{prefix}{value}"
    if kind == IdKind.UUID:
        # We don't prefix UUIDs: they're hex-formatted and prefixing would
        # break parsers. Realistically a UUID clash means we should mint
        # a fresh one. For now leave it and let validation flag it.
        return value
    if kind == IdKind.LOCA_HANDLE:
        # Handles are hex too: same as UUIDs, can't prefix. The proper
        # fix is to mint a new handle, which we leave for later.
        return value
    if kind == IdKind.PATH_STRING:
        return value
    return value


def _add_to_remap(rset: remap.RemapSet, kind: IdKind, before: str, after: str) -> None:
    """Route an entry into the right per-kind sub-table."""
    if kind == IdKind.STAT_NAME:
        rset.stats.add(before, after)
    elif kind == IdKind.UUID:
        rset.uuids.add(before, after)
    elif kind == IdKind.LOCA_HANDLE:
        rset.handles.add(before, after)
    elif kind == IdKind.ICON_NAME:
        rset.icons.add(before, after)
    elif kind == IdKind.PATH_STRING:
        rset.paths.add_substring(before, after)


# --- Execution: meta files -----------------------------------------------


def _emit_meta(
    a: Project, b: Project, config: MergeConfig, result: MergeResult,
) -> None:
    """Write the two meta.lsx files (Mods + Projects) for the merged mod."""
    new_folder = config.new_folder

    # Mods/<NewFolder>/meta.lsx
    mod_meta = _meta.merge_mod_meta(
        a.mod_meta, b.mod_meta,
        new_uuid=config.new_uuid,
        new_folder=new_folder,
        new_name=config.new_name,
        new_author=config.new_author,
        new_description=config.new_description,
    )
    mod_meta_path = config.output_dir / "Mods" / new_folder / "meta.lsx"
    _mkdir_long(mod_meta_path.parent)
    _meta.write_mod_meta_file(mod_meta, mod_meta_path)
    result.emissions.append(FileEmission(
        output_path=mod_meta_path,
        source="regenerated",
        note="merged mod identity, union of dependencies + scripts",
    ))

    # Projects/<NewFolder>/meta.lsx: fresh project identity pointing at
    # the new mod UUID. We mint a separate UUID for the project itself.
    project_meta = _meta.ProjectMeta(
        uuid=_meta.generate_uuid(),
        module=config.new_uuid,
        name=config.new_name,
        game_project="",
        updated_dependencies="true",
        lsx_version=mod_meta.lsx_version,
    )
    project_meta_path = config.output_dir / "Projects" / new_folder / "meta.lsx"
    _mkdir_long(project_meta_path.parent)
    _meta.write_project_meta_file(project_meta, project_meta_path)
    result.emissions.append(FileEmission(
        output_path=project_meta_path,
        source="regenerated",
        note="fresh project identity",
    ))


# --- Execution: content files --------------------------------------------


def _emit_files(
    a: Project, b: Project, config: MergeConfig,
    remap_a: remap.RemapSet, remap_b: remap.RemapSet,
    result: MergeResult,
) -> None:
    """Emit every content file in either input to the output.

    For files of mergeable types where both inputs have a matching
    destination, run the format-specific merge. For everything else, copy
    with the folder remap applied.

    Path mapping: an input file at ``<bucket>/<old_folder>/<rest>`` goes
    to ``<bucket>/<new_folder>/<rest>``. Files not under any bucket
    (rare, e.g. a stray .vsdx file at the project root) are skipped with
    a recorded note.
    """
    new_folder = config.new_folder

    # Build dest -> (input, file, remap) maps so we can detect mergeable pairs.
    a_emissions: dict[Path, tuple[Project, CatalogedFile, remap.RemapSet]] = {}
    b_emissions: dict[Path, tuple[Project, CatalogedFile, remap.RemapSet]] = {}

    for cf in a.files:
        if cf.category in (FileCategory.MOD_META, FileCategory.PROJECT_META):
            continue  # handled by _emit_meta
        if cf.category in (FileCategory.STORY_COMPILED, FileCategory.STORY_LOG):
            result.skipped_files.append(cf.path)
            continue
        dest = _destination_for(cf, config.output_dir, new_folder)
        if dest is None:
            continue
        a_emissions[dest] = (a, cf, remap_a)

    for cf in b.files:
        if cf.category in (FileCategory.MOD_META, FileCategory.PROJECT_META):
            continue
        if cf.category in (FileCategory.STORY_COMPILED, FileCategory.STORY_LOG):
            result.skipped_files.append(cf.path)
            continue
        dest = _destination_for(cf, config.output_dir, new_folder)
        if dest is None:
            continue
        b_emissions[dest] = (b, cf, remap_b)

    # NEW: detect collisions on referenced binary assets (textures, models,
    # banks, VFX, paired XMLs) and rename B's copy rather than dropping it.
    # This is critical for things like icon atlases: two mods both shipping
    # a "newAtlas.dds" with different contents would otherwise lose B's
    # atlas, and every icon B's UI referenced would render with A's bitmap.
    # The renamed-asset path is also added to remap_b.paths so every
    # textual reference inside B's content (icon UV maps, root templates,
    # stats Icon= fields, ...) follows the new filename.
    b_emissions = _resolve_asset_collisions(
        a_emissions, b_emissions, b.mod_folder_name, remap_b, result,
        output_root=config.output_dir,
    )

    # Emit. Iterate over the union of destinations.
    all_dests = sorted(set(a_emissions) | set(b_emissions))
    report = config.progress_callback or (lambda *_, **__: None)
    total = len(all_dests)
    for i, dest in enumerate(all_dests):
        in_a = a_emissions.get(dest)
        in_b = b_emissions.get(dest)
        # Report relative path for log-friendliness: full paths bloat the UI.
        try:
            shown = str(dest.relative_to(config.output_dir))
        except ValueError:
            shown = dest.name
        report("emit", i, total, shown)
        if in_a and in_b:
            _emit_merged(dest, in_a, in_b, config, result)
        elif in_a:
            _emit_single(dest, in_a, result)
        elif in_b:
            _emit_single(dest, in_b, result)
    report("emit", total, total, f"Wrote {total} files")


# Binary asset categories where a same-path collision means
# "rename B's copy and rewrite refs", NOT "drop B's copy". These files are
# referenced by *path* from other content (icon UV maps reference textures,
# root templates reference models and banks, stats reference icons), so
# losing one breaks every reference into it.
_RENAME_ON_COLLIDE_CATEGORIES: frozenset[FileCategory] = frozenset({
    FileCategory.TEXTURE_DDS,            # icon atlases, packed textures
    FileCategory.TEXTURE_TIF,            # source textures
    FileCategory.MODEL_GR2,              # 3D models
    FileCategory.ASSET_IMPORT_SETTINGS,  # .xml paired with .gr2/.tif/.dds
    FileCategory.VFX_LSFX,               # visual effects definitions
    FileCategory.BANK_LSF,               # VisualBank/MaterialBank/TextureBank etc.
})

# IMAGE_ASSET (mod_publish_logo.png, thumbnail.png) is deliberately NOT in
# the rename-on-collide set: those are looked up by *fixed path* by the
# Toolkit and the Nexus mod page. Renaming wouldn't help: only one of
# them gets used regardless. The keep-A behavior with a logged conflict
# is the right call there; the user has to pick which logo/thumbnail
# represents the merged mod.


def _resolve_asset_collisions(
    a_emissions: dict[Path, tuple[Project, CatalogedFile, remap.RemapSet]],
    b_emissions: dict[Path, tuple[Project, CatalogedFile, remap.RemapSet]],
    b_mod_folder: str,
    remap_b: remap.RemapSet,
    result: MergeResult,
    *,
    output_root: Path,
) -> dict[Path, tuple[Project, CatalogedFile, remap.RemapSet]]:
    """Find binary-asset collisions and rename B's copy to keep both.

    For every destination path present in both ``a_emissions`` and
    ``b_emissions`` where the category is in
    ``_RENAME_ON_COLLIDE_CATEGORIES`` and the files are NOT byte-identical:

    1. Generate a new filename for B's copy
       (``<stem>__<b_folder_suffix>.<ext>``).
    2. Add a substring substitution to ``remap_b.paths`` so all of B's
       other content that references the old filename gets rewritten
       during the emit pass. The substitution is keyed by *bare filename*
       (no directory part) because that's how UV maps, root templates,
       and stats reference these assets in practice.
    3. Re-key B's emission dict so the file emits at the new destination.
    4. Handle paired files (``foo.dds`` + ``foo.xml``,
       ``foo.GR2`` + ``foo.xml``) atomically: if we rename the binary,
       we rename its companion XML to match, and vice versa, so the
       Toolkit's asset import system stays consistent.
    5. If A and B's files are byte-identical, no rename needed: they
       dedupe naturally.

    Returns a (possibly modified) ``b_emissions`` map. The original
    ``b_emissions`` argument is not mutated.

    Each rename emits a ``MergeConflict`` of kind
    ``asset_renamed_to_keep_both`` so the user sees what happened in
    the merge summary.
    """
    # Snapshot the b_emissions to a new dict: we'll mutate the copy.
    new_b: dict[Path, tuple[Project, CatalogedFile, remap.RemapSet]] = dict(b_emissions)

    # Use a deterministic suffix derived from B's folder name. We sanitize
    # to alphanum so it's safe in filenames on every filesystem.
    suffix = "_" + _sanitize_filename_suffix(b_mod_folder)

    # Pre-compute paired-file lookups: for every dest that's an
    # ASSET_IMPORT_SETTINGS XML, what's its paired binary's dest? And
    # vice versa? We need this so a rename of one drags the other.
    a_pairs = _build_pair_index(a_emissions)
    b_pairs = _build_pair_index(b_emissions)

    # Iterate over a copy of the keys: we may mutate new_b as we go.
    for dest in list(new_b.keys()):
        if dest not in a_emissions:
            continue
        # The previous iteration may have renamed (and removed) this
        # entry: for paired files, processing the binary's collision
        # also renames its sibling XML. Skip anything no longer in new_b.
        if dest not in new_b:
            continue
        _, cf_a, _ = a_emissions[dest]
        _, cf_b, _ = new_b[dest]
        if cf_a.category != cf_b.category:
            continue  # category-mismatch handled elsewhere
        if cf_a.category not in _RENAME_ON_COLLIDE_CATEGORIES:
            continue
        # Byte-identical files dedupe silently: no rename.
        if _files_byte_identical(cf_a.path, cf_b.path):
            continue

        # Rename B's copy. Find an unused new destination filename
        # (`foo.dds` → `foo<suffix>.dds`, fallback to `foo<suffix>_2.dds`
        # if that collides too).
        new_dest = _pick_renamed_dest(dest, suffix, a_emissions, new_b)
        if new_dest is None:
            # Couldn't find a free name (extremely unlikely: would need
            # thousands of suffix collisions). Fall back to keep-A so we
            # at least don't crash, and log a real conflict.
            result.conflicts.append(MergeConflict(
                kind="asset_rename_failed",
                identifier=str(dest.relative_to(output_root)),
                where_a=str(cf_a.rel_to_project_root),
                where_b=str(cf_b.rel_to_project_root),
                resolution="kept_a_copied_verbatim",
            ))
            continue

        # Add a remap so every textual reference to the bare filename in
        # B's content gets rewritten to the new bare filename. We map
        # `oldname.dds` → `newname.dds` (without directory) because
        # that's how Larian's UV maps / stats / root templates reference
        # textures: by bare name plus a separate Path or container hint.
        # The substring substitution also catches the slash-prefixed forms
        # that crop up in path strings ("Icons/newAtlas.dds").
        old_name = dest.name
        new_name = new_dest.name
        try:
            remap_b.paths.add_substring(old_name, new_name)
        except ValueError:
            # Two collisions both want to remap the same bare filename
            # differently: shouldn't happen unless the project itself
            # has duplicate filenames across different directories.
            # Be safe and bail with a logged conflict.
            result.conflicts.append(MergeConflict(
                kind="asset_rename_failed",
                identifier=str(dest.relative_to(output_root)),
                where_a=str(cf_a.rel_to_project_root),
                where_b=str(cf_b.rel_to_project_root),
                resolution="kept_a_copied_verbatim",
            ))
            continue

        # Re-key B's emission so the file emits at the new path.
        del new_b[dest]
        new_b[new_dest] = (b_emissions[dest][0], cf_b, remap_b)

        # If this file has a paired asset-import-settings XML (or vice
        # versa), rename the partner too. The Toolkit relies on stem
        # matching to associate them: diverging the stems would break
        # the importer.
        partner = b_pairs.get(dest)
        if partner is not None and partner in new_b:
            partner_new = new_dest.parent / (
                new_dest.stem + partner.suffix
            )
            # Skip if the partner would collide with anything else.
            if partner_new not in a_emissions and partner_new not in new_b:
                partner_old_name = partner.name
                partner_new_name = partner_new.name
                try:
                    remap_b.paths.add_substring(
                        partner_old_name, partner_new_name,
                    )
                except ValueError:
                    pass  # partner remap not critical; the import xml
                          # contents reference by stem anyway
                _, partner_cf, _ = new_b[partner]
                del new_b[partner]
                new_b[partner_new] = (
                    b_emissions[partner][0], partner_cf, remap_b,
                )

        # Log it so the user sees the rename in the summary.
        result.conflicts.append(MergeConflict(
            kind="asset_renamed_to_keep_both",
            identifier=str(dest.relative_to(output_root)),
            where_a=str(cf_a.rel_to_project_root),
            where_b=str(cf_b.rel_to_project_root),
            resolution=f"renamed_b_to:{new_name}",
        ))

    return new_b


def _sanitize_filename_suffix(folder_name: str) -> str:
    """Turn a mod folder name into a short, filename-safe suffix.

    Strips any UUID tail (``ModName_<36-char-uuid>`` → ``ModName``),
    then keeps alphanumerics only, capped to 16 chars. Falls back to
    ``"alt"`` if the result would be empty.
    """
    import re
    base = folder_name
    # Strip trailing _<uuid>
    base = re.sub(
        r"_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        "", base,
    )
    safe = "".join(c for c in base if c.isalnum())[:16]
    return safe or "alt"


def _build_pair_index(
    emissions: dict[Path, tuple[Project, CatalogedFile, remap.RemapSet]],
) -> dict[Path, Path]:
    """Map each ASSET_IMPORT_SETTINGS XML's dest to its sibling binary's
    dest (and the reverse).

    Asset-import-settings XMLs share a stem and directory with their
    binary counterpart (``foo.tif`` ↔ ``foo.xml``). When we rename one,
    we must rename the other to keep the importer's stem-matching alive.

    Returns a symmetric dict: looking up either side gives the other.
    """
    pairs: dict[Path, Path] = {}
    # Group emissions by (parent_dir, stem). A pair is any group that
    # contains an ASSET_IMPORT_SETTINGS and exactly one binary partner.
    from collections import defaultdict
    by_stem: dict[tuple[Path, str], list[Path]] = defaultdict(list)
    for dest in emissions:
        by_stem[(dest.parent, dest.stem)].append(dest)
    for group in by_stem.values():
        if len(group) != 2:
            continue
        d1, d2 = group
        _, cf1, _ = emissions[d1]
        _, cf2, _ = emissions[d2]
        # Pair up if one side is the XML and the other is a binary
        # asset that lives in the rename-on-collide set.
        if (cf1.category == FileCategory.ASSET_IMPORT_SETTINGS
                and cf2.category in _RENAME_ON_COLLIDE_CATEGORIES):
            pairs[d1] = d2
            pairs[d2] = d1
        elif (cf2.category == FileCategory.ASSET_IMPORT_SETTINGS
                and cf1.category in _RENAME_ON_COLLIDE_CATEGORIES):
            pairs[d1] = d2
            pairs[d2] = d1
    return pairs


def _pick_renamed_dest(
    original: Path,
    suffix: str,
    a_emissions: dict,
    b_emissions: dict,
) -> Path | None:
    """Pick a new filename for B's copy that doesn't collide.

    Tries ``<stem><suffix><ext>`` first. If that's also taken (rare:
    requires the user to have a file of exactly that name already),
    tries ``<stem><suffix>_2<ext>``, ``..._3``, etc., up to 999.
    Returns None if no free slot is found.
    """
    parent = original.parent
    stem = original.stem
    ext = original.suffix
    candidates = [parent / f"{stem}{suffix}{ext}"] + [
        parent / f"{stem}{suffix}_{n}{ext}" for n in range(2, 1000)
    ]
    for cand in candidates:
        if cand not in a_emissions and cand not in b_emissions:
            return cand
    return None


def _destination_for(
    cf: CatalogedFile, output_root: Path, new_folder: str,
) -> Path | None:
    """Translate an input file's path to its output path under ``output_root``.

    For files inside a bucket (Mods, Public, Editor/Mods, Projects), the
    bucket prefix is preserved and the mod-folder segment is rewritten.
    Files outside any bucket are skipped (returns None).
    """
    rel = cf.rel_to_project_root
    parts = rel.parts
    if cf.bucket == "Mods" and len(parts) >= 2:
        return output_root / "Mods" / new_folder / Path(*parts[2:])
    if cf.bucket == "Public" and len(parts) >= 2:
        return output_root / "Public" / new_folder / Path(*parts[2:])
    if cf.bucket == "Editor" and len(parts) >= 3:
        return output_root / "Editor" / "Mods" / new_folder / Path(*parts[3:])
    if cf.bucket == "Projects" and len(parts) >= 2:
        return output_root / "Projects" / new_folder / Path(*parts[2:])
    return None


def _emit_single(
    dest: Path,
    source_tuple: tuple[Project, CatalogedFile, remap.RemapSet],
    result: MergeResult,
) -> None:
    """One input has this file; the other doesn't. Apply that input's
    remap if it's a parsable format, otherwise copy verbatim."""
    _, cf, rset = source_tuple
    _mkdir_long(dest.parent)

    if cf.category == FileCategory.STATS_TXT:
        parsed = stats_text.parse_file(cf.path)
        if rset:
            remap.rewrite_stats_text(parsed, rset)
        stats_text.write_file(parsed, dest)
    elif cf.category == FileCategory.STATS_XML:
        parsed = stats_xml.parse_file(cf.path)
        if rset:
            remap.rewrite_stats_xml(parsed, rset)
        stats_xml.write_file(parsed, dest)
    elif cf.category == FileCategory.LOCALIZATION:
        parsed = localization.parse_file(cf.path)
        if rset:
            remap.rewrite_localization(parsed, rset)
        localization.write_file(parsed, dest)
    elif cf.category in (
        FileCategory.ROOT_TEMPLATE_LSX, FileCategory.BANK_LSX,
        FileCategory.UI_MERGED, FileCategory.ICON_UV_LSX,
        FileCategory.ROOT_TEMPLATE_MERGED, FileCategory.LEVEL_CONTENT_LSX,
    ):
        # These categories include both binary (.lsf) and text (.lsx /
        # .lsf.lsx) forms. We can only parse-and-rewrite the text form;
        # binary forms get copied verbatim. (LSF round-trip via divine.exe
        # is a future enhancement; for current use cases binary content
        # doesn't reference mod folder names.)
        if cf.path.name.endswith(".lsx"):
            parsed = lsx.parse_file(cf.path)
            if rset:
                remap.rewrite_lsx(parsed, rset)
            lsx.write_file(parsed, dest)
        else:
            _copy_long(cf.path, dest)
    elif cf.category in (
        FileCategory.STORY_GOAL, FileCategory.SE_LUA, FileCategory.STORY_HEADER,
        FileCategory.STORY_DEFINITIONS, FileCategory.STORY_ORPHAN_IGNORE,
    ):
        # Text files we don't parse structurally: substring rewrite.
        text = cf.path.read_text(encoding="utf-8", errors="replace")
        if rset:
            text = remap.rewrite_text_file(text, rset)
        from . import io_util
        io_util.write_text_safe(dest, text, encoding="utf-8")
    else:
        # Opaque binary or unparsed XML: copy bytes unchanged.
        # (Asset-import XMLs technically have a SourceFile attribute, but
        # they reference the source TIF/GR2 by filename, not by mod folder,
        # so substring rewriting isn't needed in practice.)
        _copy_long(cf.path, dest)

    result.emissions.append(FileEmission(
        output_path=dest,
        source=f"copied_from:{cf.rel_to_project_root}",
        note="single-input file",
    ))


def _emit_merged(
    dest: Path,
    a_tuple: tuple[Project, CatalogedFile, remap.RemapSet],
    b_tuple: tuple[Project, CatalogedFile, remap.RemapSet],
    config: MergeConfig,
    result: MergeResult,
) -> None:
    """Both inputs have a file mapping to this destination. Merge if we
    know how; raise for unknown overlapping categories so we can extend
    later if a fixture hits it."""
    _, cf_a, ra = a_tuple
    _, cf_b, rb = b_tuple
    if cf_a.category != cf_b.category:
        raise MergeError(
            f"both inputs target {dest} with different categories "
            f"({cf_a.category.value} vs {cf_b.category.value})"
        )

    _mkdir_long(dest.parent)

    if cf_a.category == FileCategory.STATS_TXT:
        parsed_a = stats_text.parse_file(cf_a.path)
        parsed_b = stats_text.parse_file(cf_b.path)
        if ra: remap.rewrite_stats_text(parsed_a, ra)
        if rb: remap.rewrite_stats_text(parsed_b, rb)
        prefix = config.conflict_prefix if config.conflict_policy == "prefix" else None
        merged, _ = stats_text.merge(parsed_a, parsed_b, prefix_b_on_conflict=prefix)
        stats_text.write_file(merged, dest)

    elif cf_a.category == FileCategory.STATS_XML:
        parsed_a = stats_xml.parse_file(cf_a.path)
        parsed_b = stats_xml.parse_file(cf_b.path)
        if ra: remap.rewrite_stats_xml(parsed_a, ra)
        if rb: remap.rewrite_stats_xml(parsed_b, rb)
        if parsed_a.stat_object_definition_id != parsed_b.stat_object_definition_id:
            raise MergeError(
                f"both inputs target {dest} with different stat_object_definition_id"
            )
        prefix = config.conflict_prefix if config.conflict_policy == "prefix" else None
        merged, _ = stats_xml.merge(parsed_a, parsed_b, prefix_b_on_conflict=prefix)
        stats_xml.write_file(merged, dest)

    elif cf_a.category == FileCategory.LOCALIZATION:
        parsed_a = localization.parse_file(cf_a.path)
        parsed_b = localization.parse_file(cf_b.path)
        if ra: remap.rewrite_localization(parsed_a, ra)
        if rb: remap.rewrite_localization(parsed_b, rb)
        merged, _ = localization.merge(parsed_a, parsed_b)
        localization.write_file(merged, dest)

    elif cf_a.category == FileCategory.TREASURE_TABLE:
        # Game-side runtime format: parse via treasure_table parser, merge
        # by table name. CanMerge=1 tables in both inputs concatenate
        # subtables (mirroring the game's own runtime behavior).
        parsed_a = treasure_table.parse_file(cf_a.path)
        parsed_b = treasure_table.parse_file(cf_b.path)
        # No remap applied: TreasureTable refs are stat names (categories)
        # and identifiers we treat conservatively. (Future enhancement:
        # apply remaps.stats to the category strings if needed.)
        prefix = config.conflict_prefix if config.conflict_policy == "prefix" else None
        try:
            merged, _ = treasure_table.merge(parsed_a, parsed_b, prefix_b_on_conflict=prefix)
        except ValueError as e:
            # itemtypes mismatch: refuse to silently mix.
            raise MergeError(str(e)) from e
        treasure_table.write_file(merged, dest)

    elif cf_a.category in _STRUCTURED_LSX_MERGE_CATEGORIES:
        # Keyed-list LSX/LSF (Progressions, SpellLists, ClassDescriptions,
        # editor tables, icon UV maps, UI registrations, GUI metadata,
        # _merged.lsf root template registry, etc.). When both mods provide
        # one, structurally union the entries by UUID/MapKey rather than
        # silently keeping A's. For .lsf binary forms we need divine.exe;
        # without it we fall back to keep-A with a conflict logged so the
        # user knows entries were dropped.
        merged = _try_merge_keyed_list_lsx(
            cf_a, cf_b, ra, rb, dest, config, result,
        )
        if not merged:
            _copy_long(cf_a.path, dest)
            if _files_byte_identical(cf_a.path, cf_b.path):
                result.emissions.append(FileEmission(
                    output_path=dest,
                    source=f"copied_from:{cf_a.rel_to_project_root}",
                    note=f"{cf_a.category.value}: byte-identical, deduped",
                ))
            else:
                # The category-specific bit tells the user *what kind* of
                # entries were dropped (progressions/widgets/icons/etc.)
                # so the message in the summary is actionable.
                kind = f"{cf_a.category.value}_unmerged"
                hint = ""
                if cf_a.path.name.endswith(".lsf") or cf_b.path.name.endswith(".lsf"):
                    hint = (
                        " Set divine.exe path in Settings and re-run to "
                        "structurally merge this file."
                    )
                result.conflicts.append(MergeConflict(
                    kind=kind,
                    identifier=str(dest.relative_to(config.output_dir)),
                    where_a=str(cf_a.rel_to_project_root),
                    where_b=str(cf_b.rel_to_project_root),
                    resolution="kept_a_copied_verbatim",
                ))
                result.emissions.append(FileEmission(
                    output_path=dest,
                    source=f"copied_from:{cf_a.rel_to_project_root}",
                    note=f"{cf_a.category.value}: union failed; kept A's, "
                         f"B's entries are NOT in the merged mod.{hint}",
                ))
            return

    elif cf_a.category == FileCategory.GUI_METADATA:
        # GUI/metadata.lsf is in _STRUCTURED_LSX_MERGE_CATEGORIES, so the
        # branch above handles it. This explicit branch is kept as a
        # no-op marker for the old keep-A fallback path that used to live
        # here: preserved for grep-ability but never reached.
        raise AssertionError(
            "GUI_METADATA should have been handled by the structured-LSX "
            "branch above; this branch is unreachable"
        )

    else:
        # Other overlapping categories: fall back to "A wins, B is dropped".
        # First check whether the two are byte-identical: framework
        # boilerplate (story_header.div, shared goal scripts, Toolkit
        # placeholders) often appears in multiple mods by the same author
        # and is literally the same bytes. Surfacing a "conflict" for
        # those would be pure noise. We only record a conflict when the
        # contents actually differ: that's the case the user might want
        # to review.
        _copy_long(cf_a.path, dest)
        if _files_byte_identical(cf_a.path, cf_b.path):
            result.emissions.append(FileEmission(
                output_path=dest,
                source=f"copied_from:{cf_a.rel_to_project_root}",
                note=f"both inputs had this {cf_a.category.value} file "
                     f"with identical content; deduped silently",
            ))
        else:
            result.conflicts.append(MergeConflict(
                kind="file_overlap",
                identifier=str(dest.relative_to(config.output_dir)),
                where_a=str(cf_a.rel_to_project_root),
                where_b=str(cf_b.rel_to_project_root),
                resolution="kept_a_copied_verbatim",
            ))
            result.emissions.append(FileEmission(
                output_path=dest,
                source=f"copied_from:{cf_a.rel_to_project_root}",
                note=f"both inputs had this {cf_a.category.value} file with "
                     f"DIFFERING content; kept A's, B's was at "
                     f"{cf_b.rel_to_project_root}",
            ))
        return

    result.emissions.append(FileEmission(
        output_path=dest,
        source="merged",
        note=f"from {cf_a.rel_to_project_root} + {cf_b.rel_to_project_root}",
    ))


def _files_byte_identical(a: Path, b: Path) -> bool:
    """Return True iff ``a`` and ``b`` contain the same bytes.

    Cheap path first: if file sizes differ, no contents can match. For
    same-size files we stream-compare in chunks rather than reading both
    fully into memory (matters when comparing multi-MB binary assets like
    the mod_publish_logo.png images in our fixtures).
    """
    try:
        sa = a.stat().st_size
        sb = b.stat().st_size
    except OSError:
        return False
    if sa != sb:
        return False
    chunk = 1 << 16  # 64KB
    with a.open("rb") as fa, b.open("rb") as fb:
        while True:
            ba = fa.read(chunk)
            bb = fb.read(chunk)
            if ba != bb:
                return False
            if not ba:
                return True


# Categories whose ``.lsx`` (or, with divine, ``.lsf``) form is a list of
# UUID-/MapKey-keyed entries we should union when both mods provide one.
# These are all "keyed list" files where two mods adding different entries
# is the normal case: Progressions tables, SpellLists, ClassDescriptions,
# UI registrations, icon UV maps, the collapsed _merged.lsf root template
# index, GUI metadata, etc.
#
# NOT included: ROOT_TEMPLATE_LSX/LSF and LEVEL_CONTENT_LSX/LSF: those are
# *per-entity* files (one file IS one root template / one level object).
# If both mods have the same UUID-named file, that's a real conflict and
# the keep-A behavior is correct.
_STRUCTURED_LSX_MERGE_CATEGORIES = frozenset({
    FileCategory.BANK_LSX,            # the catch-all for "generic LSX",
                                       # which captures Progressions.lsx,
                                       # SpellLists.lsx, ClassDescriptions.lsx,
                                       # Races.lsx, Feats.lsx, Tags/*.lsx,
                                       # Equipment.lsx, and the user's
                                       # "editor tables" under Editor/Mods/
    FileCategory.UI_MERGED,           # UI widget registrations
    FileCategory.ICON_UV_LSX,         # icon UV-coordinate registry (text)
    FileCategory.ICON_UV_LSF,         # icon UV-coordinate registry (binary)
    FileCategory.ROOT_TEMPLATE_MERGED, # RootTemplates/_merged.lsf|.lsf.lsx
    FileCategory.GUI_METADATA,        # Mods/<X>/GUI/metadata.lsf
})


def _try_merge_keyed_list_lsx(
    cf_a: CatalogedFile,
    cf_b: CatalogedFile,
    ra: remap.RemapSet,
    rb: remap.RemapSet,
    dest: Path,
    config: "MergeConfig",
    result: MergeResult,
) -> bool:
    """Structured merge of two BG3 keyed-list LSX/LSF files.

    Handles both text (``.lsx``) and binary (``.lsf``) forms. Binary
    requires divine.exe to convert to LSX and back. Returns True if the
    merge succeeded and ``dest`` has been written; False if divine is
    unavailable for a needed conversion, the structure isn't union-able,
    or any step failed: in which case the caller is responsible for
    the fallback (keep-A copy).

    Never raises: any failure short-circuits to False so the caller's
    fallback path is exercised, preserving the merger's overall
    "always produce output" property.

    This is a generalization of the original GUI-metadata-only merger.
    The same union-via-divine pipeline applies to any LSF whose payload
    is a UUID-keyed list of entries (Progressions, SpellLists, icon UV
    maps, root template registries, UI registrations, ...).
    """
    import tempfile

    a_is_text = cf_a.path.name.endswith(".lsx")
    b_is_text = cf_b.path.name.endswith(".lsx")

    # Text-only: parse directly, no divine needed.
    if a_is_text and b_is_text:
        try:
            a_doc = lsx.parse_file(cf_a.path)
            b_doc = lsx.parse_file(cf_b.path)
        except Exception:
            return False

        if ra:
            remap.rewrite_lsx(a_doc, ra)
        if rb:
            remap.rewrite_lsx(b_doc, rb)

        union_policy: lsx_merge.ConflictPolicy = (
            "fail" if config.conflict_policy == "fail" else "a_wins"
        )
        try:
            union = lsx_merge.union_documents(
                a_doc, b_doc, conflict_policy=union_policy,
            )
        except lsx_merge.UnionError:
            return False

        try:
            lsx.write_file(union.document, dest)
        except OSError:
            return False

        _record_union_result(union, cf_a, cf_b, dest, result)
        return True

    # At least one side is binary LSF: need divine for the conversion.
    if config.divine is None:
        return False

    with tempfile.TemporaryDirectory(prefix="bg3merge_lsx_") as td:
        td_path = Path(td)
        a_lsx_path = td_path / "a.lsx"
        b_lsx_path = td_path / "b.lsx"
        merged_lsx_path = td_path / "merged.lsx"

        # Convert each side to LSX if it isn't already.
        try:
            if a_is_text:
                a_lsx_path = cf_a.path
            else:
                config.divine.lsf_to_lsx(cf_a.path, a_lsx_path)
            if b_is_text:
                b_lsx_path = cf_b.path
            else:
                config.divine.lsf_to_lsx(cf_b.path, b_lsx_path)
        except (_divine.DivineError, _divine.DivineNotFoundError):
            return False

        try:
            a_doc = lsx.parse_file(a_lsx_path)
            b_doc = lsx.parse_file(b_lsx_path)
        except Exception:
            return False

        if ra:
            remap.rewrite_lsx(a_doc, ra)
        if rb:
            remap.rewrite_lsx(b_doc, rb)

        union_policy = (
            "fail" if config.conflict_policy == "fail" else "a_wins"
        )
        try:
            union = lsx_merge.union_documents(
                a_doc, b_doc, conflict_policy=union_policy,
            )
        except lsx_merge.UnionError:
            return False

        # Write the merged result in the destination's native format.
        # We key on the dest path's extension so the output matches what
        # the Toolkit expects to find at that location.
        try:
            if dest.name.endswith(".lsx"):
                lsx.write_file(union.document, dest)
            else:
                # Binary LSF output via divine roundtrip.
                lsx.write_file(union.document, merged_lsx_path)
                config.divine.lsx_to_lsf(merged_lsx_path, dest)
        except (_divine.DivineError, _divine.DivineNotFoundError, OSError):
            return False

    _record_union_result(union, cf_a, cf_b, dest, result)
    return True


def _record_union_result(
    union: lsx_merge.UnionResult,
    cf_a: CatalogedFile,
    cf_b: CatalogedFile,
    dest: Path,
    result: MergeResult,
) -> None:
    """Push the union's per-entry conflicts and a summary emission into
    the overall MergeResult. Used by ``_try_merge_keyed_list_lsx``."""
    for uc in union.conflicts:
        result.conflicts.append(MergeConflict(
            kind=f"{cf_a.category.value}_entry_conflict",
            identifier=f"{uc.region_id}/{uc.node_id}/{uc.identity}",
            where_a=str(cf_a.rel_to_project_root),
            where_b=str(cf_b.rel_to_project_root),
            resolution=uc.resolution,
        ))

    note_parts = [
        f"{cf_a.category.value} union: "
        f"{union.added_from_b} added from B",
        f"{union.deduped} deduped",
    ]
    if union.conflicts:
        note_parts.append(f"{len(union.conflicts)} conflicts (kept A's)")
    result.emissions.append(FileEmission(
        output_path=dest,
        source=f"merged_from:{cf_a.rel_to_project_root}+{cf_b.rel_to_project_root}",
        note="; ".join(note_parts),
    ))


def _try_merge_gui_metadata(
    cf_a: CatalogedFile,
    cf_b: CatalogedFile,
    ra: remap.RemapSet,
    rb: remap.RemapSet,
    dest: Path,
    config: "MergeConfig",
    result: MergeResult,
) -> bool:
    """Backward-compatible wrapper: GUI/metadata.lsf is now just one
    more category handled by the generic keyed-list merger."""
    return _try_merge_keyed_list_lsx(cf_a, cf_b, ra, rb, dest, config, result)
