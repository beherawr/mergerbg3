"""Project model: walk a Toolkit project tree and catalog its contents.

A Toolkit project lives under a directory that contains some subset of:

    <ProjectRoot>/
        Editor/Mods/<ModFolder>/...        Toolkit working files (.stats etc.)
        Mods/<ModFolder>/                  Mod identity + scripts + story + loca
        Projects/<ModFolder>/              Toolkit project meta
        Public/<ModFolder>/                Packed-mod public content

This module discovers the structure, finds the mod folder name from
meta.lsx, and produces a ``Project`` object with every file categorized
by purpose. The merger then operates on Projects, not raw file paths.

Design goals:
- Forgiving discovery: a project missing some sections (no Editor/, no
  Story/) should still load. Real fixture projects vary in what they
  include.
- No parsing here: this module finds *paths*. The parsers in
  ``stats_text``, ``stats_xml``, ``lsx``, etc. are invoked by the merger
  on demand. Keeps memory usage bounded and lets us load big projects
  quickly for the GUI preview screen.
- File categories are an open enum: unknown files land in ``other`` rather
  than failing. A future Toolkit patch could add new directories and we'd
  still report them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import meta as _meta


class FileCategory(Enum):
    """The kinds of files the merger knows about.

    See ``bg3-merger-project-findings.md`` for the file-type inventory
    that produced this list.
    """
    MOD_META = "mod_meta"               # Mods/<Folder>/meta.lsx
    PROJECT_META = "project_meta"       # Projects/<Folder>/meta.lsx

    STATS_TXT = "stats_txt"             # Public/<Folder>/Stats/Generated/Data/*.txt
    STATS_XML = "stats_xml"             # Editor/Mods/<Folder>/Stats/<Cat>/*.stats
    TREASURE_TABLE = "treasure_table"   # Stats/Generated/TreasureTable.txt etc.

    LOCALIZATION = "localization"       # Mods/<Folder>/Localization/<Lang>/*.xml
    LOCALIZATION_PACKED = "loca_packed" # Mods/<Folder>/Localization/<Lang>/*.loca

    ROOT_TEMPLATE_LSF = "root_template_lsf"     # Public/<Folder>/RootTemplates/<uuid>.lsf
    ROOT_TEMPLATE_LSX = "root_template_lsx"     # Public/<Folder>/RootTemplates/<uuid>.lsx
    ROOT_TEMPLATE_MERGED = "root_template_merged"   # RootTemplates/_merged.lsf + .lsf.lsx
    BANK_LSF = "bank_lsf"               # Public/<Folder>/Content/[PAK]_*/*.lsf
    BANK_LSX = "bank_lsx"               # Public/<Folder>/Content/[PAK]_*/*.lsx
    UI_MERGED = "ui_merged"             # Public/<Folder>/Content/UI/[PAK]_UI/_merged.lsx / .lsf / .lsf.lsx
    # Virtual texture registry: Public/<Folder>/Content/[PAK]_VirtualTextures/
    # _merged.{lsx,lsf,lsf.lsx}. Holds VirtualTextureBank entries that
    # map a GTexFileName UUID to the .gts tileset on disk. Critically,
    # the Path attribute on each entry uses the full "Public/<Folder>/
    # Assets/VirtualTextures/<name>.gts" form (mod folder name included),
    # so a merge that renames the mod folder MUST rewrite these paths
    # or virtual textures will appear black in-game.
    VIRTUAL_TEXTURE_BANK = "virtual_texture_bank"
    # The baked virtual-texture data the Toolkit dumps under
    # ``Data/Generated/Public/<Folder>/VirtualTextures/`` after the
    # author runs "Build Virtual Textures":
    #   - ``.gts``  tile-set binaries (one per tileset)
    #   - ``.gtp``  tile-page binaries (mips + per-channel pages)
    #   - ``.gtex`` per-texture metadata blobs
    # Filenames ARE the identity here — the VirtualTextureBank LSF
    # references each tileset by its UUID-derived filename
    # (``TileSetFileName="<UUID>"`` matches ``<UUID>.gts``), and the
    # toolkit-generated ``GTexFileName`` matches the per-texture
    # ``.gtex`` hash name. RENAMING ANY OF THESE FILES BREAKS THE
    # VTB → on-disk link and the game renders the affected meshes
    # black. So these files must be copied byte-for-byte with their
    # original names preserved; on filename collision between two
    # mods the content is byte-identical (toolkit-deterministic for
    # the same input) so dedup is correct.
    VIRTUAL_TEXTURE_ASSET = "virtual_texture_asset"
    ICON_UV_LSX = "icon_uv_lsx"         # Public/<Folder>/GUI/<Name>_Icons.lsx (or Icons_*.lsx)
    ICON_UV_LSF = "icon_uv_lsf"         # Binary parallel of the above

    VFX_LSFX = "vfx_lsfx"               # Public/<Folder>/Assets/Effects/.../*.lsfx
    MODEL_GR2 = "model_gr2"             # Public/<Folder>/...*.GR2 (paired with .xml)
    TEXTURE_TIF = "texture_tif"         # Public/<Folder>/...*.tif (paired with .xml)
    TEXTURE_DDS = "texture_dds"         # Public/<Folder>/...*.dds (packed)
    ASSET_IMPORT_SETTINGS = "asset_import_settings"  # XML paired with GR2/TIF

    STORY_GOAL = "story_goal"           # Mods/<Folder>/Story/RawFiles/Goals/*.txt
    STORY_HEADER = "story_header"       # Mods/<Folder>/Story/RawFiles/story_header.div
    STORY_DEFINITIONS = "story_definitions"     # Mods/<Folder>/Story/RawFiles/story_definitions.div
    STORY_ORPHAN_IGNORE = "story_orphan_ignore" # Mods/<Folder>/Story/story_orphanqueries_ignore.txt
    STORY_COMPILED = "story_compiled"   # *.osi, story.div, goals.raw, story_ac.dat
    STORY_LOG = "story_log"             # Mods/<Folder>/Story/log.txt

    SE_LUA = "se_lua"                   # Mods/<Folder>/Scripts/**/*.lua
    SE_LUA_CONFIG = "se_lua_config"     # Mods/<Folder>/Scripts/**/config/*

    GUI_METADATA = "gui_metadata"       # Mods/<Folder>/GUI/metadata.lsf
    LEVEL_DATA = "level_data"           # Editor/Mods/.../Levels/...   (catch-all editor level files)
    MINIMAP = "minimap"                 # Editor/Mods/.../Minimaps/MinimapAtlas.mmxml

    # Level content (per-region object/trigger/light/scenery placements)
    # under Mods/<Folder>/Levels/<Region>/<Type>/<uuid>.lsf: binary
    # placement files generated by the Toolkit when you build a region.
    LEVEL_CONTENT_LSF = "level_content_lsf"

    # Editor-side LSX form of the same content (rare: Toolkit usually writes LSF only).
    LEVEL_CONTENT_LSX = "level_content_lsx"

    # Mods/<Folder>/Levels/<Region>/Ai/{aigrid.data, materialgrid.data,
    # chasms.lsf, navigationPortals.lsf}: compiled AI grid for pathing.
    LEVEL_AI = "level_ai"

    # Mods/<Folder>/Levels/<Region>/HLOD.lsf and similar: baked HLOD.
    LEVEL_HLOD = "level_hlod"

    # Mods/<Folder>/Levels/<Region>/Terrains/<uuid>_0.bin + _0_0.patch: packed terrain.
    LEVEL_TERRAIN = "level_terrain"

    # Globals/<Region>/<Type>/<uuid>.lsf: global "patch" content that
    # injects objects/triggers into BASE GAME regions (WLD_Main_A,
    # CTY_Main_A, BGO_Main_A, etc.). Mechanically identical to
    # LEVEL_CONTENT_LSF but lives under the Globals/ bucket.
    GLOBALS_LSF = "globals_lsf"

    # Editor/Mods/<Folder>/Levels/<Region>/SelectionGroups/<uuid>.json:
    # Toolkit editor selection-group metadata. Purely editor-state; the
    # game doesn't read this, but Toolkit reuses the UUIDs.
    EDITOR_METADATA_JSON = "editor_metadata_json"

    # Mods/<Folder>/mod_publish_logo.png, Projects/<Folder>/thumbnail.png
    IMAGE_ASSET = "image_asset"

    OTHER = "other"                     # forward-compat catch-all


@dataclass
class CatalogedFile:
    """One file in a project, with its category, full path, and the path
    *relative to the mod folder* for path-remap purposes."""
    path: Path
    category: FileCategory
    # Path relative to ProjectRoot: useful for diffing & logging.
    rel_to_project_root: Path
    # Path relative to ProjectRoot/<bucket>/<ModFolder>/, where bucket is
    # "Public" or "Mods" or "Editor/Mods" or "Projects". This is what gets
    # rewritten when we change the mod folder name. None if the file isn't
    # under a <ModFolder>-scoped directory.
    rel_under_mod_folder: Path | None
    # Which top-level bucket: "Public", "Mods", "Editor", "Projects", or "Other".
    bucket: str


@dataclass
class Project:
    """A walked Toolkit project. Created via ``Project.load(path)``."""
    root: Path                          # ProjectRoot itself
    mod_folder_name: str                # the UUID-suffixed folder name
    mod_meta: _meta.ModMeta             # from Mods/<Folder>/meta.lsx
    project_meta: _meta.ProjectMeta | None  # from Projects/<Folder>/meta.lsx; may be missing
    files: list[CatalogedFile] = field(default_factory=list)

    @classmethod
    def load(
        cls,
        root: Path | str,
        mod_folder_name: str | None = None,
    ) -> "Project":
        """Load a project from ``root``.

        Two valid layouts are supported:

        1. **Self-contained project** (zipped/shared form): ``root`` is the
           project directory itself, and there's exactly one
           ``Mods/<X>/meta.lsx`` to find. Pass ``mod_folder_name=None``
           and Project.load auto-detects the mod.

        2. **Canonical Toolkit workspace** (e.g. the BG3 Data folder):
           ``root`` is the workspace shared by many mods, where each mod
           is a subfolder under ``Editor/Mods/<X>/``, ``Mods/<X>/``,
           ``Public/<X>/``, and ``Projects/<X>/``. Pass the specific
           ``mod_folder_name`` so we know which mod to load (and so the
           file walk doesn't pull in other mods' content).

        Either way, the returned Project's ``files`` list contains only
        files belonging to ``<mod_folder_name>``: we restrict the tree
        walk to the four mod-specific subtrees rather than rglob the
        whole root.
        """
        root = Path(root).resolve()

        if mod_folder_name is None:
            # Auto-detect: there must be exactly one Mods/<X>/meta.lsx
            # under root. Multiple = canonical workspace; ask the caller
            # to disambiguate by passing mod_folder_name explicitly.
            mod_meta_paths = sorted(root.glob("Mods/*/meta.lsx"))
            if not mod_meta_paths:
                raise ValueError(
                    f"No Mods/<Folder>/meta.lsx found under {root}. "
                    "Is this actually a Toolkit project root?"
                )
            if len(mod_meta_paths) > 1:
                names = [p.parent.name for p in mod_meta_paths]
                raise ValueError(
                    f"Multiple Mods/<Folder>/meta.lsx files found under "
                    f"{root}: {names}. This looks like a shared Toolkit "
                    f"workspace; pass mod_folder_name= explicitly to "
                    f"load just one mod."
                )
            mod_meta_path = mod_meta_paths[0]
            mod_folder_name = mod_meta_path.parent.name
        else:
            # Explicit: caller knows which mod they want.
            mod_meta_path = root / "Mods" / mod_folder_name / "meta.lsx"
            if not mod_meta_path.is_file():
                raise ValueError(
                    f"No meta.lsx at {mod_meta_path}. "
                    f"Workspace doesn't appear to contain a mod named "
                    f"{mod_folder_name!r}."
                )

        mod_meta = _meta.parse_mod_meta_file(mod_meta_path)

        # Verify the on-disk folder name matches what meta.lsx says.
        on_disk_folder = mod_meta_path.parent.name
        if on_disk_folder != mod_meta.folder:
            raise ValueError(
                f"meta.lsx Folder attribute is {mod_meta.folder!r} "
                f"but the on-disk folder is {on_disk_folder!r}. "
                "These must match for the Toolkit to load the project."
            )

        # Project meta is optional: non-Toolkit-authored mods don't have it.
        project_meta: _meta.ProjectMeta | None = None
        project_meta_path = root / "Projects" / mod_folder_name / "meta.lsx"
        if project_meta_path.is_file():
            project_meta = _meta.parse_project_meta_file(project_meta_path)

        # Walk ONLY the mod-specific subtrees. For self-contained
        # projects this catches everything (since the project IS the
        # mod's subtree); for canonical workspaces this correctly
        # excludes files belonging to other mods sharing the same root.
        #
        # ``Generated/Public/<mod>/`` is the OUTPUT of the Toolkit's
        # build pipeline — primarily ``.gts``/``.gtp``/``.gtex``
        # virtual-texture data plus baked ``.GR2`` model copies. The
        # game's runtime resolves VirtualTextureBank references
        # (TileSetFileName + GTexFileName) through this directory. We
        # need to walk it because if a mod has virtual textures and
        # the merger doesn't carry the Generated/ tree over to the
        # merged mod, the VTB ends up pointing at filenames that don't
        # exist on disk and the game renders the affected meshes
        # black — even though the VTB itself is correctly merged.
        files: list[CatalogedFile] = []
        mod_subtrees = [
            root / "Editor" / "Mods" / mod_folder_name,
            root / "Mods" / mod_folder_name,
            root / "Public" / mod_folder_name,
            root / "Projects" / mod_folder_name,
            root / "Generated" / "Public" / mod_folder_name,
        ]
        for subtree in mod_subtrees:
            if not subtree.is_dir():
                continue
            for file_path in sorted(subtree.rglob("*")):
                if not file_path.is_file():
                    continue
                files.append(_categorize(file_path, root, mod_folder_name))

        return cls(
            root=root,
            mod_folder_name=mod_folder_name,
            mod_meta=mod_meta,
            project_meta=project_meta,
            files=files,
        )

    # --- Query helpers -----------------------------------------------------

    def files_by_category(self, category: FileCategory) -> list[CatalogedFile]:
        return [f for f in self.files if f.category == category]

    def categories_present(self) -> set[FileCategory]:
        return {f.category for f in self.files}

    def file_count_by_category(self) -> dict[FileCategory, int]:
        counts: dict[FileCategory, int] = {}
        for f in self.files:
            counts[f.category] = counts.get(f.category, 0) + 1
        return counts

    def summary(self) -> str:
        """Human-readable digest. Used by the GUI's project-preview screen."""
        lines = [
            f"Project: {self.mod_meta.name!r}  (UUID {self.mod_meta.uuid})",
            f"Folder : {self.mod_folder_name}",
            f"Author : {self.mod_meta.author!r}",
            f"Files  : {len(self.files)}",
        ]
        if self.mod_meta.dependencies:
            lines.append("Deps   :")
            for dep in self.mod_meta.dependencies:
                lines.append(f"    {dep.name!r} ({dep.uuid})")
        lines.append("Categories:")
        for cat, count in sorted(
            self.file_count_by_category().items(),
            key=lambda kv: kv[0].value,
        ):
            lines.append(f"    {cat.value:<24} {count:>3}")
        return "\n".join(lines)


# --- Categorization --------------------------------------------------------


def _categorize(
    file_path: Path,
    project_root: Path,
    mod_folder_name: str,
) -> CatalogedFile:
    """Bucket a file into a FileCategory based on its path.

    Decision is path-based only: we don't open the file. This is fast and
    enables the GUI to show a project catalog immediately on load.
    """
    rel = file_path.relative_to(project_root)
    parts = rel.parts
    name = file_path.name

    # Determine bucket and rel_under_mod_folder.
    bucket = "Other"
    rel_under_mod: Path | None = None
    if len(parts) >= 2 and parts[0] == "Mods" and parts[1] == mod_folder_name:
        bucket = "Mods"
        rel_under_mod = Path(*parts[2:]) if len(parts) > 2 else Path()
    elif len(parts) >= 2 and parts[0] == "Public" and parts[1] == mod_folder_name:
        bucket = "Public"
        rel_under_mod = Path(*parts[2:]) if len(parts) > 2 else Path()
    elif (len(parts) >= 3 and parts[0] == "Editor"
          and parts[1] == "Mods" and parts[2] == mod_folder_name):
        bucket = "Editor"
        rel_under_mod = Path(*parts[3:]) if len(parts) > 3 else Path()
    elif len(parts) >= 2 and parts[0] == "Projects" and parts[1] == mod_folder_name:
        bucket = "Projects"
        rel_under_mod = Path(*parts[2:]) if len(parts) > 2 else Path()
    elif (len(parts) >= 3 and parts[0] == "Generated"
          and parts[1] == "Public" and parts[2] == mod_folder_name):
        # Generated/Public/<mod>/... is the Toolkit's baked-asset
        # output for one mod. We bucket it as "Generated" so the
        # merger knows to copy it to the merged mod's matching
        # Generated/Public/<new_mod>/... location.
        bucket = "Generated"
        rel_under_mod = Path(*parts[3:]) if len(parts) > 3 else Path()

    # --- Categorize by path pattern -----------------------------------

    # meta.lsx variants
    if name == "meta.lsx":
        if bucket == "Mods":
            cat = FileCategory.MOD_META
        elif bucket == "Projects":
            cat = FileCategory.PROJECT_META
        else:
            cat = FileCategory.OTHER
        return CatalogedFile(file_path, cat, rel, rel_under_mod, bucket)

    # Stats: packed .txt under Public/<Mod>/Stats/Generated/Data/
    if (bucket == "Public" and rel_under_mod is not None
            and len(rel_under_mod.parts) >= 4
            and rel_under_mod.parts[0] == "Stats"
            and rel_under_mod.parts[1] == "Generated"
            and rel_under_mod.parts[2] == "Data"
            and name.endswith(".txt")):
        return CatalogedFile(
            file_path, FileCategory.STATS_TXT, rel, rel_under_mod, bucket,
        )

    # TreasureTable, Equipment, ItemTypes, etc. live alongside Data/
    if (bucket == "Public" and rel_under_mod is not None
            and len(rel_under_mod.parts) == 3
            and rel_under_mod.parts[0] == "Stats"
            and rel_under_mod.parts[1] == "Generated"
            and name.endswith(".txt")):
        return CatalogedFile(
            file_path, FileCategory.TREASURE_TABLE, rel, rel_under_mod, bucket,
        )

    # Stats: Toolkit source .stats under Editor/Mods/<Mod>/Stats/<Cat>/
    if (bucket == "Editor" and rel_under_mod is not None
            and len(rel_under_mod.parts) >= 3
            and rel_under_mod.parts[0] == "Stats"
            and name.endswith(".stats")):
        return CatalogedFile(
            file_path, FileCategory.STATS_XML, rel, rel_under_mod, bucket,
        )

    # Localization
    if (bucket == "Mods" and rel_under_mod is not None
            and len(rel_under_mod.parts) >= 3
            and rel_under_mod.parts[0] == "Localization"):
        if name.endswith(".xml"):
            return CatalogedFile(
                file_path, FileCategory.LOCALIZATION, rel, rel_under_mod, bucket,
            )
        if name.endswith(".loca"):
            return CatalogedFile(
                file_path, FileCategory.LOCALIZATION_PACKED, rel, rel_under_mod, bucket,
            )

    # Root templates
    if (bucket == "Public" and rel_under_mod is not None
            and len(rel_under_mod.parts) >= 2
            and rel_under_mod.parts[0] == "RootTemplates"):
        # _merged.lsf and _merged.lsf.lsx are a binary+text parallel pair
        # the Toolkit writes when it collapses all per-uuid templates into
        # one file. Treat as their own category so the merger knows they
        # might be present alongside the individual files.
        if name in {"_merged.lsf", "_merged.lsf.lsx"}:
            return CatalogedFile(
                file_path, FileCategory.ROOT_TEMPLATE_MERGED, rel, rel_under_mod, bucket,
            )
        if name.endswith(".lsf"):
            return CatalogedFile(
                file_path, FileCategory.ROOT_TEMPLATE_LSF, rel, rel_under_mod, bucket,
            )
        if name.endswith(".lsx"):
            return CatalogedFile(
                file_path, FileCategory.ROOT_TEMPLATE_LSX, rel, rel_under_mod, bucket,
            )

    # UI atlas registry: Public/<Mod>/Content/UI/[PAK]_UI/_merged.{lsx,lsf,lsf.lsx}.
    # All three forms map to one category; the merger picks the LSX form
    # for parsing when available.
    if (bucket == "Public" and rel_under_mod is not None
            and "Content" in rel_under_mod.parts
            and "UI" in rel_under_mod.parts
            and any("[PAK]_UI" in p for p in rel_under_mod.parts)
            and name in {"_merged.lsx", "_merged.lsf", "_merged.lsf.lsx"}):
        return CatalogedFile(
            file_path, FileCategory.UI_MERGED, rel, rel_under_mod, bucket,
        )

    # Icon UV coordinates under Public/<Mod>/GUI/. The Toolkit-default
    # name is ``Icons_<modtag>.lsx`` but human authors use any name
    # (``Simple_Icons.lsx``, ``MyMod_GUI.lsx``, etc.). We match by location.
    # Per Bloodfang, both LSX and LSF parallel forms can coexist.
    if (bucket == "Public" and rel_under_mod is not None
            and len(rel_under_mod.parts) >= 2
            and rel_under_mod.parts[0] == "GUI"
            and "Assets" not in rel_under_mod.parts):
        if name.endswith(".lsx"):
            return CatalogedFile(
                file_path, FileCategory.ICON_UV_LSX, rel, rel_under_mod, bucket,
            )
        if name.endswith(".lsf"):
            return CatalogedFile(
                file_path, FileCategory.ICON_UV_LSF, rel, rel_under_mod, bucket,
            )

    # Virtual texture registry: Public/<Mod>/Content/[PAK]_VirtualTextures/
    # _merged.{lsx,lsf,lsf.lsx}. Parallel to UI_MERGED. Has to come
    # BEFORE the generic BANK_LSF rule below or it'd be swallowed and
    # treated as an opaque bank instead of a registry whose binary
    # paths need remapping on mod-folder rename.
    if (bucket == "Public" and rel_under_mod is not None
            and "Content" in rel_under_mod.parts
            and any("[PAK]_VirtualTextures" in p for p in rel_under_mod.parts)
            and name in {"_merged.lsx", "_merged.lsf", "_merged.lsf.lsx"}):
        return CatalogedFile(
            file_path, FileCategory.VIRTUAL_TEXTURE_BANK, rel, rel_under_mod, bucket,
        )

    # Banks (VisualBank/MaterialBank/TextureBank/etc.) under Content/[PAK]_*/
    if (bucket == "Public" and rel_under_mod is not None
            and "Content" in rel_under_mod.parts
            and any(p.startswith("[PAK]_") for p in rel_under_mod.parts)):
        if name.endswith(".lsf"):
            return CatalogedFile(
                file_path, FileCategory.BANK_LSF, rel, rel_under_mod, bucket,
            )
        if name.endswith(".lsx"):
            return CatalogedFile(
                file_path, FileCategory.BANK_LSX, rel, rel_under_mod, bucket,
            )

    # VFX
    if name.endswith(".lsfx"):
        return CatalogedFile(
            file_path, FileCategory.VFX_LSFX, rel, rel_under_mod, bucket,
        )

    # Models
    if name.endswith(".GR2") or name.endswith(".gr2"):
        return CatalogedFile(
            file_path, FileCategory.MODEL_GR2, rel, rel_under_mod, bucket,
        )

    # Source textures (TIF)
    if name.lower().endswith(".tif") or name.lower().endswith(".tiff"):
        return CatalogedFile(
            file_path, FileCategory.TEXTURE_TIF, rel, rel_under_mod, bucket,
        )

    # Virtual texture data the Toolkit bakes into Generated/Public/
    # <Mod>/VirtualTextures/:
    #   .gts   — tileset binaries  (one per VirtualTexture resource)
    #   .gtp   — tile pages        (mip + per-channel data for a tileset)
    #   .gtex  — per-texture metadata blobs
    # Filenames ARE the identity: each one's name (a UUID or content
    # hash) is what the VirtualTextureBank references in its
    # TileSetFileName / GTexFileName attributes. Renaming any of these
    # files breaks the VTB → on-disk link and produces black meshes
    # in-game, so VIRTUAL_TEXTURE_ASSET is NOT in the rename-on-collide
    # set: collisions between two mods' identically-UUID-named files
    # mean the content is byte-identical (toolkit-deterministic for
    # the same source), so verbatim copy with implicit dedup is right.
    if (name.lower().endswith(".gts") or name.lower().endswith(".gtp")
            or name.lower().endswith(".gtex")):
        return CatalogedFile(
            file_path, FileCategory.VIRTUAL_TEXTURE_ASSET, rel, rel_under_mod, bucket,
        )

    # Packed textures (DDS)
    if name.endswith(".dds") or name.endswith(".DDS"):
        return CatalogedFile(
            file_path, FileCategory.TEXTURE_DDS, rel, rel_under_mod, bucket,
        )

    # Asset import settings XML: same stem as a sibling .gr2/.tif file
    if (name.endswith(".xml") and bucket == "Public"
            and _has_sibling_binary(file_path)):
        return CatalogedFile(
            file_path, FileCategory.ASSET_IMPORT_SETTINGS, rel, rel_under_mod, bucket,
        )

    # Story: Osiris source goals
    if (bucket == "Mods" and rel_under_mod is not None
            and len(rel_under_mod.parts) >= 3
            and rel_under_mod.parts[0] == "Story"
            and rel_under_mod.parts[1] == "RawFiles"
            and rel_under_mod.parts[2] == "Goals"
            and name.endswith(".txt")):
        return CatalogedFile(
            file_path, FileCategory.STORY_GOAL, rel, rel_under_mod, bucket,
        )

    # Story: compiler-generated header / definitions
    if (bucket == "Mods" and rel_under_mod is not None
            and rel_under_mod.parts[:2] == ("Story", "RawFiles")):
        if name == "story_header.div":
            return CatalogedFile(
                file_path, FileCategory.STORY_HEADER, rel, rel_under_mod, bucket,
            )
        if name == "story_definitions.div":
            return CatalogedFile(
                file_path, FileCategory.STORY_DEFINITIONS, rel, rel_under_mod, bucket,
            )

    # Story: compiled outputs (to be discarded on merge)
    if (bucket == "Mods" and rel_under_mod is not None
            and len(rel_under_mod.parts) == 2
            and rel_under_mod.parts[0] == "Story"):
        if name in {"story.div", "story.div.osi", "goals.raw", "story_ac.dat"}:
            return CatalogedFile(
                file_path, FileCategory.STORY_COMPILED, rel, rel_under_mod, bucket,
            )
        if name == "log.txt":
            return CatalogedFile(
                file_path, FileCategory.STORY_LOG, rel, rel_under_mod, bucket,
            )
        if name == "story_orphanqueries_ignore.txt":
            return CatalogedFile(
                file_path, FileCategory.STORY_ORPHAN_IGNORE, rel, rel_under_mod, bucket,
            )
        if name == "story_orphanqueries_found.txt":
            # Also a compiler artifact; lumped with compiled for discard.
            return CatalogedFile(
                file_path, FileCategory.STORY_COMPILED, rel, rel_under_mod, bucket,
            )

    # Script Extender Lua
    if (bucket == "Mods" and rel_under_mod is not None
            and len(rel_under_mod.parts) >= 2
            and rel_under_mod.parts[0] == "Scripts"):
        if name.endswith(".lua"):
            return CatalogedFile(
                file_path, FileCategory.SE_LUA, rel, rel_under_mod, bucket,
            )
        if "config" in rel_under_mod.parts:
            return CatalogedFile(
                file_path, FileCategory.SE_LUA_CONFIG, rel, rel_under_mod, bucket,
            )

    # GUI metadata
    if (bucket == "Mods" and rel_under_mod is not None
            and len(rel_under_mod.parts) >= 2
            and rel_under_mod.parts[0] == "GUI"
            and name == "metadata.lsf"):
        return CatalogedFile(
            file_path, FileCategory.GUI_METADATA, rel, rel_under_mod, bucket,
        )

    # Minimap placeholder
    if name == "MinimapAtlas.mmxml":
        return CatalogedFile(
            file_path, FileCategory.MINIMAP, rel, rel_under_mod, bucket,
        )

    # --- Level content (Mods/<Mod>/Levels/<Region>/...) ---
    # The Toolkit writes per-region placement data as <uuid>.lsf files
    # bucketed by object type. We split out the special data files (AI
    # grid, HLOD, terrain) and treat everything else as LEVEL_CONTENT.
    if (bucket == "Mods" and rel_under_mod is not None
            and len(rel_under_mod.parts) >= 2
            and rel_under_mod.parts[0] == "Levels"):
        # Region is rel_under_mod.parts[1]; subdir (if any) is parts[2]; etc.
        subdir = rel_under_mod.parts[2] if len(rel_under_mod.parts) >= 3 else None
        # AI grid data: aigrid.data, materialgrid.data, chasms.lsf, navigationPortals.lsf
        if subdir == "Ai":
            return CatalogedFile(
                file_path, FileCategory.LEVEL_AI, rel, rel_under_mod, bucket,
            )
        # HLOD baked data: HLOD.lsf at the Levels/<Region>/ level
        if len(rel_under_mod.parts) == 3 and name.startswith("HLOD"):
            return CatalogedFile(
                file_path, FileCategory.LEVEL_HLOD, rel, rel_under_mod, bucket,
            )
        # Terrain: Levels/<Region>/Terrains/<uuid>_X.bin and ..._X_X.patch
        if subdir == "Terrains" and (name.endswith(".bin") or name.endswith(".patch")):
            return CatalogedFile(
                file_path, FileCategory.LEVEL_TERRAIN, rel, rel_under_mod, bucket,
            )
        # Everything else under Mods/<Mod>/Levels/ is placement content.
        if name.endswith(".lsf"):
            return CatalogedFile(
                file_path, FileCategory.LEVEL_CONTENT_LSF, rel, rel_under_mod, bucket,
            )
        if name.endswith(".lsx"):
            return CatalogedFile(
                file_path, FileCategory.LEVEL_CONTENT_LSX, rel, rel_under_mod, bucket,
            )

    # --- Globals (Mods/<Mod>/Globals/<Region>/<Type>/<uuid>.lsf) ---
    # "Patch" content that injects objects/triggers into BASE GAME regions
    # (WLD_Main_A, CTY_Main_A, BGO_Main_A, etc.) or the mod's own region.
    # Same shape as level content but lives under Globals/ at the Mods bucket.
    if (bucket == "Mods" and rel_under_mod is not None
            and len(rel_under_mod.parts) >= 2
            and rel_under_mod.parts[0] == "Globals"):
        if name.endswith(".lsf") or name.endswith(".lsx"):
            return CatalogedFile(
                file_path, FileCategory.GLOBALS_LSF, rel, rel_under_mod, bucket,
            )

    # --- Editor-side level metadata JSON ---
    # Editor/Mods/<Mod>/Levels/<Region>/SelectionGroups/<uuid>.json and
    # similar Toolkit-only editor state. The game doesn't read these but
    # the Toolkit references their UUIDs for editor consistency.
    if (bucket == "Editor" and rel_under_mod is not None
            and len(rel_under_mod.parts) >= 1
            and rel_under_mod.parts[0] == "Levels"
            and name.endswith(".json")):
        return CatalogedFile(
            file_path, FileCategory.EDITOR_METADATA_JSON, rel, rel_under_mod, bucket,
        )

    # --- Image assets ---
    # mod_publish_logo.png at the Mods root, thumbnail.png at the Projects
    # root, plus level thumbnails (Levels/<Region>/{banner,thumbnail,
    # templateThumbnail}.png): all are images, the merger copies them
    # verbatim with the folder-rename in the destination path.
    if name.lower().endswith((".png", ".jpg", ".jpeg")):
        return CatalogedFile(
            file_path, FileCategory.IMAGE_ASSET, rel, rel_under_mod, bucket,
        )

    # Level editor data (Editor/Mods/<Mod>/Levels/...): catch-all for files
    # under Editor-side level directories not matched by the more specific
    # rules above (e.g. .lsf scenery in editor-mirror state, .patch files).
    if (bucket == "Editor" and rel_under_mod is not None
            and len(rel_under_mod.parts) >= 1
            and rel_under_mod.parts[0] == "Levels"):
        return CatalogedFile(
            file_path, FileCategory.LEVEL_DATA, rel, rel_under_mod, bucket,
        )

    # Generic LSX/LSF not covered above
    if name.endswith(".lsx"):
        return CatalogedFile(
            file_path, FileCategory.BANK_LSX, rel, rel_under_mod, bucket,
        )

    return CatalogedFile(
        file_path, FileCategory.OTHER, rel, rel_under_mod, bucket,
    )


def _has_sibling_binary(xml_path: Path) -> bool:
    """An asset-import-settings XML sits next to a .gr2 or .tif of the same stem.
    Used to distinguish those XMLs from generic LSX/LSF metadata XMLs."""
    stem = xml_path.stem
    parent = xml_path.parent
    for ext in (".GR2", ".gr2", ".tif", ".TIF", ".tiff", ".TIFF",
                ".png", ".PNG", ".dds", ".DDS"):
        if (parent / f"{stem}{ext}").exists():
            return True
    return False
