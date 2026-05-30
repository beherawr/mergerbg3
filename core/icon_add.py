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

# Hotbar atlas geometry: matches what nightb and mysticw ship — 512x512
# DDS organized as an 8x8 grid of 64x64 cells (64 slots per atlas). When
# a mod needs more than 64 hotbar-atlas icons, the tool auto-overflows
# into a second atlas (newAtlas_2.dds + Icons_<mod>_2.lsx), and so on.
ATLAS_PX = 512
ICON_PX = 64
GRID = ATLAS_PX // ICON_PX    # 8 columns and rows
SLOTS = GRID * GRID           # 64 slots per atlas
UV_STEP = ICON_PX / ATLAS_PX  # 0.125 — UV span of one tile

# Half-pixel inset on each UV side. Prevents adjacent tiles from
# bleeding into each other when the engine samples at smaller mip
# levels. Both nightb (NB438_Atlas.lsx) and mysticw
# (ArcaneVanguardAtlas.lsx) use this exact value of 0.5/512.
UV_INSET = 0.5 / ATLAS_PX     # 0.0009765625

# Atlas DDS filename. The BG3 Toolkit defaults a freshly-created atlas
# to 'newAtlas.dds', and both reference mods kept that name. We follow
# the convention instead of coining our own. Overflow atlases get a
# numeric suffix: newAtlas_2.dds, newAtlas_3.dds, etc.
ATLAS_DDS_BASENAME = "newAtlas"

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
        # Distinguish "user never configured divine" from "user configured
        # divine but the path doesn't resolve" — the second case has hit
        # users who got an unhelpful "not configured" message even though
        # they had set a path in Settings.
        if divine_path:
            result.notes.append(
                f"GUI/metadata.lsf.lsx written as text fallback. The "
                f"divine.exe path in Settings ({divine_path!r}) doesn't "
                f"resolve to an existing file - check for typos, surrounding "
                f"quotes (from Windows 'Copy as path'), or that the file "
                f"hasn't been moved. The BG3 multitool will still convert "
                f"the text form at pack time."
            )
        else:
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


def _atlas_dds_path(data_root: Path, mod_folder: str, atlas_index: int = 1) -> Path:
    """The atlas DDS sheet. First atlas is the toolkit-default
    'newAtlas.dds'; overflow atlases get a numeric suffix
    ('newAtlas_2.dds', 'newAtlas_3.dds', ...). Lowercase '.dds' matches
    what real third-party mods ship."""
    name = (ATLAS_DDS_BASENAME if atlas_index == 1
            else f"{ATLAS_DDS_BASENAME}_{atlas_index}")
    return (data_root / "Public" / mod_folder / "Assets" / "Textures"
            / "Icons" / f"{name}.dds")


def _atlas_uv_lsx_path(data_root: Path, mod_folder: str, atlas_index: int = 1) -> Path:
    """The Public-side atlas LSX (TextureAtlasInfo + IconUVList).
    Real mods use author-named LSX files (e.g. 'NB438_Atlas.lsx');
    we use a deterministic 'Icons_<mod>.lsx' so the tool can find its
    own files on subsequent adds. Overflow numbering matches the DDS:
    Icons_<mod>_2.lsx alongside newAtlas_2.dds."""
    name = (f"Icons_{mod_folder}" if atlas_index == 1
            else f"Icons_{mod_folder}_{atlas_index}")
    return data_root / "Public" / mod_folder / "GUI" / f"{name}.lsx"


def _atlas_uv_lsf_path(data_root: Path, mod_folder: str, atlas_index: int = 1) -> Path:
    """Binary LSF form of the Public-side atlas LSX, written alongside
    it via divine. The game's runtime reads the binary form; the
    toolkit can read either, but real mods ship both so the engine has
    the binary at load time."""
    return _atlas_uv_lsx_path(data_root, mod_folder, atlas_index).with_suffix(".lsf")


def _texturebank_path(
    data_root: Path, mod_folder: str, atlas_uuid: str, ext: str,
) -> Path:
    """The TextureBank registry for one atlas. Each atlas needs a
    matching TextureBank Resource entry under
    ``Public/<mod>/Content/[PAK]_Icons_<mod>/<atlas_uuid>.lsf{.lsx}``.

    Without this file, the atlas's UUID is "dangling": the
    ``TextureAtlasPath.UUID`` in the icon LSX has no corresponding
    Resource in any TextureBank. The toolkit warns about this with
    "Resource with UUID ... not found for texture ... in texture
    atlas". The tooltip system can sometimes still resolve the DDS
    by walking the LSX directly, but the inventory rendering pipeline
    expects the texture to come from a TextureBank entry, so without
    one the inventory slot renders blank even though the tooltip
    shows the icon.

    Cross-checked against nightb (which keeps these in
    [PAK]_CharacterVisuals) and mysticw (which uses a randomly-named
    [PAK]_Generated_<guid>). The folder name itself doesn't matter
    to the engine - it's just a packaging hint - so we use a stable
    deterministic name ``[PAK]_Icons_<mod>`` so re-runs of the tool
    don't pile up new folders. The file basename MUST be the atlas's
    UUID so the engine can index it by ID.

    ``ext`` is one of ``"lsf"`` (binary, what the game reads) or
    ``"lsf.lsx"`` (text, what the toolkit can also read).
    """
    return (data_root / "Public" / mod_folder / "Content"
            / f"[PAK]_Icons_{mod_folder}" / f"{atlas_uuid}.{ext}")


def _slot_to_pixel(slot: int) -> tuple[int, int]:
    """Slot index → top-left pixel of that tile within the atlas DDS."""
    row, col = divmod(slot, GRID)
    return col * ICON_PX, row * ICON_PX


def _slot_to_uv(slot: int) -> tuple[float, float, float, float]:
    """Slot index → (U1, V1, U2, V2) with the half-pixel inset that
    real third-party mods use.

    Without an inset, sampling at smaller mip levels can pick up pixels
    from the neighbouring tile and produce visible seams. Both nightb
    and mysticw inset by exactly half a pixel (0.5/512 = 0.0009765625).
    Example: slot 0 → (0.0009765625, 0.0009765625, 0.12402344, 0.12402344).
    """
    row, col = divmod(slot, GRID)
    u1 = col * UV_STEP + UV_INSET
    v1 = row * UV_STEP + UV_INSET
    u2 = (col + 1) * UV_STEP - UV_INSET
    v2 = (row + 1) * UV_STEP - UV_INSET
    return u1, v1, u2, v2


def _uv_to_slot(u1: float, v1: float) -> int | None:
    """Decode the slot index back from a (U1, V1) pair, accounting for
    the half-pixel inset. Returns None if values fall outside the grid."""
    col = round((u1 - UV_INSET) / UV_STEP)
    row = round((v1 - UV_INSET) / UV_STEP)
    if not (0 <= col < GRID and 0 <= row < GRID):
        return None
    return row * GRID + col


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


def _new_full_uv_lsx(atlas_dds_relative: str, atlas_uuid: str) -> lsx.LsxDocument:
    """Build a Public-side atlas LSX matching what nightb and mysticw ship.

    Cross-checked against both reference mods. Critical schema details
    that differ from the lslib defaults we used to emit:

      - Region order: IconUVList FIRST, TextureAtlasInfo SECOND. We
        used to emit them the other way around.
      - ``Path`` attribute uses ``type="string"``, not ``"LSString"``.
        The toolkit accepts string and renders the atlas; LSString made
        the path unreadable.
      - ``Height`` and ``Width`` use ``type="int32"``, not ``"int64"``.
      - ``Path`` value is RELATIVE to the mod root, e.g. just
        ``Assets/Textures/Icons/newAtlas.dds``. We used to emit the full
        ``Public/<mod>/Assets/...`` form, which the toolkit can't
        resolve to a file.
      - Header is ``major="4" minor="8" revision="0" build="400"``, no
        ``lslib_meta`` attribute. Our old header was the lslib default
        and the toolkit may have been silently rejecting it.

    `atlas_dds_relative` should already be the mod-relative path
    (`Assets/Textures/Icons/newAtlas.dds`) — see _add_atlas_icon.
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<save>
    <version major="4" minor="8" revision="0" build="400"/>
    <region id="IconUVList">
        <node id="root">
            <children>
            </children>
        </node>
    </region>
    <region id="TextureAtlasInfo">
        <node id="root">
            <children>
                <node id="TextureAtlasIconSize">
                    <attribute id="Height" type="int32" value="{ICON_PX}"/>
                    <attribute id="Width" type="int32" value="{ICON_PX}"/>
                </node>
                <node id="TextureAtlasPath">
                    <attribute id="Path" type="string" value="{atlas_dds_relative}"/>
                    <attribute id="UUID" type="FixedString" value="{atlas_uuid}"/>
                </node>
                <node id="TextureAtlasTextureSize">
                    <attribute id="Height" type="int32" value="{ATLAS_PX}"/>
                    <attribute id="Width" type="int32" value="{ATLAS_PX}"/>
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


def _new_texturebank_lsx(
    atlas_uuid: str, atlas_name: str, source_file: str,
) -> lsx.LsxDocument:
    """Build a TextureBank LSX that registers one atlas DDS with the
    BG3 asset streamer.

    Cross-checked byte-for-byte against the TextureBank files shipped
    by nightb (Content/[PAK]_CharacterVisuals/<atlas_uuid>.lsf.lsx) and
    mysticw (Content/[PAK]_Generated_<guid>/<atlas_uuid>.lsf.lsx). Both
    use this exact schema, which differs from the atlas LSX schema in
    several ways the toolkit cares about:

      - Header is ``major="4" minor="7" revision="1" build="3"`` with
        ``lslib_meta="v1,bswap_guids,lsf_keys_adjacency"`` (atlas LSX
        uses major=4 minor=8 build=400 with no lslib_meta).
      - Encoding is lowercase ``"utf-8"`` (atlas LSX is uppercase).
      - The whole file has a UTF-8 BOM, tab indent, and CRLF endings.
        Our serializer normalizes to LF + 4-space, which the toolkit
        still accepts.

    Schema details (mismatching any of these blanks the inventory icon
    in-game while the tooltip still shows it, because the inventory
    streamer rejects malformed bank entries):

      - ``ID``: the atlas UUID (matches ``TextureAtlasPath.UUID`` from
        the atlas LSX). This is the link between the two files.
      - ``Name`` / ``Template``: both the atlas's stem (e.g.
        ``"newAtlas"``). They're literal strings, not UUIDs.
      - ``SourceFile``: FULL ``Public/<mod>/Assets/Textures/Icons/
        <atlas>.dds`` path (not mod-relative like the atlas LSX uses).
      - ``Type``: ``int32`` 1 (zero would mean a different texture
        kind; UI atlases are type 1).
      - ``SRGB``: ``False`` (icon atlases store linear data because
        the UI's icon shader handles sRGB conversion itself).
      - ``Width`` / ``Height``: 512 / 512 — must match the actual DDS.
      - ``Depth``: 1 (this is a 2D texture, not a 3D volume texture).
      - ``Streaming``: ``True`` (the engine pages tiles in and out).
    """
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<save>
    <version major="4" minor="7" revision="1" build="3" lslib_meta="v1,bswap_guids,lsf_keys_adjacency"/>
    <region id="TextureBank">
        <node id="TextureBank">
            <children>
                <node id="Resource">
                    <attribute id="ID" type="FixedString" value="{atlas_uuid}"/>
                    <attribute id="Name" type="LSString" value="{atlas_name}"/>
                    <attribute id="SourceFile" type="LSString" value="{source_file}"/>
                    <attribute id="Template" type="FixedString" value="{atlas_name}"/>
                    <attribute id="Streaming" type="bool" value="True"/>
                    <attribute id="Type" type="int32" value="1"/>
                    <attribute id="SRGB" type="bool" value="False"/>
                    <attribute id="Width" type="int32" value="{ATLAS_PX}"/>
                    <attribute id="Height" type="int32" value="{ATLAS_PX}"/>
                    <attribute id="Depth" type="int32" value="1"/>
                </node>
            </children>
        </node>
    </region>
</save>
"""
    return lsx.parse_bytes(xml.encode("utf-8"))


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
                slot = _uv_to_slot(float(u1), float(v1))
            except ValueError:
                continue
            if slot is not None:
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


def _find_atlas_for_icon(
    data_root: Path, mod_folder: str, icon_name: str,
) -> tuple[int, int, lsx.LsxDocument | None, str | None]:
    """Pick which atlas to add this icon to. Walks existing atlases in
    order and stops at the first one that either already has the icon
    name (re-add → reuse slot) or has a free slot. If all existing
    atlases are full, returns the next index for a fresh atlas.

    Returns: (atlas_index, slot, existing_doc_or_None, atlas_uuid_or_None).
      - atlas_index: 1-based atlas number to write to
      - slot: chosen slot (0-63)
      - existing_doc: None when a new atlas needs to be created
      - atlas_uuid: pre-existing UUID if the atlas is being reused
    """
    atlas_index = 1
    while True:
        uv_path = _atlas_uv_lsx_path(data_root, mod_folder, atlas_index)
        if not uv_path.exists():
            return atlas_index, 0, None, None
        doc, name_to_slot, used, atlas_uuid = _read_existing_full_uv(uv_path)
        if icon_name in name_to_slot:
            return atlas_index, name_to_slot[icon_name], doc, atlas_uuid
        if len(used) < SLOTS:
            return atlas_index, _next_free_slot(used), doc, atlas_uuid
        atlas_index += 1


def _write_atlas_uv_files(
    uv_doc: lsx.LsxDocument, lsx_path: Path, lsf_path: Path,
    divine_path: str | None, result: IconAddResult,
) -> None:
    """Write the Public-side atlas UV map: LSX text always, plus the
    binary LSF alongside it when divine.exe is configured.

    Both nightb and mysticw ship both forms in Public/<mod>/GUI/. The
    toolkit reads the LSX to show your icon in its picker; the game's
    runtime reads the LSF binary to render the icon in-game. Shipping
    only the LSX (which is what the old code did) is why the toolkit
    showed the name but couldn't render the texture.
    """
    lsx_existed = lsx_path.exists()
    lsf_existed = lsf_path.exists()

    _ensure_parent(lsx_path)
    lsx.write_file(uv_doc, lsx_path)
    (result.files_updated if lsx_existed else result.files_written).append(lsx_path)

    try:
        divine_exe = divine_mod.find_divine(divine_path)
    except divine_mod.DivineNotFoundError:
        # Same distinction as in _register_in_metadata: name the bad
        # path when one was provided, so the user can see WHAT didn't
        # resolve. Saying just "not configured" when they actually had
        # configured a (now-stale or typo'd) path was misleading.
        if divine_path:
            result.notes.append(
                f"Atlas LSX written ({lsx_path.name}) but the binary "
                f"{lsf_path.name} was NOT written. The divine.exe path "
                f"in Settings ({divine_path!r}) doesn't resolve to an "
                f"existing file - check for typos, surrounding quotes "
                f"(from Windows 'Copy as path'), or that the file "
                f"hasn't been moved. Without the LSF, the BG3 Toolkit "
                f"may show the icon name without rendering its texture."
            )
        else:
            result.notes.append(
                f"Atlas LSX written ({lsx_path.name}) but the binary "
                f"{lsf_path.name} was NOT written because divine.exe "
                f"isn't configured. The BG3 Toolkit may show the icon "
                f"name without rendering its texture until the LSF "
                f"exists. Set divine.exe in Settings and re-add."
            )
        return

    try:
        d = divine_mod.Divine(exe_path=divine_exe)
        d.lsx_to_lsf(lsx_path, lsf_path)
    except divine_mod.DivineError as e:
        raise IconAddError(
            f"divine.exe failed converting atlas LSX to LSF: {e}"
        ) from e

    (result.files_updated if lsf_existed else result.files_written).append(lsf_path)


def _write_texturebank_files(
    data_root: Path, mod_folder: str, atlas_uuid: str, atlas_name: str,
    atlas_dds_path: Path, divine_path: str | None, result: IconAddResult,
) -> None:
    """Write the TextureBank pair (lsf.lsx + lsf) that registers an
    atlas with the BG3 asset streamer.

    Without this, the atlas DDS exists on disk and the icon UV map
    references its UUID, but no TextureBank Resource defines what that
    UUID actually IS. In-game, the tooltip system can sometimes still
    resolve the DDS via the icon LSX, but the inventory rendering
    pipeline goes through the TextureBank and shows a blank slot when
    the entry is missing. The toolkit also surfaces this with a
    "Resource with UUID ... not found for texture ... in texture
    atlas" warning.

    Idempotent: if the file already exists with the same UUID, we leave
    it alone. Re-running icon-add on the same mod doesn't proliferate
    duplicate TextureBank entries.

    Writes both LSX text form (so the toolkit can still read it without
    divine) and LSF binary form (what the game's runtime reads).
    """
    lsx_path = _texturebank_path(data_root, mod_folder, atlas_uuid, "lsf.lsx")
    lsf_path = _texturebank_path(data_root, mod_folder, atlas_uuid, "lsf")

    # Idempotency: if both forms already exist we're done. The TB
    # contents are derivable from inputs - if the LSX is on disk it
    # already says what we'd write.
    if lsx_path.exists() and lsf_path.exists():
        result.notes.append(
            f"TextureBank for atlas {atlas_name} already exists at "
            f"{lsx_path.parent.name}/{lsx_path.name}; left unchanged."
        )
        return

    # Build the document. SourceFile uses the full mod-relative form,
    # not the Assets/-relative form (that's how nightb and mysticw
    # have it, even though it would be redundant relative to the
    # mod's own folder name - the game expects the full path).
    source_file = (f"Public/{mod_folder}/Assets/Textures/Icons/"
                   f"{atlas_dds_path.name}")
    tb_doc = _new_texturebank_lsx(atlas_uuid, atlas_name, source_file)

    lsx_existed = lsx_path.exists()
    lsf_existed = lsf_path.exists()
    _ensure_parent(lsx_path)
    lsx.write_file(tb_doc, lsx_path)
    (result.files_updated if lsx_existed else result.files_written).append(lsx_path)

    try:
        divine_exe = divine_mod.find_divine(divine_path)
    except divine_mod.DivineNotFoundError:
        if divine_path:
            result.notes.append(
                f"TextureBank LSX written ({lsx_path.name}) but the "
                f"binary form was NOT written. The divine.exe path in "
                f"Settings ({divine_path!r}) doesn't resolve to an "
                f"existing file. Without the .lsf, in-game inventory "
                f"slots will show blank icons even though tooltips "
                f"render correctly."
            )
        else:
            result.notes.append(
                f"TextureBank LSX written ({lsx_path.name}) but the "
                f"binary form was NOT written because divine.exe "
                f"isn't configured. In-game inventory slots will show "
                f"blank icons until the LSF exists; tooltips will "
                f"still render. Set divine.exe in Settings and re-add."
            )
        return

    try:
        d = divine_mod.Divine(exe_path=divine_exe)
        d.lsx_to_lsf(lsx_path, lsf_path)
    except divine_mod.DivineError as e:
        raise IconAddError(
            f"divine.exe failed converting TextureBank LSX to LSF: {e}"
        ) from e

    (result.files_updated if lsf_existed else result.files_written).append(lsf_path)


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

    # --- 3. Hotbar atlas (64-slot sheet, overflow when full). ---
    # Walk existing atlases for the right place to put this icon.
    atlas_index, slot, uv_doc, atlas_uuid = _find_atlas_for_icon(
        data_root, mod_folder, icon_name,
    )
    atlas_dds_path = _atlas_dds_path(data_root, mod_folder, atlas_index)
    atlas_lsx_path = _atlas_uv_lsx_path(data_root, mod_folder, atlas_index)
    atlas_lsf_path = _atlas_uv_lsf_path(data_root, mod_folder, atlas_index)
    result.atlas_path = atlas_dds_path
    result.slot_index = slot

    # Detect the "already present" case for the user-facing note. We can
    # tell from uv_doc + a key lookup; _find_atlas_for_icon's contract
    # is that it returns the existing slot for known names.
    if uv_doc is not None:
        existing_keys = set()
        region = uv_doc.region("IconUVList")
        if region is not None:
            existing_keys = {
                c.attr_value("MapKey") for c in region.root_node.children
                if c.id == "IconUV"
            }
        if icon_name in existing_keys:
            result.notes.append(
                f"Icon {icon_name!r} already in atlas {atlas_index}; "
                f"updating in place (slot {slot})."
            )

    if not atlas_uuid:
        atlas_uuid = str(uuid_mod.uuid4())

    # --- 3a. Atlas DDS: load (or create) and paste the 64x64 tile. ---
    atlas_existed = atlas_dds_path.exists()
    atlas_img = _load_or_create_atlas(atlas_dds_path)
    icon64 = src.resize((ICON_PX, ICON_PX), Image.LANCZOS)
    px, py = _slot_to_pixel(slot)
    # Clear the cell first so re-adds don't blend with old pixels.
    atlas_img.paste((0, 0, 0, 0), (px, py, px + ICON_PX, py + ICON_PX))
    atlas_img.paste(icon64, (px, py), icon64)
    _ensure_parent(atlas_dds_path)
    try:
        atlas_img.save(
            str(io_util.to_long_path(atlas_dds_path)),
            format="DDS", pixel_format="DXT5",
        )
    except Exception as e:
        raise IconAddError(f"Failed to write atlas DDS: {e}") from e
    (result.files_updated if atlas_existed else result.files_written).append(atlas_dds_path)

    # --- 3b. Atlas UV map: LSX + LSF binary, both Public side. ---
    # The LSX Path attribute is relative to the mod root (just
    # 'Assets/Textures/Icons/newAtlas.dds'), not the full
    # 'Public/<mod>/Assets/...' path. That's what nightb and mysticw
    # ship, and what the toolkit can resolve.
    if uv_doc is None:
        relative_dds = f"Assets/Textures/Icons/{atlas_dds_path.name}"
        uv_doc = _new_full_uv_lsx(relative_dds, atlas_uuid)
    _append_uv_node_in_region(uv_doc, "IconUVList", icon_name, slot)
    _write_atlas_uv_files(uv_doc, atlas_lsx_path, atlas_lsf_path, divine_path, result)

    # --- 3c. TextureBank: registers the atlas DDS with the asset
    # streamer. Without this, the toolkit warns about a "dangling"
    # atlas UUID and in-game inventory slots render blank even when
    # tooltips show the icon correctly. atlas_name is the DDS stem
    # ('newAtlas' for the first atlas, 'newAtlas_2' for the second,
    # ...), matching the Name/Template fields nightb and mysticw use.
    atlas_name = atlas_dds_path.stem
    _write_texturebank_files(
        data_root, mod_folder, atlas_uuid, atlas_name,
        atlas_dds_path, divine_path, result,
    )

    # --- 4. Register all the Mods/-side files in metadata.lsf ---
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
