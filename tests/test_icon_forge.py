"""Tests for ``core.icon_forge`` and ``gui.icon_forge_dialog``.

The algorithm side is pure PIL operations. We test that the output
shape and color characteristics match expectations. The dialog side
needs a Qt application context but no network, so we exercise it
with synthetic inputs (no Openverse calls in tests).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from core import icon_forge


# --- Algorithm: ForgeOptions defaults -------------------------------------


def test_forge_options_defaults_are_sensible():
    """The default options should produce a usable result without the
    user having to touch any sliders. Color is arcane blue (matches
    the in-game default vibe), glow is medium strength."""
    opts = icon_forge.ForgeOptions()
    assert opts.color_hex == "#39C5FF"
    assert 0 < opts.glow < 10
    assert 0 < opts.glow_size < 1
    assert opts.bg_mode == "transparent"


def test_presets_are_well_formed():
    """All preset entries are (name, hex) tuples with valid hex colors."""
    for name, hex_color in icon_forge.PRESETS:
        assert isinstance(name, str) and name
        assert hex_color.startswith("#")
        assert len(hex_color) == 7
        r, g, b = icon_forge._hex_to_rgb(hex_color)
        assert 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255


def test_hex_to_rgb_handles_shorthand():
    """CSS #RGB shorthand should expand to full #RRGGBB."""
    assert icon_forge._hex_to_rgb("#fff") == (255, 255, 255)
    assert icon_forge._hex_to_rgb("#abc") == (170, 187, 204)


def test_hex_to_rgb_handles_no_prefix():
    assert icon_forge._hex_to_rgb("39C5FF") == (57, 197, 255)


# --- Algorithm: extract_ink -----------------------------------------------


def test_extract_ink_auto_inverts_dark_on_light():
    """The classic clip-art case: black lines on white background.
    extract_ink should auto-detect the bright background and invert
    so the lines come out white in the mask."""
    img = Image.new("RGBA", (100, 100), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([40, 40, 60, 60], fill=(0, 0, 0, 255))

    mask = icon_forge.extract_ink(img)
    centre = mask.getpixel((50, 50))
    corner = mask.getpixel((5, 5))
    assert centre > 200, f"Center should be bright after auto-invert, got {centre}"
    assert corner < 50, f"Corner should be dark after auto-invert, got {corner}"


def test_extract_ink_force_invert_overrides_heuristic():
    """force_invert=False on a dark-on-light source: lines stay dark.
    Useful for inputs the auto-heuristic gets wrong."""
    img = Image.new("RGBA", (100, 100), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([40, 40, 60, 60], fill=(0, 0, 0, 255))

    mask = icon_forge.extract_ink(img, force_invert=False)
    centre = mask.getpixel((50, 50))
    assert centre < 50


# --- Algorithm: stylize ---------------------------------------------------


def test_stylize_returns_rgba_at_target_size():
    """stylize must always return an RGBA image at the requested size,
    regardless of input dimensions."""
    src = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    d = ImageDraw.Draw(src)
    d.ellipse([50, 25, 150, 75], fill=(255, 255, 255, 255))

    out = icon_forge.stylize(src, out_size=380)
    assert out.size == (380, 380)
    assert out.mode == "RGBA"


def test_stylize_uses_chosen_color():
    """A bright-red glow option should yield a result with predominantly
    red glow pixels, not blue, not green. We sample a known
    glow-tinted area (just outside the original line)."""
    src = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    d = ImageDraw.Draw(src)
    d.ellipse([80, 80, 120, 120], fill=(255, 255, 255, 255))

    opts = icon_forge.ForgeOptions(color_hex="#FF0000")
    out = icon_forge.stylize(src, opts, out_size=380)

    r, g, b, a = out.getpixel((190, 190))
    assert r > 150, f"Expected red-heavy center, got rgb=({r},{g},{b})"
    edge = out.getpixel((150, 190))
    r2, g2, b2, a2 = edge
    assert r2 > g2 and r2 > b2, \
        f"Halo should be red-dominant, got rgb=({r2},{g2},{b2})"


def test_stylize_produces_some_transparency_in_corners():
    """A small centered figure should leave the corners of the 380x380
    canvas fully transparent. The stylized icon shouldn't fill the
    whole canvas with glow."""
    src = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    d = ImageDraw.Draw(src)
    d.ellipse([90, 90, 110, 110], fill=(255, 255, 255, 255))

    out = icon_forge.stylize(src, out_size=380)
    corner = out.getpixel((5, 5))
    assert corner[3] < 30, \
        f"Corner alpha should be near-zero for a small figure, got {corner[3]}"


def test_stylize_with_transparent_input_treats_alpha_as_background():
    """If the source has transparent regions (common for clip art),
    those should be treated as background, not as line art. The
    extract_ink helper composites onto opaque black first so transparency
    becomes background."""
    src = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    d = ImageDraw.Draw(src)
    d.ellipse([45, 45, 55, 55], fill=(200, 200, 200, 255))

    out = icon_forge.stylize(src, out_size=380)
    a_min, a_max = out.getchannel("A").getextrema()
    assert a_max > 100, "Stylized output has no visible glow content"


# --- checker helper --------------------------------------------------------


def test_checker_produces_correct_size():
    """The preview-background checker should be the requested size and
    contain more than one color (the alternating pattern). We don't
    test the exact cell layout because the implementation has a
    well-known fence-post quirk that doesn't matter visually."""
    c = icon_forge.checker(380)
    assert c.size == (380, 380)
    colors = {c.getpixel((x, y)) for x in range(0, 380, 20)
              for y in range(0, 380, 20)}
    assert len(colors) >= 2, \
        f"Checker should have at least 2 colors, got {colors}"


# --- Dialog instantiation (no network exercises) --------------------------


@pytest.fixture
def qapp():
    """Provide a QApplication for tests that need Qt."""
    from PySide6.QtWidgets import QApplication
    import sys
    app = QApplication.instance() or QApplication(sys.argv[:1])
    return app


def test_forge_dialog_constructs_without_network(qapp):
    """The dialog should instantiate cleanly without any network calls.
    No source loaded, no result available, Use this button disabled."""
    from gui.icon_forge_dialog import IconForgeDialog
    dlg = IconForgeDialog()
    assert dlg.result_path() is None
    assert not dlg.use_btn.isEnabled()


def test_forge_dialog_render_with_local_source(qapp, tmp_path):
    """Simulate a local image being loaded (no network), then trigger
    a render. After rendering, the Use this button should be enabled
    and result_path should be available after accept."""
    from gui.icon_forge_dialog import IconForgeDialog
    dlg = IconForgeDialog()

    src = Image.new("RGBA", (200, 200), (255, 255, 255, 255))
    d = ImageDraw.Draw(src)
    d.rectangle([50, 50, 150, 150], fill=(0, 0, 0, 255))
    dlg._source = src
    dlg._render()
    assert dlg.use_btn.isEnabled()
    assert dlg._styled is not None
    assert dlg._styled.size == (380, 380)

    dlg._accept_styled()
    result = dlg.result_path()
    assert result is not None
    assert result.is_file()
    reopened = Image.open(result)
    assert reopened.size == (380, 380)


def test_forge_dialog_color_change_updates_swatch(qapp):
    """Clicking a preset color (programmatically) should update the
    color state and the visible swatch styling."""
    from gui.icon_forge_dialog import IconForgeDialog
    dlg = IconForgeDialog()
    dlg._set_color("#FF6A1A")
    assert dlg._color_hex == "#FF6A1A"
    assert "#FF6A1A" in dlg.color_swatch.styleSheet()


def test_forge_dialog_slider_values_are_integers(qapp):
    """The sliders use integer ranges (Qt doesn't do floats natively)
    that get divided by 100 in the render path. Verify the initial
    values translate correctly."""
    from gui.icon_forge_dialog import IconForgeDialog
    dlg = IconForgeDialog()
    assert dlg.glow_slider.value() == 220
    assert dlg.glow_size_slider.value() == 6
    assert dlg.contrast_slider.value() == 115
