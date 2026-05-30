"""Image composition for icon add: optional background + tooltip fade.

The Add Icon dialog supports two cosmetic treatments authors typically
do by hand in an image editor:

  1. **Hotbar background** — composite the icon over a stock 144x144
     runic-frame image so it has the same visual style as base-game
     spell/skill icons. The atlas tile (64x64) is then derived by
     downscaling the composited 144x144 result.

  2. **Tooltip fade** — apply a radial alpha falloff to the 380x380
     tooltip artwork so the edges blend into the dark tooltip popup
     background instead of being a hard rectangle.

Both are OFF by default. The user opts in per-icon from the dialog,
with a live preview showing what each looks like.

The bundled backgrounds live at ``gui/assets/icon_backgrounds/`` in
the source tree, and at ``<bundle>/gui/assets/icon_backgrounds/`` once
PyInstaller has built the exe. ``list_backgrounds()`` finds them in
both layouts via ``sys._MEIPASS`` when frozen.

This module deliberately knows nothing about DDS, divine, atlases, or
the data-root layout. It takes ``PIL.Image`` objects in, returns
``PIL.Image`` objects out. ``core.icon_add`` calls these helpers and
hands the resulting images to the existing DDS-writing code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image, ImageFilter


# --- Background discovery ---------------------------------------------------


def _backgrounds_dir() -> Path | None:
    """Return the directory containing the bundled icon backgrounds.

    Two layouts to support:
      - Running from source: ``<repo>/gui/assets/icon_backgrounds/``
      - PyInstaller one-file: ``<sys._MEIPASS>/gui/assets/icon_backgrounds/``

    Returns ``None`` if neither exists (e.g. someone deleted the
    folder), so callers can degrade to "no backgrounds available"
    rather than crashing.
    """
    import sys
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "gui" / "assets" / "icon_backgrounds")
    # Dev/source layout: this file lives at core/icon_compose.py, so
    # the assets folder is at ../gui/assets/icon_backgrounds.
    candidates.append(
        Path(__file__).resolve().parent.parent
        / "gui" / "assets" / "icon_backgrounds"
    )
    for c in candidates:
        if c.is_dir():
            return c
    return None


def _humanize_filename(stem: str) -> str:
    """Turn ``bonusAction_bg`` into ``Bonus Action``, ``itembg1`` into
    ``Itembg 1``, ``spell_bg`` into ``Spell``.

    Heuristic: strip a trailing ``_bg`` if present (so it doesn't read
    as "Spell Bg" in the dropdown), then insert spaces before
    uppercase letters that follow lowercase letters (camelCase split),
    then replace underscores with spaces and title-case each token.

    Some files don't follow camelCase or _bg suffix conventions
    (``itembg1.png``, ``actionPassive_bg.DDS``, ``heaingPassive_bg.DDS``);
    the heuristic does a best-effort transform and the result is good
    enough for a dropdown — users can still recognize what each maps
    to.
    """
    import re
    name = stem
    # Strip trailing _bg (case-insensitive) so labels read cleanly.
    name = re.sub(r"_?[Bb][Gg]$", "", name)
    # camelCase → space-separated.
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    # Underscores → spaces.
    name = name.replace("_", " ")
    # Collapse whitespace and title-case.
    name = " ".join(part.capitalize() for part in name.split() if part)
    return name or stem  # fall back to raw stem if it sanitized to empty


@dataclass(frozen=True)
class BackgroundChoice:
    """One available background. The dropdown shows ``label``, the
    underlying flow stores ``filename`` so we round-trip exactly the
    same asset between dialog and disk."""
    filename: str       # exact filename including extension
    label: str          # human-readable, shown in the dropdown
    path: Path          # absolute path on disk


def list_backgrounds() -> list[BackgroundChoice]:
    """Discover all backgrounds shipped with the app.

    Returns a list of :class:`BackgroundChoice` sorted alphabetically
    by display label, suitable for populating a dropdown. The caller
    is responsible for prepending a "None / no background" entry
    (we don't include it here because the dropdown widget treats it
    specially — a None selection isn't a real ``BackgroundChoice``).

    Accepts ``.png``, ``.PNG``, ``.dds``, ``.DDS`` files only; ignores
    anything else (a stray README, a thumbs file, ...).
    """
    bg_dir = _backgrounds_dir()
    if bg_dir is None:
        return []
    choices: list[BackgroundChoice] = []
    for p in sorted(bg_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".png", ".dds"):
            continue
        choices.append(BackgroundChoice(
            filename=p.name,
            label=_humanize_filename(p.stem),
            path=p,
        ))
    # Stable sort by display label so the dropdown is alphabetical
    # regardless of underlying filesystem order.
    choices.sort(key=lambda c: c.label.lower())
    return choices


def load_background(choice: BackgroundChoice) -> Image.Image:
    """Open the background image at its native size. Pillow handles
    both PNG and DDS, so we don't need a format switch here.

    The result is RGBA; backgrounds in the shipped set are already
    144x144 RGBA, but we convert defensively so callers can rely on
    the alpha channel being present without checking the source
    format."""
    return Image.open(choice.path).convert("RGBA")


# --- Composition options ----------------------------------------------------


@dataclass
class IconComposeOptions:
    """Per-icon cosmetic options the user picks in the Add Icon dialog.

    Both fields are no-ops at their default values, so passing a
    default-constructed instance through the icon_add pipeline
    produces output byte-identical to the pre-feature behaviour. The
    user has to explicitly opt in to either treatment.

    Attributes:
        background: Choice of stock background to composite under the
            icon. ``None`` (the default) means no background — paste
            the user's PNG on transparent canvas as before.
        tooltip_fade: Strength of the radial alpha falloff applied to
            the 380x380 tooltip image. 0.0 (the default) means no
            fade. 1.0 means strong fade (edges fully transparent).
            Smooth Gaussian falloff in between; see ``compose_tooltip``.
    """
    background: Optional[BackgroundChoice] = None
    tooltip_fade: float = 0.0  # 0.0..1.0

    @property
    def applies_background(self) -> bool:
        return self.background is not None

    @property
    def applies_fade(self) -> bool:
        # Very small fade values are below human perception once the
        # DDS is encoded with DXT5's quantization, so treat near-zero
        # as off. Without this, dragging the slider to 1% and back
        # would still process every icon through the fade pipeline
        # with no visible benefit.
        return self.tooltip_fade > 0.02


# --- Atlas tile / controller icon composition ------------------------------


def compose_atlas_tile(
    foreground: Image.Image,
    options: IconComposeOptions,
    target_px: int,
) -> Image.Image:
    """Produce a single ``target_px``-by-``target_px`` RGBA image
    suitable for either the 64x64 hotbar atlas slot or the 144x144
    controller icon.

    Pipeline:
      1. Start with a target-sized transparent canvas, or a copy of
         the chosen background scaled to the target size.
      2. Resize the user's foreground PNG to the target size, using
         high-quality Lanczos resampling.
      3. Alpha-composite the foreground over the canvas.

    If no background is selected, this degrades to "resize foreground
    to target_px" — the same operation the pre-feature code performed.
    Callers can keep the no-background fast path by checking
    ``options.applies_background`` and skipping this helper entirely,
    but calling it with no background is still correct.

    The 64x64 hotbar tile is derived from a fresh composition at
    target_px=64 rather than downscaling a 144x144 composition, so the
    background's runic-frame detail isn't aliased to mush. Pillow's
    Lanczos resizing on the background handles the 144→64 downscale
    cleanly.
    """
    fg = foreground.convert("RGBA").resize(
        (target_px, target_px), Image.Resampling.LANCZOS
    )

    if options.background is None:
        # No background: callers that hit this branch could have
        # short-circuited, but we handle it cleanly anyway. The result
        # equals the previous no-effects code path.
        return fg

    bg = load_background(options.background).resize(
        (target_px, target_px), Image.Resampling.LANCZOS
    )
    # Composite fg onto bg respecting both alpha channels.
    result = Image.alpha_composite(bg, fg)
    return result


# --- Tooltip 380x380 with radial fade --------------------------------------


def _radial_fade_mask(size: int, strength: float) -> Image.Image:
    """Build a single-channel (L mode) mask for a 380x380 tooltip
    image. White (255) at the centre, transitioning to black (0) near
    the edges. The transition curve is controlled by ``strength``:

      - strength=0.0  → mask is all 255 (no fade); caller should
                        short-circuit and not call this at all.
      - strength=1.0  → mask reaches 0 at the corners and is strongly
                        attenuated everywhere outside the inner 60%.
      - intermediate  → linear interpolation of the cutoff radius.

    The shape: a soft round vignette. We compute a circular distance
    from centre, then linearly map (distance, inner_radius,
    outer_radius) → (255, 0). Outside outer_radius the mask is fully
    transparent. Inside inner_radius it's fully opaque. Between them,
    smooth linear falloff.

    Implementation note: we generate this at the target size each call
    rather than caching, because the strength changes interactively
    with the slider and caching wouldn't help for the size that
    matters (380px, single image per add).
    """
    # Map strength 0..1 to the inner/outer radii (as fractions of
    # size/2). The numbers below were eyeballed against nightb's real
    # tooltip images (Action_DismissShadowforgedBlade_NB438.DDS) so
    # strength=0.7 looks roughly like what real reference mods ship.
    half = size / 2.0
    # outer always reaches the corners (radius = sqrt(2) * half ≈ 1.41).
    # inner shrinks from corners (strength=0) toward centre (strength=1).
    outer = half * 1.45
    inner = half * (1.45 - 1.20 * strength)
    if inner < 1.0:
        inner = 1.0  # avoid div-by-zero

    # Compute the mask as a 2D distance field. Pillow doesn't have a
    # built-in radial gradient generator; we build one with numpy if
    # available (fast) or a pure-Python fallback. Most installs have
    # numpy via Pillow's dependencies, but we don't want to hard-require
    # it.
    try:
        import numpy as np
        ys, xs = np.ogrid[:size, :size]
        cx = cy = half - 0.5
        dist = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
        # Linear falloff between inner and outer; clamp to 0..1.
        t = (outer - dist) / (outer - inner)
        t = np.clip(t, 0.0, 1.0)
        arr = (t * 255.0).astype("uint8")
        return Image.fromarray(arr, mode="L")
    except ImportError:
        # Pure-Python fallback. Slower but always works. For 380x380
        # this is ~140k pixels and runs in under a second in Python,
        # which is fine for a slider interaction (we're not rendering
        # every frame, just on slider release).
        mask = Image.new("L", (size, size), 0)
        pixels = mask.load()
        for y in range(size):
            for x in range(size):
                dx = x - (half - 0.5)
                dy = y - (half - 0.5)
                d = (dx * dx + dy * dy) ** 0.5
                if d <= inner:
                    pixels[x, y] = 255
                elif d >= outer:
                    pixels[x, y] = 0
                else:
                    t = (outer - d) / (outer - inner)
                    pixels[x, y] = int(t * 255)
        return mask


def compose_tooltip(
    foreground: Image.Image,
    options: IconComposeOptions,
    target_px: int = 380,
) -> Image.Image:
    """Produce a ``target_px``-by-``target_px`` RGBA tooltip image.

    Pipeline:
      1. Resize the foreground to target_px.
      2. If a fade is configured, multiply the foreground's alpha
         channel by a radial mask so the edges blend out smoothly.
         (We do NOT also apply a background here — tooltips in BG3
         render against the popup's dark gradient, not against a
         per-icon background frame. Real reference mods like nightb
         confirm this: their Tooltips/Icons/*.DDS files have
         transparent backgrounds, not framed ones.)
      3. Return the result.

    The fade is intentionally separate from the hotbar-background
    treatment. The user can have a runic-frame hotbar icon AND a
    soft-edge tooltip icon, or either independently, or neither.
    """
    fg = foreground.convert("RGBA").resize(
        (target_px, target_px), Image.Resampling.LANCZOS
    )

    if not options.applies_fade:
        return fg

    # Multiply the foreground alpha channel by the radial mask. The
    # result has the same RGB as the foreground but with attenuated
    # alpha at the edges.
    mask = _radial_fade_mask(target_px, options.tooltip_fade)
    r, g, b, a = fg.split()
    # Use ImageChops.multiply with the alpha treated as another L-mode
    # image. Mask values 0..255 scale alpha by 0..1 respectively.
    from PIL import ImageChops
    new_a = ImageChops.multiply(a, mask)
    return Image.merge("RGBA", (r, g, b, new_a))
