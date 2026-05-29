"""Generate BG3 icon assets from a source PNG.

This module produces all the files a working BG3 mod actually needs to
ship an icon, grounded in inspection of three independent in-game-working
mods:

  - **Class_RogueKira** (user's own work-in-progress class)
  - **nightb / Nightbringer** (third-party class mod, Nexus)
  - **mysticw / Mystic Warden** (third-party class mod with 8 subclasses)

Where the layout differs from public tutorials, this code follows what
the working mods actually do. Where the three mods disagree, we follow
the convention shared by the two independent third-party mods, since a
single author can ship eccentricities the engine tolerates.

Key facts the inspection established:

1. **Every icon DDS lives under `Mods/<Mod>/GUI/Assets/...`**, NOT
   `Public/<Mod>/GUI/...`. The only exceptions are the 64x64 atlas
   sheet (under `Public/<Mod>/Assets/Textures/Icons/`) and the
   TextureBank `_merged.lsx` (under `Public/<Mod>/Content/UI/[PAK]_UI/`).

2. **Every icon DDS must be registered in
   `Mods/<Mod>/GUI/metadata.lsf`** for the engine to load it. The
   metadata file declares the dimensions (w/h) and mipmap setting
   (mipcount=1) for each image. An unregistered DDS doesn't load.

3. **AssetsLowRes/ files are GENUINE low-resolution copies.** Both
   third-party mods consistently store them at roughly half the Assets/
   resolution (tooltip 380→192, controller 144→72, class standard
   300→152, hotbar 144→72, portrait 152→76). Class_RogueKira used
   byte-identical copies, but that's an author shortcut, not the
   convention.

4. **AssetsLowRes/ files are NOT registered in metadata.lsf.** Both
   nightb (96 keys, 0 LowRes) and mysticw (354 keys, 0 LowRes) register
   only the Assets/ paths. The engine resolves the LowRes counterpart
   by path convention.

5. **Item tooltip icons are dual-written:** the same 380x380 DDS lives
   in BOTH `Tooltips/Icons/` and `Tooltips/ItemIcons/`. Skill-family
   icons only go in `Tooltips/Icons/`.

6. **Class icon sizes vary by author**, but the most common convention
   across third-party mods is 300x300 standard + 144x144 hotbar (the
   nightb pattern). mysticw uses 152x152 standard. We default to
   nightb's pattern since it matches the wiki's class-icon screen
   sizing guidance.

7. **Atlas UV map exists in two forms**: a full `Icons_<Mod>.lsx`
   under `Public/<Mod>/GUI/` (with both TextureAtlasInfo and IconUVList
   regions, the form the toolkit consumes) AND a stripped
   `Icons_<Mod>.lsf.lsx` under `Mods/<Mod>/GUI/` (just a `root` region
   listing IconUV entries, the form the game consumes after the
   toolkit's conversion).

This module has NO Qt dependency: pure file/image work, headlessly
testable. It uses `core.divine` to convert LSX to binary LSF for
`metadata.lsf` and `Simple_Icons.lsf`; if divine.exe isn't configured,
it falls back to writing the `.lsf.lsx` text form (which the BG3
multitool converts to .lsf at pack time).
"""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from PIL import Image

from . import lsx
from . import io_util
from . import divine as divine_mod


# ===========================================================================
# Families and types
# ===========================================================================


class IconFamily(Enum):
    ATLAS = "atlas"           # spell/skill/item/passive/status
    CLASS = "class"           # class/subclass
    PORTRAIT = "portrait"     # character portrait
    ACTION_RESOURCE = "action_resource"  # custom action resource (with states)


@dataclass(frozen=True)
class IconTypeSpec:
    """Everything that varies per selectable icon type."""
    label: str
    family: IconFamily
    # ATLAS only: which Tooltips and ControllerUIIcons subfolders to use,
    # and whether to dual-write Tooltips/ItemIcons as well.
    controller_subfolder: str = "skills_png"
    write_to_item_tooltips: bool = False


# The selectable icon types, in display order. Keyed by label.
ICON_TYPES: dict[str, IconTypeSpec] = {
    "Spell / Skill": IconTypeSpec(
        "Spell / Skill", IconFamily.ATLAS,
        controller_subfolder="skills_png",
        write_to_item_tooltips=False,
    ),
    "Passive": IconTypeSpec(
        "Passive", IconFamily.ATLAS,
        controller_subfolder="skills_png",
        write_to_item_tooltips=False,
    ),
    "Status": IconTypeSpec(
        "Status", IconFamily.ATLAS,
        controller_subfolder="skills_png",
        write_to_item_tooltips=False,
    ),
    "Item": IconTypeSpec(
        "Item", IconFamily.ATLAS,
        controller_subfolder="items_png",
        write_to_item_tooltips=True,   # dual-write Tooltips/Icons + Tooltips/ItemIcons
    ),
    "Class / Subclass": IconTypeSpec(
        "Class / Subclass", IconFamily.CLASS,
    ),
    "Action Resource": IconTypeSpec(
        "Action Resource", IconFamily.ACTION_RESOURCE,
    ),
    "Portrait": IconTypeSpec(
        "Portrait", IconFamily.PORTRAIT,
    ),
}


# --- Sizes ------------------------------------------------------------------

# ATLAS family
TOOLTIP_PX = 380
CONTROLLER_PX = 144

# Hotbar atlas geometry: 2048x2048 of 64x64 cells = 32x32 grid = 1024 slots.
ATLAS_PX = 2048
ICON_PX = 64
GRID = ATLAS_PX // ICON_PX
SLOTS = GRID * GRID
UV_STEP = ICON_PX / ATLAS_PX  # 0.03125

# CLASS family: cross-checked against nightb (third-party class mod):
#   - Standard class icon: 300x300 (Assets) → 152x152 (AssetsLowRes)
#   - Hotbar class icon:   144x144 (Assets) →  72x72 (AssetsLowRes)
# mysticw uses 152/76 for standard (smaller); 300/152 is the more
# common convention and matches the wiki guidance for "main class screen
# size", so we pick that. Hotbar size matches between mods at 144.
CLASS_PX = 300
CLASS_LOWRES_PX = 152
CLASS_HOTBAR_PX = 144
CLASS_HOTBAR_LOWRES_PX = 72

# PORTRAIT family
PORTRAIT_PX = 152
PORTRAIT_LOWRES_PX = 76  # The ONE family where AssetsLowRes is actually smaller.

# ACTION_RESOURCE family
AR_DEFAULT_PX = 80          # ActionResources_c/Icons/<name>.DDS
AR_STATE_W = 44             # Width of state variants (Resources/<state>/<name>.DDS)
AR_STATE_H = 64             # Height of state variants - they are NOT square
AR_SHARED_PX = 48           # Shared/Resources/<name>.DDS (and state variants)
AR_CC_PX = 128              # CC/icons_resources/<name>.DDS

# The four states an action resource icon comes in. The unsuffixed entry
# (just `Resources/<name>.DDS`) is the default/base variant.
AR_STATES = ["Highlight", "Missing", "Used"]


# ===========================================================================
# Public data types
# ===========================================================================


@dataclass
class IconAddResult:
    """What add_icon produced: the icon name to reference, every file
    written/updated, and a hint telling the user how to wire it in."""
    icon_name: str
    family: IconFamily
    files_written: list[Path] = field(default_factory=list)
    files_updated: list[Path] = field(default_factory=list)
    atlas_path: Path | None = None  # ATLAS family only
    slot_index: int = -1            # ATLAS family only
    reference_hint: str = ""
    notes: list[str] = field(default_factory=list)


class IconAddError(Exception):
    """Raised when an icon can't be added (bad PNG, atlas full, etc.)."""


# ===========================================================================
# Shared helpers
# ===========================================================================


def _ensure_parent(path: Path) -> None:
    """mkdir -p the parent dir, long-path-safe on Windows."""
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        import os
        os.makedirs(io_util.to_long_path(parent), exist_ok=True)


def _load_png(png_path: Path) -> Image.Image:
    """Load source PNG as RGBA. Raises IconAddError on unreadable input."""
    try:
        img = Image.open(png_path)
        img.load()
    except Exception as e:
        raise IconAddError(f"Couldn't read PNG: {png_path} ({e})") from e
    return img.convert("RGBA")


def _resize_to(img: Image.Image, w: int, h: int) -> Image.Image:
    """LANCZOS resize for crisp downscaling."""
    return img.resize((w, h), Image.LANCZOS)


def _write_dds_at_size(img: Image.Image, w: int, h: int, dest: Path) -> None:
    """Resize and save as DXT5/BC3 DDS (no mipmaps)."""
    resized = _resize_to(img, w, h)
    _ensure_parent(dest)
    try:
        resized.save(
            str(io_util.to_long_path(dest)),
            format="DDS", pixel_format="DXT5",
        )
    except Exception as e:
        raise IconAddError(
            f"Failed to write DDS {dest.name}: {e}. "
            f"(Pillow may not support DXT5 DDS in this build.)"
        ) from e


def _lowres_size(full_w: int, full_h: int) -> tuple[int, int]:
    """Pick the AssetsLowRes resolution for a given full-resolution pair.

    Third-party class mods (nightb, mysticw) consistently store
    AssetsLowRes copies at roughly half the Assets resolution. The exact
    pairing isn't always /2 — both real mods round tooltip 380 to 192
    (not 190), which matches BG3's vanilla low-res atlas conventions.
    For everything else, exact half. For odd numbers (e.g. 144/2=72,
    300/2=150) the integer division is fine.
    """
    if full_w == full_h == TOOLTIP_PX:
        return 192, 192
    return full_w // 2, full_h // 2


def _write_dds_pair(
    img: Image.Image, w: int, h: int,
    full_path: Path, lowres_path: Path,
    result: IconAddResult,
) -> tuple[Path, Path]:
    """Write a DDS to ``Assets/`` at full size and a downscaled copy to
    ``AssetsLowRes/`` at half resolution.

    Per cross-checking nightb and mysticw (two independent third-party
    class mods on Nexus), AssetsLowRes/ files are genuinely
    lower-resolution — typically half the Assets/ dimensions. The
    earlier Class_RogueKira observation of byte-identical copies was an
    author-specific shortcut, not the convention.
    """
    _write_dds_at_size(img, w, h, full_path)
    lr_w, lr_h = _lowres_size(w, h)
    _write_dds_at_size(img, lr_w, lr_h, lowres_path)
    result.files_written.append(full_path)
    result.files_written.append(lowres_path)
    return full_path, lowres_path


def _validate_icon_name(icon_name: str) -> str:
    """Strip and reject names with characters that would break filesystem
    or LSX. Allows hyphens, underscores, parens (portrait override names
    need those)."""
    icon_name = icon_name.strip()
    if not icon_name:
        raise IconAddError("Icon name can't be empty.")
    if any(c in icon_name for c in '\\/:*?"<>| '):
        raise IconAddError(
            "Icon name can't contain spaces or any of \\ / : * ? \" < > |."
        )
    return icon_name


# ===========================================================================
# metadata.lsf registry (Mods/<Mod>/GUI/metadata.lsf)
# ===========================================================================
#
# Structure (from Fade's example + the working class mod's binary):
#
#   <region id="config">
#     <node id="config">
#       <children>
#         <node id="entries">
#           <children>
#             <node id="Object">
#               <attribute id="MapKey" value="Assets/Tooltips/Icons/Foo.png" />
#               <children>
#                 <node id="entries">
#                   <attribute id="h" type="int16" value="380" />
#                   <attribute id="mipcount" type="int8" value="1" />
#                   <attribute id="w" type="int16" value="380" />
#                 </node>
#               </children>
#             </node>
#             ... one Object per registered image ...
#           </children>
#         </node>
#       </children>
#     </node>
#   </region>
#
# MapKey is the path relative to Mods/<Mod>/GUI/, with the .png
# extension regardless of the on-disk .DDS. We accumulate all entries
# for a single add_icon call and write them in one batched pass.


def _metadata_lsf_path(data_root: Path, mod_folder: str) -> Path:
    return data_root / "Mods" / mod_folder / "GUI" / "metadata.lsf"


def _metadata_lsx_path(data_root: Path, mod_folder: str) -> Path:
    """The text-form fallback when divine.exe isn't available."""
    return data_root / "Mods" / mod_folder / "GUI" / "metadata.lsf.lsx"


def _new_metadata_document() -> lsx.LsxDocument:
    xml = """<?xml version="1.0" encoding="utf-8"?>
<save>
    <version major="4" minor="8" revision="0" build="200" lslib_meta="v1,bswap_guids" />
    <region id="config">
        <node id="config">
            <children>
                <node id="entries">
                    <children>
                    </children>
                </node>
            </children>
        </node>
    </region>
</save>
"""
    return lsx.parse_bytes(xml.encode("utf-8"))


def _build_metadata_entry(map_key: str, width: int, height: int) -> lsx.Node:
    inner = lsx.Node(
        id="entries",
        attributes=[
            lsx.Attribute(id="h", type="int16", value=str(height)),
            lsx.Attribute(id="mipcount", type="int8", value="1"),
            lsx.Attribute(id="w", type="int16", value=str(width)),
        ],
    )
    return lsx.Node(
        id="Object",
        attributes=[lsx.Attribute(id="MapKey", type="FixedString", value=map_key)],
        children=[inner],
    )


def _find_metadata_entries_container(doc: lsx.LsxDocument) -> lsx.Node | None:
    region = doc.region("config")
    if region is None:
        return None
    for child in region.root_node.children:
        if child.id == "entries":
            return child
    return None


def _upsert_metadata_entries(
    doc: lsx.LsxDocument,
    entries: list[tuple[str, int, int]],  # (map_key, w, h)
) -> None:
    container = _find_metadata_entries_container(doc)
    if container is None:
        raise IconAddError(
            "GUI/metadata.lsf has unexpected shape (no config>entries node)."
        )
    # Drop any prior entries for the same MapKeys (idempotent re-add).
    map_keys = {mk for mk, _, _ in entries}
    container.children = [
        c for c in container.children
        if not (c.id == "Object" and c.attr_value("MapKey") in map_keys)
    ]
    for mk, w, h in entries:
        container.children.append(_build_metadata_entry(mk, w, h))


def _read_existing_metadata(
    data_root: Path, mod_folder: str, divine_path: str | None,
) -> tuple[lsx.LsxDocument, Path | None]:
    """Return (doc, source_path_if_existed). Prefers the .lsx text form
    if present; otherwise reads .lsf via divine. Returns a fresh empty
    document if neither exists."""
    lsx_path = _metadata_lsx_path(data_root, mod_folder)
    lsf_path = _metadata_lsf_path(data_root, mod_folder)

    if lsx_path.exists():
        return lsx.parse_file(lsx_path), lsx_path

    if lsf_path.exists():
        try:
            divine_exe = divine_mod.find_divine(divine_path)
        except divine_mod.DivineNotFoundError:
            raise IconAddError(
                f"A binary GUI/metadata.lsf exists at {lsf_path} but "
                f"divine.exe isn't configured, so the tool can't read it. "
                f"Configure divine.exe in Settings, or convert it to "
                f"metadata.lsf.lsx manually before adding more icons."
            )
        d = divine_mod.Divine(exe_path=divine_exe)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".lsx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            d.lsf_to_lsx(lsf_path, tmp_path)
            return lsx.parse_file(tmp_path), lsf_path
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return _new_metadata_document(), None


def _write_metadata(
    doc: lsx.LsxDocument, data_root: Path, mod_folder: str,
    divine_path: str | None,
) -> tuple[Path, bool]:
    """Write metadata.lsf binary if divine is available, else
    metadata.lsf.lsx text. Returns (path_written, wrote_binary)."""
    lsx_path = _metadata_lsx_path(data_root, mod_folder)
    lsf_path = _metadata_lsf_path(data_root, mod_folder)

    try:
        divine_exe = divine_mod.find_divine(divine_path)
    except divine_mod.DivineNotFoundError:
        divine_exe = None

    if divine_exe is not None:
        import tempfile
        _ensure_parent(lsf_path)
        with tempfile.NamedTemporaryFile(suffix=".lsx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            lsx.write_file(doc, tmp_path)
            d = divine_mod.Divine(exe_path=divine_exe)
            d.lsx_to_lsf(tmp_path, lsf_path)
        except divine_mod.DivineError as e:
            raise IconAddError(
                f"divine.exe failed converting GUI metadata to .lsf: {e}"
            ) from e
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        try:
            if lsx_path.exists():
                lsx_path.unlink()
        except OSError:
            pass
        return lsf_path, True

    _ensure_parent(lsx_path)
    lsx.write_file(doc, lsx_path)
    return lsx_path, False


def _register_in_metadata(
    data_root: Path, mod_folder: str,
    entries: list[tuple[str, int, int]],
    divine_path: str | None,
    result: IconAddResult,
) -> None:
    """Read-modify-write metadata.lsf with a batch of new entries."""
    if not entries:
        return
    doc, existing_source = _read_existing_metadata(data_root, mod_folder, divine_path)
    _upsert_metadata_entries(doc, entries)
    out_path, wrote_binary = _write_metadata(doc, data_root, mod_folder, divine_path)
    (result.files_updated if existing_source is not None else result.files_written).append(out_path)
    if not wrote_binary:
        result.notes.append(
            "GUI/metadata.lsf.lsx written as text fallback (divine.exe "
            "not configured). The BG3 multitool will convert it at pack "
            "time. To get the binary directly, set divine.exe in Settings."
        )


def _gui_relative_png(asset_root: str, parts: list[str], icon_name: str) -> str:
    """Build a metadata MapKey: path under GUI/, .png extension regardless
    of the on-disk .DDS. ``asset_root`` is 'Assets' or 'AssetsLowRes'."""
    return "/".join([asset_root, *parts, f"{icon_name}.png"])


# ===========================================================================
# ATLAS family (Spell/Skill/Item/Passive/Status)
# ===========================================================================


def _atlas_dds_path(data_root: Path, mod_folder: str) -> Path:
    """The 64-cell sheet itself - stays under Public/."""
    return (data_root / "Public" / mod_folder / "Assets" / "Textures"
            / "Icons" / f"Icons_{mod_folder}.DDS")


def _atlas_uv_lsx_path(data_root: Path, mod_folder: str) -> Path:
    """Full UV map (TextureAtlasInfo + IconUVList) - Public side, .lsx form
    the toolkit reads."""
    return data_root / "Public" / mod_folder / "GUI" / f"Icons_{mod_folder}.lsx"


def _atlas_uv_lsf_path(data_root: Path, mod_folder: str) -> Path:
    """Simpler UV map (just IconUVList) - Mods side, .lsf/.lsf.lsx form
    the game reads."""
    return data_root / "Mods" / mod_folder / "GUI" / f"Icons_{mod_folder}.lsf"


def _texturebank_lsx_path(data_root: Path, mod_folder: str) -> Path:
    """TextureBank entry that registers the atlas - Public side."""
    return (data_root / "Public" / mod_folder / "Content" / "UI"
            / "[PAK]_UI" / "_merged.lsf.lsx")


def _slot_to_pixel(slot: int) -> tuple[int, int]:
    row, col = divmod(slot, GRID)
    return col * ICON_PX, row * ICON_PX


def _slot_to_uv(slot: int) -> tuple[float, float, float, float]:
    row, col = divmod(slot, GRID)
    u1 = col * UV_STEP
    v1 = row * UV_STEP
    return u1, v1, u1 + UV_STEP, v1 + UV_STEP


def _build_uv_node(icon_name: str, slot: int) -> lsx.Node:
    u1, v1, u2, v2 = _slot_to_uv(slot)
    return lsx.Node(
        id="IconUV",
        attributes=[
            lsx.Attribute(id="MapKey", type="FixedString", value=icon_name),
            lsx.Attribute(id="U1", type="float", value=repr(u1)),
            lsx.Attribute(id="U2", type="float", value=repr(u2)),
            lsx.Attribute(id="V1", type="float", value=repr(v1)),
            lsx.Attribute(id="V2", type="float", value=repr(v2)),
        ],
    )


def _new_full_uv_lsx(mod_folder: str, atlas_uuid: str) -> lsx.LsxDocument:
    """The Public-side Simple_Icons.lsx (full atlas-info + UV list)."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<save>
    <version major="4" minor="0" revision="6" build="5" />
    <region id="TextureAtlasInfo">
        <node id="root">
            <children>
                <node id="TextureAtlasIconSize">
                    <attribute id="Height" type="int64" value="{ICON_PX}"/>
                    <attribute id="Width" type="int64" value="{ICON_PX}"/>
                </node>
                <node id="TextureAtlasPath">
                    <attribute id="Path" type="LSString" value="Public/{mod_folder}/Assets/Textures/Icons/Icons_{mod_folder}.DDS"/>
                    <attribute id="UUID" type="FixedString" value="{atlas_uuid}"/>
                </node>
                <node id="TextureAtlasTextureSize">
                    <attribute id="Height" type="int64" value="{ATLAS_PX}"/>
                    <attribute id="Width" type="int64" value="{ATLAS_PX}"/>
                </node>
            </children>
        </node>
    </region>
    <region id="IconUVList">
        <node id="root">
            <children>
            </children>
        </node>
    </region>
</save>
"""
    return lsx.parse_bytes(xml.encode("utf-8"))


def _new_short_uv_lsx() -> lsx.LsxDocument:
    """The Mods-side Simple_Icons.lsf.lsx (just an IconUV list under
    `region id='root'`). Matches the simpler shape the game reads."""
    xml = """<?xml version="1.0" encoding="utf-8"?>
<save>
    <version major="4" minor="0" revision="6" build="5" lslib_meta="v1,bswap_guids" />
    <region id="root">
        <node id="root">
            <children>
            </children>
        </node>
    </region>
</save>
"""
    return lsx.parse_bytes(xml.encode("utf-8"))


def _new_texturebank_lsx(mod_folder: str, atlas_uuid: str) -> lsx.LsxDocument:
    source = f"Public/{mod_folder}/Assets/Textures/Icons/Icons_{mod_folder}.DDS"
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<save>
    <version major="4" minor="0" revision="6" build="5" lslib_meta="v1,bswap_guids" />
    <region id="TextureBank">
        <node id="TextureBank">
            <children>
                <node id="Resource">
                    <attribute id="ID" type="FixedString" value="{atlas_uuid}" />
                    <attribute id="Localized" type="bool" value="False" />
                    <attribute id="Name" type="LSString" value="Icons_{mod_folder}" />
                    <attribute id="SRGB" type="bool" value="True" />
                    <attribute id="SourceFile" type="LSString" value="{source}" />
                    <attribute id="Streaming" type="bool" value="True" />
                    <attribute id="Template" type="FixedString" value="Icons_Items" />
                    <attribute id="Type" type="int64" value="0" />
                    <attribute id="_OriginalFileVersion_" type="int64" value="144115188075855912" />
                </node>
            </children>
        </node>
    </region>
</save>
"""
    return lsx.parse_bytes(xml.encode("utf-8"))


def _iter_nodes(node: lsx.Node):
    """Depth-first iterate a node and all descendants."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _read_existing_full_uv(uv_path: Path) -> tuple[lsx.LsxDocument | None, dict[str, int], set[int], str | None]:
    """Return (doc, name->slot map, used slots, atlas_uuid)."""
    if not uv_path.exists():
        return None, {}, set(), None
    doc = lsx.parse_file(uv_path)
    name_to_slot: dict[str, int] = {}
    used: set[int] = set()
    atlas_uuid: str | None = None

    info = doc.region("TextureAtlasInfo")
    if info is not None:
        for node in _iter_nodes(info.root_node):
            if node.id == "TextureAtlasPath":
                atlas_uuid = node.attr_value("UUID")

    uvlist = doc.region("IconUVList")
    if uvlist is not None:
        for node in _iter_nodes(uvlist.root_node):
            if node.id != "IconUV":
                continue
            key = node.attr_value("MapKey")
            u1, v1 = node.attr_value("U1"), node.attr_value("V1")
            if u1 is None or v1 is None:
                continue
            try:
                col = round(float(u1) / UV_STEP)
                row = round(float(v1) / UV_STEP)
            except ValueError:
                continue
            if 0 <= row < GRID and 0 <= col < GRID:
                slot = row * GRID + col
                used.add(slot)
                if key:
                    name_to_slot[key] = slot
    return doc, name_to_slot, used, atlas_uuid


def _next_free_slot(used: set[int]) -> int:
    for i in range(SLOTS):
        if i not in used:
            return i
    raise IconAddError(
        f"The icon atlas is full ({SLOTS} icons). Consider a second atlas."
    )


def _append_uv_node_in_region(doc: lsx.LsxDocument, region_id: str, icon_name: str, slot: int) -> None:
    region = doc.region(region_id)
    if region is None:
        raise IconAddError(f"UV doc missing region {region_id!r}.")
    root = region.root_node
    root.children = [
        c for c in root.children
        if not (c.id == "IconUV" and c.attr_value("MapKey") == icon_name)
    ]
    root.children.append(_build_uv_node(icon_name, slot))


def _load_or_create_atlas(atlas_path: Path) -> Image.Image:
    if atlas_path.exists():
        try:
            existing = Image.open(io_util.to_long_path(atlas_path))
            existing.load()
            return existing.convert("RGBA")
        except Exception as e:
            raise IconAddError(
                f"An icon atlas exists at {atlas_path} but couldn't be read "
                f"({e}). Refusing to overwrite it."
            ) from e
    return Image.new("RGBA", (ATLAS_PX, ATLAS_PX), (0, 0, 0, 0))


def _write_short_uv_lsf(
    data_root: Path, mod_folder: str, icon_name: str, slot: int,
    divine_path: str | None, result: IconAddResult,
) -> None:
    """Maintain Mods/<Mod>/GUI/Simple_Icons.lsf (binary, via divine) or
    .lsf.lsx fallback. The Mods-side form is the simpler `region id='root'`
    layout the game reads at runtime."""
    lsf_path = _atlas_uv_lsf_path(data_root, mod_folder)
    lsx_fallback = lsf_path.with_suffix(".lsf.lsx")  # Mods/.../Simple_Icons.lsf.lsx

    # Read existing if present.
    doc: lsx.LsxDocument | None = None
    if lsx_fallback.exists():
        doc = lsx.parse_file(lsx_fallback)
    else:
        try:
            divine_exe = divine_mod.find_divine(divine_path)
        except divine_mod.DivineNotFoundError:
            divine_exe = None
        if lsf_path.exists() and divine_exe is not None:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".lsx", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                d = divine_mod.Divine(exe_path=divine_exe)
                d.lsf_to_lsx(lsf_path, tmp_path)
                doc = lsx.parse_file(tmp_path)
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
    if doc is None:
        doc = _new_short_uv_lsx()

    # Append the entry (the short form's region is named "root", not "IconUVList").
    _append_uv_node_in_region(doc, "root", icon_name, slot)

    # Write back.
    try:
        divine_exe = divine_mod.find_divine(divine_path)
    except divine_mod.DivineNotFoundError:
        divine_exe = None

    if divine_exe is not None:
        import tempfile
        _ensure_parent(lsf_path)
        with tempfile.NamedTemporaryFile(suffix=".lsx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            lsx.write_file(doc, tmp_path)
            d = divine_mod.Divine(exe_path=divine_exe)
            d.lsx_to_lsf(tmp_path, lsf_path)
        except divine_mod.DivineError as e:
            raise IconAddError(f"divine.exe failed converting Simple_Icons to .lsf: {e}") from e
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        # Clean up any stale text fallback.
        try:
            if lsx_fallback.exists():
                lsx_fallback.unlink()
        except OSError:
            pass
        (result.files_updated if lsf_path.exists() else result.files_written).append(lsf_path)
    else:
        _ensure_parent(lsx_fallback)
        lsx.write_file(doc, lsx_fallback)
        (result.files_updated if lsx_fallback.exists() else result.files_written).append(lsx_fallback)


def _add_atlas_icon(
    data_root: Path, mod_folder: str, icon_name: str, spec: IconTypeSpec,
    src: Image.Image, result: IconAddResult,
    divine_path: str | None,
) -> None:
    metadata_entries: list[tuple[str, int, int]] = []
    # Cross-check note: nightb and mysticw register ONLY the Assets/
    # paths in metadata.lsf, not the AssetsLowRes/ ones. So we follow
    # that convention - LowRes DDS files exist on disk but aren't
    # registered. (The Assets/ entries register the full-resolution
    # dimensions; the engine resolves the LowRes counterpart by path
    # convention.)

    # --- 1. Tooltip (380): Mods/<Mod>/GUI/Assets/Tooltips/Icons/<name>.DDS ---
    tooltip_base = data_root / "Mods" / mod_folder / "GUI" / "Assets" / "Tooltips" / "Icons"
    tooltip_lowres = data_root / "Mods" / mod_folder / "GUI" / "AssetsLowRes" / "Tooltips" / "Icons"
    _write_dds_pair(
        src, TOOLTIP_PX, TOOLTIP_PX,
        tooltip_base / f"{icon_name}.DDS",
        tooltip_lowres / f"{icon_name}.DDS",
        result,
    )
    metadata_entries.append(
        (_gui_relative_png("Assets", ["Tooltips", "Icons"], icon_name), TOOLTIP_PX, TOOLTIP_PX)
    )

    # --- 1b. Item tooltip dual-write: Tooltips/ItemIcons/<name>.DDS ---
    # Real working mods dual-write items into BOTH Tooltips/Icons AND
    # Tooltips/ItemIcons. We do the same for items.
    if spec.write_to_item_tooltips:
        item_base = data_root / "Mods" / mod_folder / "GUI" / "Assets" / "Tooltips" / "ItemIcons"
        item_lowres = data_root / "Mods" / mod_folder / "GUI" / "AssetsLowRes" / "Tooltips" / "ItemIcons"
        _write_dds_pair(
            src, TOOLTIP_PX, TOOLTIP_PX,
            item_base / f"{icon_name}.DDS",
            item_lowres / f"{icon_name}.DDS",
            result,
        )
        metadata_entries.append(
            (_gui_relative_png("Assets", ["Tooltips", "ItemIcons"], icon_name), TOOLTIP_PX, TOOLTIP_PX)
        )

    # --- 2. Controller (144) ---
    ctl_base = data_root / "Mods" / mod_folder / "GUI" / "Assets" / "ControllerUIIcons" / spec.controller_subfolder
    ctl_lowres = data_root / "Mods" / mod_folder / "GUI" / "AssetsLowRes" / "ControllerUIIcons" / spec.controller_subfolder
    _write_dds_pair(
        src, CONTROLLER_PX, CONTROLLER_PX,
        ctl_base / f"{icon_name}.DDS",
        ctl_lowres / f"{icon_name}.DDS",
        result,
    )
    metadata_entries.append(
        (_gui_relative_png("Assets", ["ControllerUIIcons", spec.controller_subfolder], icon_name), CONTROLLER_PX, CONTROLLER_PX)
    )

    # --- 3. Hotbar atlas (64): Public/<Mod>/Assets/Textures/Icons/Icons_<Mod>.DDS ---
    atlas_path = _atlas_dds_path(data_root, mod_folder)
    full_uv_path = _atlas_uv_lsx_path(data_root, mod_folder)
    tb_path = _texturebank_lsx_path(data_root, mod_folder)
    result.atlas_path = atlas_path

    atlas_existed = atlas_path.exists()
    uv_doc, name_to_slot, used_slots, atlas_uuid = _read_existing_full_uv(full_uv_path)

    if icon_name in name_to_slot:
        slot = name_to_slot[icon_name]
        result.notes.append(
            f"Icon {icon_name!r} already in the atlas; updating in place (slot {slot})."
        )
    else:
        slot = _next_free_slot(used_slots)
    result.slot_index = slot

    if not atlas_uuid:
        atlas_uuid = str(uuid_mod.uuid4())

    atlas_img = _load_or_create_atlas(atlas_path)
    icon64 = src.resize((ICON_PX, ICON_PX), Image.LANCZOS)
    px, py = _slot_to_pixel(slot)
    # Clear the cell first so re-adds don't blend with old pixels.
    atlas_img.paste((0, 0, 0, 0), (px, py, px + ICON_PX, py + ICON_PX))
    atlas_img.paste(icon64, (px, py), icon64)
    _ensure_parent(atlas_path)
    try:
        atlas_img.save(
            str(io_util.to_long_path(atlas_path)),
            format="DDS", pixel_format="DXT5",
        )
    except Exception as e:
        raise IconAddError(f"Failed to write atlas DDS: {e}") from e
    (result.files_updated if atlas_existed else result.files_written).append(atlas_path)

    # --- 4. Full UV map (Public side, .lsx) ---
    uv_existed = full_uv_path.exists()
    if uv_doc is None:
        uv_doc = _new_full_uv_lsx(mod_folder, atlas_uuid)
    _append_uv_node_in_region(uv_doc, "IconUVList", icon_name, slot)
    _ensure_parent(full_uv_path)
    lsx.write_file(uv_doc, full_uv_path)
    (result.files_updated if uv_existed else result.files_written).append(full_uv_path)

    # --- 5. Short UV map (Mods side, .lsf or .lsf.lsx) ---
    _write_short_uv_lsf(data_root, mod_folder, icon_name, slot, divine_path, result)

    # --- 6. TextureBank entry (Public side, .lsf.lsx) ---
    if not tb_path.exists():
        tb_doc = _new_texturebank_lsx(mod_folder, atlas_uuid)
        _ensure_parent(tb_path)
        lsx.write_file(tb_doc, tb_path)
        result.files_written.append(tb_path)
    else:
        result.notes.append(
            "TextureBank _merged.lsf.lsx already exists; left unchanged "
            "(assumed to already register this atlas)."
        )

    # --- 7. Register all the Mods/-side files in metadata.lsf ---
    _register_in_metadata(data_root, mod_folder, metadata_entries, divine_path, result)

    result.reference_hint = (
        f'Reference this icon as:  data "Icon" "{icon_name}"  in the '
        f"relevant stats file."
    )


# ===========================================================================
# CLASS family (Class/Subclass)
# ===========================================================================


def _add_class_icon(
    data_root: Path, mod_folder: str, icon_name: str, spec: IconTypeSpec,
    src: Image.Image, result: IconAddResult,
    divine_path: str | None,
) -> None:
    """Class/subclass icon.

    Cross-checked against nightb and mysticw (third-party class mods):
      - Standard class icon at Assets/ClassIcons/<Name>.DDS - 300x300
        (nightb). mysticw uses 152x152, so author convention varies;
        300 is also what Class_RogueKira used. We use 300 as the default.
      - Hotbar icon at Assets/ClassIcons/hotbar/<Name>.DDS - 144x144
        (nightb's convention). NOT the same size as the standard icon
        - that was the Class_RogueKira anomaly. Real third-party mods
        do shrink the hotbar version.
      - AssetsLowRes copies at roughly half: 152x152 standard, 72x72 hotbar.
      - ONLY the Assets/ paths register in metadata.lsf; AssetsLowRes/
        is on disk but not registered.
    """
    gui = data_root / "Mods" / mod_folder / "GUI"

    # (full_path, lowres_path, full_size, lowres_size, metadata_parts)
    file_specs = [
        # Standard class icon: 300 → 152
        (gui / "Assets" / "ClassIcons" / f"{icon_name}.DDS",
         gui / "AssetsLowRes" / "ClassIcons" / f"{icon_name}.DDS",
         CLASS_PX, CLASS_LOWRES_PX, ["ClassIcons"]),
        # Hotbar class icon: 144 → 72 (matches nightb)
        (gui / "Assets" / "ClassIcons" / "hotbar" / f"{icon_name}.DDS",
         gui / "AssetsLowRes" / "ClassIcons" / "hotbar" / f"{icon_name}.DDS",
         CLASS_HOTBAR_PX, CLASS_HOTBAR_LOWRES_PX, ["ClassIcons", "hotbar"]),
    ]

    metadata_entries: list[tuple[str, int, int]] = []
    for full_path, lr_path, full_sz, lr_sz, parts in file_specs:
        _write_dds_at_size(src, full_sz, full_sz, full_path)
        _write_dds_at_size(src, lr_sz, lr_sz, lr_path)
        result.files_written.append(full_path)
        result.files_written.append(lr_path)
        # Only the Assets/ entry registers in metadata.lsf (per nightb/mysticw).
        metadata_entries.append(
            (_gui_relative_png("Assets", parts, icon_name), full_sz, full_sz)
        )

    _register_in_metadata(data_root, mod_folder, metadata_entries, divine_path, result)

    result.reference_hint = (
        f"Class/subclass icons are matched by the class's internal name. "
        f"Name this icon to match your ClassDescription's Name "
        f"(currently {icon_name!r})."
    )


# ===========================================================================
# ACTION_RESOURCE family
# ===========================================================================


def _add_action_resource_icon(
    data_root: Path, mod_folder: str, icon_name: str, spec: IconTypeSpec,
    src: Image.Image, result: IconAddResult,
    divine_path: str | None,
) -> None:
    """Custom Action Resource icon set. Real working mods write a quite
    elaborate set: a default DDS, four state variants (default + 3
    states), Shared/Resources copies (also stated), and a CC copy.
    AssetsLowRes mirrors all of it.

    Sizes from the working-mod inspection:
      - ActionResources_c/Icons/<name>.DDS                      80x80
      - ActionResources_c/Icons/Resources/<name>.DDS            44x64
      - ActionResources_c/Icons/Resources/Highlight/<name>.DDS  44x64
      - ActionResources_c/Icons/Resources/Missing/<name>.DDS    44x64
      - ActionResources_c/Icons/Resources/Used/<name>.DDS       44x64
      - Shared/Resources/<name>.DDS                             48x48
      - Shared/Resources/Highlight/<name>.DDS                   48x48
      - Shared/Resources/Missing/<name>.DDS                     48x48
      - Shared/Resources/Used/<name>.DDS                        48x48
      - CC/icons_resources/<name>.DDS                           128x128
    Each is mirrored under AssetsLowRes/ at the same size.

    Note: 44x64 is NOT square. This is intentional - the action resource
    state icons are bottles/pips that read taller-than-wide.
    """
    gui = data_root / "Mods" / mod_folder / "GUI"

    # Compose the writes as (relative_parts, width, height) tuples.
    writes: list[tuple[list[str], int, int]] = [
        (["ActionResources_c", "Icons"], AR_DEFAULT_PX, AR_DEFAULT_PX),
        (["ActionResources_c", "Icons", "Resources"], AR_STATE_W, AR_STATE_H),
        *[(["ActionResources_c", "Icons", "Resources", st], AR_STATE_W, AR_STATE_H) for st in AR_STATES],
        (["Shared", "Resources"], AR_SHARED_PX, AR_SHARED_PX),
        *[(["Shared", "Resources", st], AR_SHARED_PX, AR_SHARED_PX) for st in AR_STATES],
        (["CC", "icons_resources"], AR_CC_PX, AR_CC_PX),
    ]

    metadata_entries: list[tuple[str, int, int]] = []

    for parts, w, h in writes:
        full = gui / "Assets" / Path(*parts) / f"{icon_name}.DDS"
        lowres = gui / "AssetsLowRes" / Path(*parts) / f"{icon_name}.DDS"
        # Write Assets/ at full size; AssetsLowRes/ at half (per
        # nightb/mysticw convention). For very small base sizes
        # (44x64 state variants) halving lands at 22x32 which is the
        # smallest variant the engine sees - still valid.
        _write_dds_at_size(src, w, h, full)
        lr_w, lr_h = _lowres_size(w, h)
        _write_dds_at_size(src, lr_w, lr_h, lowres)
        result.files_written.append(full)
        result.files_written.append(lowres)
        # Only the Assets/ entry registers in metadata.lsf (per nightb/mysticw).
        metadata_entries.append((_gui_relative_png("Assets", parts, icon_name), w, h))

    _register_in_metadata(data_root, mod_folder, metadata_entries, divine_path, result)

    result.reference_hint = (
        f"Action Resource icon set generated. The resource is referenced "
        f"by the Name attribute in your ActionResourceDefinitions.lsx "
        f"(should match {icon_name!r}). The 4-state set (default + "
        f"Highlight/Missing/Used) drives the panel display; the Shared "
        f"and CC copies are required for the resource to appear in the "
        f"character sheet and CC screens."
    )
    result.notes.append(
        "Action Resource icons traditionally use bottle/pip art with a "
        "vertical aspect (the state variants are 44x64, not square)."
    )


# ===========================================================================
# PORTRAIT family
# ===========================================================================


def _add_portrait_icon(
    data_root: Path, mod_folder: str, icon_name: str, spec: IconTypeSpec,
    src: Image.Image, result: IconAddResult,
    divine_path: str | None,
) -> None:
    """Character portrait: a 152x152 DDS at GUI/Assets/Portraits and a
    76x76 low-res copy at GUI/AssetsLowRes/Portraits. THE ONLY family
    where the AssetsLowRes copy is actually a different (smaller) size.
    Both registered in metadata.lsf.

    Two distinct use cases:
    - **Portrait in your own mod**: any clean name works; the metadata
      entry tells the engine the dimensions.
    - **Override a base-game NPC's portrait**: the filename must match
      the target character's existing portrait exactly (typically a
      GUID-prefixed name from their root template Icon attribute), and
      the file must live under the target's mod folder (commonly
      GustavDev), not your own.
    """
    gui = data_root / "Mods" / mod_folder / "GUI"
    full = gui / "Assets" / "Portraits" / f"{icon_name}.DDS"
    lowres = gui / "AssetsLowRes" / "Portraits" / f"{icon_name}.DDS"

    # Portrait is the ONE family where the LowRes copy is genuinely
    # smaller (76x76 vs 152x152), so we encode each at its own size
    # rather than write-and-copy.
    _write_dds_at_size(src, PORTRAIT_PX, PORTRAIT_PX, full)
    _write_dds_at_size(src, PORTRAIT_LOWRES_PX, PORTRAIT_LOWRES_PX, lowres)
    result.files_written.append(full)
    result.files_written.append(lowres)

    # Per cross-checking: third-party mods register ONLY the Assets/
    # path in metadata.lsf, not the AssetsLowRes/ one. Fade's original
    # example metadata.lsx also showed only the Assets/Portraits entry.
    metadata_entries = [
        (_gui_relative_png("Assets", ["Portraits"], icon_name), PORTRAIT_PX, PORTRAIT_PX),
    ]
    _register_in_metadata(data_root, mod_folder, metadata_entries, divine_path, result)

    result.reference_hint = (
        "Portrait file is in Mods/<Mod>/GUI/Assets/Portraits/ and "
        f"registered in metadata.lsf. Any clean name works for portraits "
        f"in your own mod. To OVERRIDE a base-game NPC's portrait, the "
        f"filename must match the target character's existing portrait "
        f"exactly (often a GUID-prefixed name like "
        f"'<uuid>-(Icon_<...>)' from their root template Icon attribute), "
        f"and the mod folder must be the character's home folder "
        f"(often GustavDev)."
    )


# ===========================================================================
# Public entry point
# ===========================================================================


def add_icon(
    data_root: Path,
    mod_folder: str,
    icon_name: str,
    icon_type: str,
    png_path: Path,
    divine_path: str | None = None,
) -> IconAddResult:
    """Add one icon to a mod from a source PNG.

    Args:
        data_root: directory containing Mods/<mod_folder>/ and
            Public/<mod_folder>/.
        mod_folder: the mod's folder name.
        icon_name: how the icon is referenced (stat Icon value for atlas
            family; class/race/resource internal name for the rest).
            Must be filesystem-safe (no spaces or special chars except
            hyphens, underscores, parens).
        icon_type: one of ICON_TYPES' keys.
        png_path: the source PNG (ideally large and square).
        divine_path: path to divine.exe, used to write binary .lsf files
            (metadata.lsf, Simple_Icons.lsf). If None or unconfigured,
            falls back to .lsf.lsx text forms.

    Returns an IconAddResult. Raises IconAddError on any problem.
    """
    icon_name = _validate_icon_name(icon_name)
    if icon_type not in ICON_TYPES:
        raise IconAddError(
            f"Unknown icon type {icon_type!r}. "
            f"Expected one of: {', '.join(ICON_TYPES)}."
        )
    spec = ICON_TYPES[icon_type]

    png_path = Path(png_path)
    if not png_path.is_file():
        raise IconAddError(f"PNG not found: {png_path}")
    src = _load_png(png_path)

    # Warn if source is smaller than the biggest target size for this
    # family. Doesn't fail - upscaling just softens the result.
    biggest = {
        IconFamily.ATLAS: TOOLTIP_PX,
        IconFamily.CLASS: CLASS_PX,
        IconFamily.PORTRAIT: PORTRAIT_PX,
        IconFamily.ACTION_RESOURCE: AR_CC_PX,
    }[spec.family]

    result = IconAddResult(icon_name=icon_name, family=spec.family)

    if src.width < biggest or src.height < biggest:
        result.notes.append(
            f"Source PNG is {src.width}x{src.height}, smaller than the "
            f"{biggest}x{biggest} target; it will be upscaled and may look soft."
        )
    if src.width != src.height:
        result.notes.append(
            f"Source PNG isn't square ({src.width}x{src.height}); it will "
            f"be stretched to square where applicable."
        )

    if spec.family is IconFamily.ATLAS:
        _add_atlas_icon(data_root, mod_folder, icon_name, spec, src, result, divine_path)
    elif spec.family is IconFamily.CLASS:
        _add_class_icon(data_root, mod_folder, icon_name, spec, src, result, divine_path)
    elif spec.family is IconFamily.ACTION_RESOURCE:
        _add_action_resource_icon(data_root, mod_folder, icon_name, spec, src, result, divine_path)
    elif spec.family is IconFamily.PORTRAIT:
        _add_portrait_icon(data_root, mod_folder, icon_name, spec, src, result, divine_path)
    else:  # pragma: no cover - exhaustive
        raise IconAddError(f"Unhandled family {spec.family}")

    return result
