"""Workspace discovery: list every Toolkit mod under a given directory.

Supports two workspace layouts:

1. **Canonical Toolkit workspace** (the BG3 Data folder): each mod is a
   subfolder under ``<workspace>/Mods/<X>/`` plus sibling subfolders
   under ``Editor/Mods/<X>/``, ``Public/<X>/``, and ``Projects/<X>/``.
   Multiple mods coexist by sharing those four bucket directories.
   The Toolkit reads/writes here directly.

2. **Self-contained project** (zipped or exported form): a folder that
   contains its own four-bucket subtree (``<projectdir>/Editor/Mods/<X>/``,
   ``<projectdir>/Mods/<X>/``, etc.). Useful when the user has projects
   sitting outside the Toolkit workspace — backups, sharing, etc.

We scan for BOTH because the user might have one or both in the same
workspace. Self-contained projects at the workspace root are found
opportunistically and are deduplicated against any canonical-layout
finds (so a self-contained snapshot of an already-active mod doesn't
appear twice in the picker).

The merger's full ``Project.load()`` walks the entire mod subtree
(thousands of files for a level mod). Doing that for every folder would
make the GUI sluggish. This module reads only each candidate's
``meta.lsx`` — under a second even for workspaces with dozens of mods.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import meta as _meta
from . import lsx as _lsx


# Folders that exist at the root of a canonical Toolkit workspace and
# are NOT individual mods — they're either the four shared buckets or
# Toolkit/base-game directories. We skip them when looking for
# self-contained project subdirs at the workspace root.
_RESERVED_WORKSPACE_FOLDERS = frozenset({
    "Editor", "Mods", "Projects", "Public",                 # the 4 buckets
    "Generated", "Localization", "Processed", "Scripts",    # base/toolkit
    "bin", "templates",
})


@dataclass
class DiscoveredProject:
    """A workspace entry: just enough to display in a picker.

    ``data_root`` is the directory that contains the four bucket subfolders
    (``Editor/Mods/<X>``, ``Mods/<X>``, ``Public/<X>``, ``Projects/<X>``).
    For canonical-layout mods this is the workspace itself; for
    self-contained projects this is the project's own directory.
    ``mod_folder_name`` is ``<X>`` — the subfolder name that's shared
    across all four buckets.

    All identity fields are read from ``Mods/<X>/meta.lsx``. We
    deliberately do NOT count files, walk the tree, or build a reference
    index here — that work happens only after the user selects a project.
    """
    data_root: Path
    mod_folder_name: str
    mod_name: str
    mod_uuid: str
    author: str
    description: str
    # Hint to the GUI about the discovered layout — useful for warnings
    # ("merging in-place into a self-contained project will collapse it
    # into the workspace") and for choosing the right output strategy.
    # Values: "canonical" (shared workspace) or "self_contained".
    layout: str = "canonical"

    @property
    def display_label(self) -> str:
        if self.author:
            return f"{self.mod_name}  —  by {self.author}"
        return self.mod_name

    @property
    def project_root(self) -> Path:
        """Backward-compatible alias for code that wants 'the path of
        this project' as a single value. For canonical-layout mods this
        is the data_root (workspace); for self-contained projects it's
        the project directory. Useful for display only — to actually
        load the project, callers should pass ``data_root`` and
        ``mod_folder_name`` to ``Project.load()`` so the mod is loaded
        in isolation from any siblings sharing the workspace."""
        return self.data_root

    @property
    def identity_key(self) -> tuple[Path, str]:
        """A unique key for this mod within (and across) workspaces.
        Two DiscoveredProjects are the same mod iff their identity_keys
        match. Use this for equality checks rather than comparing
        ``project_root`` alone — in a canonical workspace many mods
        share the same data_root, so project_root isn't unique."""
        return (self.data_root, self.mod_folder_name)


@dataclass
class DiscoveryError:
    """A folder that didn't load — surfaced in the GUI's "skipped" panel."""
    folder: Path
    reason: str


def discover_projects(
    workspace: Path | str,
) -> tuple[list[DiscoveredProject], list[DiscoveryError]]:
    """Scan ``workspace`` for every Toolkit mod present.

    Returns ``(found, skipped)``. Both lists are sorted alphabetically
    by mod name. ``found`` is what the GUI shows in the picker;
    ``skipped`` is what it surfaces in a "couldn't read these N folders"
    tooltip so the user can investigate.

    Tolerant by design: a single bad folder never raises. Errors are
    collected and the scan moves on.
    """
    workspace = Path(workspace).resolve()
    found: list[DiscoveredProject] = []
    errors: list[DiscoveryError] = []

    if not workspace.is_dir():
        return found, errors

    # --- Pass 1: canonical workspace layout -------------------------------
    # Each mod is at <workspace>/Mods/<X>/meta.lsx. This is the BG3
    # Toolkit's native layout — the user's actual Data folder.
    canonical_mods_dir = workspace / "Mods"
    canonical_folder_names: set[str] = set()
    if canonical_mods_dir.is_dir():
        for child in sorted(canonical_mods_dir.iterdir()):
            if not child.is_dir():
                continue
            meta_path = child / "meta.lsx"
            if not meta_path.is_file():
                # Could be PAK extractions or partial mods; silently skip.
                continue
            disc = _load_discovered(
                meta_path=meta_path,
                data_root=workspace,
                expected_folder_name=child.name,
                layout="canonical",
                errors=errors,
            )
            if disc is not None:
                found.append(disc)
                canonical_folder_names.add(disc.mod_folder_name)

    # --- Pass 2: self-contained projects at workspace root ----------------
    # Each one is a folder containing its own 4-bucket subtree. We skip
    # the reserved canonical folders we just scanned, and we silently
    # skip anything that doesn't look like a project.
    for child in sorted(workspace.iterdir()):
        if not child.is_dir() or child.name in _RESERVED_WORKSPACE_FOLDERS:
            continue
        meta_candidates = list(child.glob("Mods/*/meta.lsx"))
        if not meta_candidates:
            # Not a self-contained project; silently skip. Many things
            # at workspace root are unrelated (PAK extractions etc.).
            continue
        if len(meta_candidates) > 1:
            errors.append(DiscoveryError(
                folder=child,
                reason=f"self-contained project has {len(meta_candidates)} "
                       f"Mods/<Folder>/meta.lsx files (expected 1)",
            ))
            continue
        meta_path = meta_candidates[0]
        disc = _load_discovered(
            meta_path=meta_path,
            data_root=child,
            expected_folder_name=meta_path.parent.name,
            layout="self_contained",
            errors=errors,
        )
        if disc is None:
            continue
        # Deduplicate against canonical finds — if this self-contained
        # project is just a snapshot of a mod that's already active in
        # the canonical workspace, don't list it twice.
        if disc.mod_folder_name in canonical_folder_names:
            continue
        found.append(disc)

    # Stable, case-insensitive sort by mod name for the picker.
    found.sort(key=lambda d: d.mod_name.lower())
    return found, errors


def _load_discovered(
    meta_path: Path,
    data_root: Path,
    expected_folder_name: str,
    layout: str,
    errors: list[DiscoveryError],
) -> DiscoveredProject | None:
    """Parse a meta.lsx and return a DiscoveredProject, or append an
    error and return None on failure. Shared between the two passes."""
    try:
        doc = _lsx.parse_file(meta_path)
        mm = _meta.parse_mod_meta(doc)
    except Exception as e:
        errors.append(DiscoveryError(
            folder=meta_path.parent,
            reason=f"meta.lsx parse failed: {type(e).__name__}: {e}",
        ))
        return None

    if expected_folder_name != mm.folder:
        errors.append(DiscoveryError(
            folder=meta_path.parent,
            reason=f"meta.lsx Folder={mm.folder!r} but on-disk "
                   f"directory is {expected_folder_name!r}",
        ))
        return None

    return DiscoveredProject(
        data_root=data_root,
        mod_folder_name=mm.folder,
        mod_name=mm.name,
        mod_uuid=mm.uuid,
        author=mm.author,
        description=mm.description,
        layout=layout,
    )
