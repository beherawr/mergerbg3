"""Stylization core for the icon forge.

Adapted from the standalone ``bg3_icon_forge.py`` tool: takes any input
image (a piece of line art, clip art, an icon photograph, whatever the
user supplies) and produces a BG3-style glowing emblem with a chosen
color, glow intensity, and contrast. The output is a single 380x380
RGBA PIL ``Image`` ready to feed into the rest of the icon-add
pipeline as the source PNG.

This module is intentionally UI-agnostic: no Tkinter, no Qt, no
filesystem access, no network. The GUI sub-dialog at
``gui/icon_forge_dialog.py`` is what wraps these helpers with search
bars, color pickers, and previews.

The algorithm here is the result of iteration in a prior session
against real BG3 icon examples; the magic numbers (radius multipliers,
alpha multipliers, contrast curve) were tuned against the cyan and
purple previews we settled on. Don't tweak them without checking the
visual result against a reference BG3 spell icon.
"""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps


# Magic-school color presets the dialog shows as quick-pick swatches.
# Hex values picked to match the in-game look: arcane blue, fire amber,
# frost pale-cyan, etc. Authors can also pick a custom color.
PRESETS: list[tuple[str, str]] = [
    ("Arcane",   "#39C5FF"),
    ("Fire",     "#FF6A1A"),
    ("Frost",    "#9FE8FF"),
    ("Necrotic", "#5BD46B"),
    ("Poison",   "#9BE04B"),
    ("Radiant",  "#FFD24A"),
    ("Psychic",  "#C46BFF"),
    ("Blood",    "#E8453B"),
]


@dataclass
class ForgeOptions:
    """All knobs the user can turn in the forge sub-dialog.

    The default values were retuned after observing that the original
    standalone-tool defaults produced too much halo for the variety of
    sources users actually load (especially the bundled game-icons,
    which already have rich internal detail that gets washed out by a
    heavy glow). The current defaults aim for "crisp line work with a
    modest halo"; users who want the heavier original look can crank
    Glow up to ~2.2 with the slider.

    Field-by-field values came from a sweep against a mix of icon
    types (lightning bolt, skull, dragon orb, rune, shield) - the goal
    was to find one default that looked acceptable across all of them
    rather than great on one and terrible on another.
    """
    color_hex: str = "#39C5FF"     # any "#RRGGBB" or shorthand "#RGB"
    glow: float = 1.4              # outer-glow intensity multiplier (was 2.2)
    glow_size: float = 0.04        # outer-glow radius as fraction of canvas (was 0.06)
    contrast: float = 1.15         # line-mask contrast curve
    core_boost: float = 1.5        # how bright the white-hot inner core is (was 1.3)
    force_invert: bool | None = None
    # ^ None = auto-detect (dark lines on light bg gets inverted); True
    #   forces "treat dark pixels as lines", False forces "treat light
    #   pixels as lines". Most authors leave this on auto.
    vignette: float = 0.55         # only used when bg_mode != "transparent"
    bg_mode: str = "transparent"   # "transparent" or "dark"


# --- Helpers ----------------------------------------------------------------


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    """Parse a CSS-style hex color into (R, G, B). Tolerates "#RGB",
    "#RRGGBB", with or without the leading '#'."""
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _to_luminance(img: Image.Image) -> Image.Image:
    """Flatten any RGBA source onto opaque black and return an L-mode
    grayscale. Doing the alpha composite first means transparent areas
    of the source (common with clip art) become black in the resulting
    mask, i.e. they're treated as background, not as bright lines."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
    return Image.alpha_composite(bg, img).convert("L")


def extract_ink(
    img: Image.Image,
    force_invert: bool | None = None,
    contrast: float = 1.15,
) -> Image.Image:
    """Produce an L-mode mask where bright pixels = the "ink" (line art
    that the stylizer treats as the figure to glow).

    Auto-detection: if the source image's mean brightness is over 50%,
    we assume it's dark lines on a light background and invert. Below
    50%, we assume it's already light-on-dark and leave it alone. This
    works well for typical clip art (black ink on white paper) AND for
    pre-styled icons (light figure on dark background) without the user
    having to choose. ``force_invert`` overrides the heuristic.
    """
    lum = _to_luminance(img)
    mean = sum(lum.getdata()) / (lum.width * lum.height)
    invert = mean > 127 if force_invert is None else force_invert
    mask = ImageOps.invert(lum) if invert else lum
    if contrast != 1.0:
        # Linear contrast curve around midpoint 128.
        mask = mask.point(
            lambda p: max(0, min(255, int((p - 128) * contrast + 128)))
        )
    return mask


def stylize(
    img: Image.Image,
    options: ForgeOptions = None,
    out_size: int = 380,
    max_work_size: int = 1536,
) -> Image.Image:
    """Apply the BG3 emissive treatment and return a ``out_size``-square
    RGBA image. Defaults to 380x380, the tooltip-tier size, which is
    the same size the user would have supplied if they'd browsed for a
    PNG. The rest of the icon-add pipeline downscales from there.

    Algorithm overview:

      1. Resize the source up to the working resolution. The working
         resolution adapts to the source: small bundled icons (256px)
         work at 768; user-loaded 1024+ sources work at their native
         size up to ``max_work_size``. Higher working resolution gives
         crisper output when the source has detail to preserve.
         ``max_work_size`` lets callers cap it (the preview pane uses
         a lower cap for slider-drag responsiveness).
      2. Extract an L-mode "ink mask" of where the lines are.
      3. Optionally lay down a dark vignette background (bg_mode=dark).
      4. Stack three colorized, blurred copies of the mask using SCREEN
         compositing. This is what produces the soft outer glow with
         a brighter core. The (rad_mult, a_mult) pairs are tuned: the
         middle layer is wider+dimmer for atmosphere, the outer is
         tight+strong for line definition, the inner is tight+strong
         for a punchy core highlight.
      5. Lay the colored lines on top so the line strokes themselves
         are saturated, not washed out by glow.
      6. Add a final near-white hot-core pass so the line interiors
         look emissive (like a neon tube with a bright core and a
         colored halo).
      7. Downscale to ``out_size``.
    """
    if options is None:
        options = ForgeOptions()

    base = img.convert("RGBA")
    # Adaptive working resolution. We want the working canvas to be
    # at least big enough to give Gaussian blurs room (768px), but no
    # bigger than the source actually provides detail for (no point
    # working at 2048 from a 256px source). Cap at max_work_size to
    # protect the preview pane's responsiveness.
    src_max = max(base.size)
    work = max(out_size, 768, min(src_max, max_work_size))
    base = ImageOps.contain(base, (work, work))
    sz = work

    mask = extract_ink(base, force_invert=options.force_invert,
                       contrast=options.contrast)
    # Center the mask on a square canvas; input may not be square.
    centered = Image.new("L", (sz, sz), 0)
    centered.paste(mask, ((sz - mask.width) // 2, (sz - mask.height) // 2))
    mask = centered

    r, g, b = _hex_to_rgb(options.color_hex)
    out = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))

    if options.bg_mode == "dark":
        # Radial dark gradient. Visible if the user picks "Dark glow"
        # background mode in the dialog. Mostly useful as a preview
        # aid to see how the icon will look on the in-game tooltip's
        # dark backdrop. We don't ship the dark background to disk;
        # only the lines+glow get written, on transparent canvas.
        grad = Image.new("L", (sz, sz), 0)
        d = ImageDraw.Draw(grad)
        cx = cy = sz / 2
        maxr = sz / 2
        for i in range(int(maxr), 0, -2):
            t = i / maxr
            d.ellipse(
                [cx - i, cy - i, cx + i, cy + i],
                fill=int(255 * (1 - t) * options.vignette),
            )
        stone = Image.new("RGBA", (sz, sz), (20, 18, 26, 255))
        black = Image.new("RGBA", (sz, sz), (0, 0, 0, 255))
        out = Image.alpha_composite(out, Image.composite(stone, black, grad))

    # Outer glow: stacked Gaussian-blurred colorized layers.
    # (rad_mult, a_mult) pairs from the original tuning session.
    radius = max(2, int(options.glow_size * sz))
    glow_layers = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    for rad_mult, a_mult in [(1.0, 1.0), (2.4, 0.55), (0.5, 1.0)]:
        blurred = mask.filter(ImageFilter.GaussianBlur(radius * rad_mult))
        blurred = blurred.point(
            lambda p: min(255, int(p * options.glow * a_mult))
        )
        layer = Image.new("RGBA", (sz, sz), (r, g, b, 0))
        layer.putalpha(blurred)
        glow_layers = ImageChops.screen(glow_layers, layer)
    out = Image.alpha_composite(out, glow_layers)

    # Saturated colored lines on top of the glow.
    line_rgb = Image.new("RGBA", (sz, sz), (r, g, b, 0))
    line_rgb.putalpha(mask)
    out = Image.alpha_composite(out, line_rgb)

    # Hot near-white emissive core. Blend 60% white into the line color
    # to get a punchy highlight without going pure white (which would
    # lose the color identity entirely).
    core = mask.point(lambda p: min(255, int(p * options.core_boost)))
    hot = Image.blend(
        Image.new("RGB", (sz, sz), (r, g, b)),
        Image.new("RGB", (sz, sz), (255, 255, 255)),
        0.6,
    ).convert("RGBA")
    hot.putalpha(core.point(lambda p: int(p * 0.7)))
    out = Image.alpha_composite(out, hot)

    return out.resize((out_size, out_size), Image.Resampling.LANCZOS)


# --- Checkerboard preview helper -------------------------------------------


def checker(
    size: int,
    light: tuple[int, int, int] = (58, 54, 66),
    dark: tuple[int, int, int] = (40, 37, 47),
    cell: int = 12,
) -> Image.Image:
    """Build a checkerboard background image so transparency in the
    preview is visible (otherwise the styled output blends into the
    dialog background and the user can't tell what's transparent vs
    what's just dark). Colors picked to be unobtrusive but visible
    against both light and dark stylized icons."""
    im = Image.new("RGB", (size, size), light)
    d = ImageDraw.Draw(im)
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            if (x // cell + y // cell) % 2:
                d.rectangle([x, y, x + cell, y + cell], fill=dark)
    return im
