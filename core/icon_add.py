"""Generate BG3 icon assets from a source PNG.

Adding a custom icon to a BG3 mod is not "drop a PNG somewhere": the game
wants DDS textures (BC3/DXT5, no mipmaps) at type-specific sizes in
type-specific folders, and for hotbar icons it also needs an *atlas*
(tiled sheet), a UV-coordinate map, and a TextureBank registration.

Crucially, icon types fall into THREE families with completely different
file layouts. We model that explicitly rather than pretending one path
fits all:

  ATLAS family  - Spell / Skill / Item / Passive / Status / Action.
    Three DDS sizes:
      tooltip 380x380  -> Public/<Mod>/GUI/Assets/Tooltips/{Icons|ItemIcons}/<name>.DDS
      controller 144   -> Public/Game/GUI/Assets/ControllerUIIcons/{skills|items}_png/<name>.DDS
      hotbar 64        -> tiled into Public/<Mod>/Assets/Textures/Icons/Icons_<Mod>.dds
    Plus a UV map (GUI/Icons_<Mod>.lsx) and a TextureBank
    (Content/UI/[PAK]_UI/_merged.lsx). Referenced from a stat's
    ``data "Icon" "<name>"`` field.

  CLASS family  - Class / Subclass.
    Standard ~380 + a hotbar copy resized to ~60%, plus low-res copies:
      Public/<Mod>/Assets/Textures/Icons/ClassIcons/<name>.dds
      Public/<Mod>/Assets/Textures/Icons/ClassIcons/hotbar/<name>.dds
      Public/<Mod>/AssetsLowRes/Textures/Icons/ClassIcons/<name>.dds
      Public/<Mod>/AssetsLowRes/Textures/Icons/ClassIcons/hotbar/<name>.dds
    No atlas / UV map. Referenced by the class's internal name.

  CC family     - Race / Background / God (character-creation icons).
    500x500, one DDS + a low-res copy, in type-specific CC subfolders:
      Public/<Mod>/Assets/Textures/Icons/CC/icons_{races|backgrounds|deities}/<name>.dds
      Public/<Mod>/AssetsLowRes/Textures/Icons/CC/icons_{...}/<name>.dds

Per the user's choice, the source PNG is used AS-IS at every size (we
only resize; we do NOT auto-add spell backgrounds or item fade
gradients). Low-res copies ARE generated for the CLASS and CC families.

DDS encoding is BC3/DXT5 without mipmaps (Pillow 12+ writes this
directly, so no external texture tool is needed for the common case).

No Qt dependency here: pure file/image work, unit-testable headlessly.
"""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from PIL import Image

from . import lsx
from . import io_util


# --- Families and types -----------------------------------------------------


class IconFamily(Enum):
    ATLAS = "atlas"      # spell/skill/item/passive/status/action
    CLASS = "class"      # class/subclass
    CC = "cc"            # race/background/god


@dataclass(frozen=True)
class IconTypeSpec:
    """Everything that varies per selectable icon type."""
    label: str               # what the user picks in the UI
    family: IconFamily
    # ATLAS family: which Tooltips subfolder + ControllerUIIcons subfolder.
    tooltip_subfolder: str = "Icons"        # "Icons" or "ItemIcons"
    controller_subfolder: str = "skills_png"  # "skills_png" or "items_png"
    # CC family: which CC subfolder + the square size.
    cc_subfolder: str = ""                  # icons_races / icons_backgrounds / icons_deities
    cc_size: int = 500


# The selectable icon types, in display order. Keyed by label.
ICON_TYPES: dict[str, IconTypeSpec] = {
    "Spell / Skill": IconTypeSpec(
        "Spell / Skill", IconFamily.ATLAS,
        tooltip_subfolder="Icons", controller_subfolder="skills_png",
    ),
    "Passive": IconTypeSpec(
        "Passive", IconFamily.ATLAS,
        tooltip_subfolder="Icons", controller_subfolder="skills_png",
    ),
    "Status": IconTypeSpec(
        "Status", IconFamily.ATLAS,
        tooltip_subfolder="Icons", controller_subfolder="skills_png",
    ),
    "Action Resource": IconTypeSpec(
        "Action Resource", IconFamily.ATLAS,
        tooltip_subfolder="Icons", controller_subfolder="skills_png",
    ),
    "Item": IconTypeSpec(
        "Item", IconFamily.ATLAS,
        tooltip_subfolder="ItemIcons", controller_subfolder="items_png",
    ),
    "Class / Subclass": IconTypeSpec(
        "Class / Subclass", IconFamily.CLASS,
    ),
    "Race": IconTypeSpec(
        "Race", IconFamily.CC, cc_subfolder="icons_races", cc_size=500,
    ),
    "Background": IconTypeSpec(
        "Background", IconFamily.CC, cc_subfolder="icons_backgrounds", cc_size=500,
    ),
    "God / Deity": IconTypeSpec(
        "God / Deity", IconFamily.CC, cc_subfolder="icons_deities", cc_size=500,
    ),
}


# Atlas geometry (ATLAS family). 2048x2048 of 64x64 cells = 32x32 grid.
ATLAS_PX = 2048
ICON_PX = 64
GRID = ATLAS_PX // ICON_PX           # 32
SLOTS = GRID * GRID                  # 1024
UV_STEP = ICON_PX / ATLAS_PX         # 0.03125

# ATLAS family non-atlas sizes.
TOOLTIP_PX = 380
CONTROLLER_PX = 144

# CLASS family sizes.
CLASS_STANDARD_PX = 380
CLASS_HOTBAR_PX = 228   # ~60% of 380, matching the guide's "resize to 50-65%"


@dataclass
class IconAddResult:
    """What add_icon produced: the icon name to reference, and the files
    written/updated."""
    icon_name: str
    family: IconFamily
    files_written: list[Path] = field(default_factory=list)
    files_updated: list[Path] = field(default_factory=list)
    atlas_path: Path | None = None
    slot_index: int = -1
    reference_hint: str = ""   # human guidance on how to reference the icon
    notes: list[str] = field(default_factory=list)


class IconAddError(Exception):
    """Raised when an icon can't be added (bad PNG, atlas full, etc.)."""


# --- Shared helpers ---------------------------------------------------------


def _ensure_parent(path: Path) -> None:
    """mkdir -p the parent dir, long-path-safe on Windows."""
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Long path: fall back to the prefixed form.
        import os
        os.makedirs(io_util.to_long_path(parent), exist_ok=True)


def _load_png(png_path: Path) -> Image.Image:
    """Load the source PNG as RGBA. Raises IconAddError on unreadable input."""
    try:
        img = Image.open(png_path)
        img.load()
    except Exception as e:
        raise IconAddError(f"Couldn't read PNG: {png_path} ({e})") from e
    return img.convert("RGBA")


def _write_dds(img: Image.Image, size: int, dest: Path) -> None:
    """Resize ``img`` to size x size (LANCZOS) and write DXT5/BC3 DDS,
    no mipmaps."""
    resized = img.resize((size, size), Image.LANCZOS)
    _ensure_parent(dest)
    try:
        resized.save(
            str(io_util.to_long_path(dest)), format="DDS", pixel_format="DXT5",
        )
    except Exception as e:
        raise IconAddError(
            f"Failed to write DDS {dest.name}: {e}. "
            f"(Your Pillow build may not support DXT5 DDS encoding.)"
        ) from e


def _validate_icon_name(icon_name: str) -> str:
    icon_name = icon_name.strip()
    if not icon_name:
        raise IconAddError("Icon name can't be empty.")
    if any(c in icon_name for c in '\\/:*?"<>| '):
        raise IconAddError(
            "Icon name can't contain spaces or any of \\ / : * ? \" < > |. "
            "Use letters, numbers, and underscores."
        )
    return icon_name


# --- ATLAS family -----------------------------------------------------------


def _atlas_path(data_root: Path, mod_folder: str) -> Path:
    return (data_root / "Public" / mod_folder / "Assets" / "Textures"
            / "Icons" / f"Icons_{mod_folder}.dds")


def _uv_map_path(data_root: Path, mod_folder: str) -> Path:
    return data_root / "Public" / mod_folder / "GUI" / f"Icons_{mod_folder}.lsx"


def _merged_ui_path(data_root: Path, mod_folder: str) -> Path:
    return (data_root / "Public" / mod_folder / "Content" / "UI"
            / "[PAK]_UI" / "_merged.lsx")


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


def _new_uv_map_document(mod_folder: str, atlas_uuid: str) -> lsx.LsxDocument:
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
                    <attribute id="Path" type="LSString" value="Assets/Textures/Icons/Icons_{mod_folder}.dds"/>
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


def _new_merged_ui_document(mod_folder: str, atlas_uuid: str) -> lsx.LsxDocument:
    source = f"Public/{mod_folder}/Assets/Textures/Icons/Icons_{mod_folder}.dds"
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<save>
    <version major="4" minor="4" revision="4" build="602" />
    <region id="TextureBank">
        <node id="TextureBank">
            <children>
                <node id="Resource">
                    <attribute id="ID" type="FixedString" value="{atlas_uuid}" />
                    <attribute id="Localized" type="bool" value="False" />
                    <attribute id="Name" type="LSString" value="{mod_folder}" />
                    <attribute id="SRGB" type="bool" value="True" />
                    <attribute id="SourceFile" type="LSString" value="{source}" />
                    <attribute id="Streaming" type="bool" value="True" />
                    <attribute id="Template" type="FixedString" value="Icons_Items" />
                    <attribute id="Type" type="int32" value="0" />
                    <attribute id="_OriginalFileVersion_" type="int64" value="144115188075855873" />
                </node>
            </children>
        </node>
    </region>
</save>
"""
    return lsx.parse_bytes(xml.encode("utf-8"))


def _read_existing_uv_map(
    uv_path: Path,
) -> tuple[lsx.LsxDocument | None, dict[str, int], set[int], str | None]:
    """Return (doc, name->slot map, used slots, atlas_uuid) for an existing
    UV map, or (None, {}, set(), None) if absent. Slots are reverse-derived
    from each IconUV's U1/V1 floats."""
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
            u1 = node.attr_value("U1")
            v1 = node.attr_value("V1")
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


def _iter_nodes(node: lsx.Node):
    """Depth-first iterate a node and all descendants."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _next_free_slot(used: set[int]) -> int:
    for i in range(SLOTS):
        if i not in used:
            return i
    raise IconAddError(
        f"The icon atlas is full ({SLOTS} icons). Consider a second atlas."
    )


def _append_uv_node(doc: lsx.LsxDocument, icon_name: str, slot: int) -> None:
    """Append (or replace) an IconUV entry in the IconUVList region."""
    region = doc.region("IconUVList")
    if region is None:
        raise IconAddError("UV map has no IconUVList region to append to.")
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


def _add_atlas_icon(
    data_root: Path, mod_folder: str, icon_name: str, spec: IconTypeSpec,
    src: Image.Image, result: IconAddResult,
) -> None:
    # 1. tooltip DDS (uppercase .DDS) in the type's Tooltips subfolder.
    tooltip = (data_root / "Public" / mod_folder / "GUI" / "Assets"
               / "Tooltips" / spec.tooltip_subfolder / f"{icon_name}.DDS")
    _write_dds(src, TOOLTIP_PX, tooltip)
    result.files_written.append(tooltip)

    # 2. controller DDS (144) in the type's ControllerUIIcons subfolder.
    controller = (data_root / "Public" / "Game" / "GUI" / "Assets"
                  / "ControllerUIIcons" / spec.controller_subfolder
                  / f"{icon_name}.DDS")
    _write_dds(src, CONTROLLER_PX, controller)
    result.files_written.append(controller)

    # 3. hotbar atlas + UV map + TextureBank.
    atlas_path = _atlas_path(data_root, mod_folder)
    uv_path = _uv_map_path(data_root, mod_folder)
    merged_path = _merged_ui_path(data_root, mod_folder)
    result.atlas_path = atlas_path

    atlas_existed = atlas_path.exists()
    uv_doc, name_to_slot, used_slots, atlas_uuid = _read_existing_uv_map(uv_path)

    if icon_name in name_to_slot:
        slot = name_to_slot[icon_name]
        result.notes.append(
            f"Icon {icon_name!r} already in the atlas; updating in place "
            f"(slot {slot})."
        )
    else:
        slot = _next_free_slot(used_slots)
    result.slot_index = slot

    if not atlas_uuid:
        atlas_uuid = str(uuid_mod.uuid4())

    atlas_img = _load_or_create_atlas(atlas_path)
    icon64 = src.resize((ICON_PX, ICON_PX), Image.LANCZOS)
    px, py = _slot_to_pixel(slot)
    atlas_img.paste((0, 0, 0, 0), (px, py, px + ICON_PX, py + ICON_PX))
    atlas_img.paste(icon64, (px, py), icon64)
    _ensure_parent(atlas_path)
    try:
        atlas_img.save(
            str(io_util.to_long_path(atlas_path)), format="DDS", pixel_format="DXT5",
        )
    except Exception as e:
        raise IconAddError(f"Failed to write atlas DDS: {e}") from e
    (result.files_updated if atlas_existed else result.files_written).append(atlas_path)

    # UV map.
    uv_existed = uv_path.exists()
    if uv_doc is None:
        uv_doc = _new_uv_map_document(mod_folder, atlas_uuid)
    _append_uv_node(uv_doc, icon_name, slot)
    _ensure_parent(uv_path)
    lsx.write_file(uv_doc, uv_path)
    (result.files_updated if uv_existed else result.files_written).append(uv_path)

    # TextureBank: create only if missing.
    if not merged_path.exists():
        merged_doc = _new_merged_ui_document(mod_folder, atlas_uuid)
        _ensure_parent(merged_path)
        lsx.write_file(merged_doc, merged_path)
        result.files_written.append(merged_path)
    else:
        result.notes.append(
            "TextureBank _merged.lsx already exists; left unchanged "
            "(assumed to already register this atlas)."
        )

    result.reference_hint = (
        f'Reference this icon as:  data "Icon" "{icon_name}"  in the '
        f'relevant stats file (Spell_*.txt, Passive.txt, Armor.txt, etc.).'
    )


# --- CLASS family -----------------------------------------------------------


def _add_class_icon(
    data_root: Path, mod_folder: str, icon_name: str, spec: IconTypeSpec,
    src: Image.Image, result: IconAddResult,
) -> None:
    base = data_root / "Public" / mod_folder
    targets = [
        # (subpath, size)
        (base / "Assets" / "Textures" / "Icons" / "ClassIcons" / f"{icon_name}.dds", CLASS_STANDARD_PX),
        (base / "Assets" / "Textures" / "Icons" / "ClassIcons" / "hotbar" / f"{icon_name}.dds", CLASS_HOTBAR_PX),
        (base / "AssetsLowRes" / "Textures" / "Icons" / "ClassIcons" / f"{icon_name}.dds", CLASS_STANDARD_PX),
        (base / "AssetsLowRes" / "Textures" / "Icons" / "ClassIcons" / "hotbar" / f"{icon_name}.dds", CLASS_HOTBAR_PX),
    ]
    for dest, size in targets:
        _write_dds(src, size, dest)
        result.files_written.append(dest)
    result.reference_hint = (
        f"Class/subclass icons are matched by the class's internal name. "
        f"Name this icon to match your ClassDescription's Name "
        f"(currently {icon_name!r})."
    )


# --- CC family (race / background / god) ------------------------------------


def _add_cc_icon(
    data_root: Path, mod_folder: str, icon_name: str, spec: IconTypeSpec,
    src: Image.Image, result: IconAddResult,
) -> None:
    base = data_root / "Public" / mod_folder
    sub = spec.cc_subfolder
    targets = [
        base / "Assets" / "Textures" / "Icons" / "CC" / sub / f"{icon_name}.dds",
        base / "AssetsLowRes" / "Textures" / "Icons" / "CC" / sub / f"{icon_name}.dds",
    ]
    for dest in targets:
        _write_dds(src, spec.cc_size, dest)
        result.files_written.append(dest)
    result.reference_hint = (
        f"Character-creation icons ({spec.label}) are matched by the "
        f"race/background/god's internal name (backgrounds use the GUID). "
        f"Name this icon to match (currently {icon_name!r})."
    )
    result.notes.append(
        "Tip: CC icons are usually white with transparency, and "
        "backgrounds are named by their GUID rather than a clean name."
    )


# --- Public entry point -----------------------------------------------------


def add_icon(
    data_root: Path,
    mod_folder: str,
    icon_name: str,
    icon_type: str,
    png_path: Path,
) -> IconAddResult:
    """Add one icon to a mod from a source PNG.

    Args:
        data_root: directory containing Public/<mod_folder>/...
        mod_folder: the mod's folder name (the ``<X>`` in Public/<X>/).
        icon_name: how the icon is referenced (a stat Icon value for the
            ATLAS family; the class/race/etc. internal name otherwise).
            Must be filesystem-safe.
        icon_type: one of ICON_TYPES' keys.
        png_path: the source PNG (any size; ideally square and large).

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

    # Warn (don't fail) if the source is smaller than the biggest target
    # size for this family, since upscaling softens the result.
    biggest = {
        IconFamily.ATLAS: TOOLTIP_PX,
        IconFamily.CLASS: CLASS_STANDARD_PX,
        IconFamily.CC: spec.cc_size,
    }[spec.family]

    result = IconAddResult(icon_name=icon_name, family=spec.family)
    if src.width < biggest or src.height < biggest:
        result.notes.append(
            f"Source PNG is {src.width}x{src.height}, smaller than the "
            f"{biggest}x{biggest} target; it will be upscaled and may look soft."
        )
    if src.width != src.height:
        result.notes.append(
            f"Source PNG isn't square ({src.width}x{src.height}); it will be "
            f"stretched to square. For best results use a square image."
        )

    if spec.family is IconFamily.ATLAS:
        _add_atlas_icon(data_root, mod_folder, icon_name, spec, src, result)
    elif spec.family is IconFamily.CLASS:
        _add_class_icon(data_root, mod_folder, icon_name, spec, src, result)
    elif spec.family is IconFamily.CC:
        _add_cc_icon(data_root, mod_folder, icon_name, spec, src, result)
    else:  # pragma: no cover - exhaustive
        raise IconAddError(f"Unhandled family {spec.family}")

    return result
