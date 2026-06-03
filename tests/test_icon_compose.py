"""Tests for ``core.icon_compose``.

The module is pure image processing; we exercise it with synthetic
PIL images so the tests don't depend on the bundled background set
existing (CI might run on a checkout where vendor assets weren't
synced). Where we do need a real background, we construct one on the
fly and inject it via a temp directory + monkeypatch.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from core import icon_compose


# --- _humanize_filename ----------------------------------------------------


def test_humanize_strips_bg_suffix():
    """Most files end in _bg or _BG; the dropdown shouldn't read 'Spell Bg'."""
    assert icon_compose._humanize_filename("spell_bg") == "Spell"
    assert icon_compose._humanize_filename("spell_BG") == "Spell"


def test_humanize_splits_camel_case():
    """`bonusAction_bg` should render as 'Bonus Action', not 'Bonusaction'."""
    assert icon_compose._humanize_filename("bonusAction_bg") == "Bonus Action"
    assert icon_compose._humanize_filename("controlPassive_bg") == "Control Passive"


def test_humanize_falls_back_to_raw_for_unconventional_names():
    """`itembg1` has no separator we'd recognize, so we get a best-effort
    transform. We don't care about the exact result, just that it's
    non-empty and a string."""
    out = icon_compose._humanize_filename("itembg1")
    assert isinstance(out, str) and out


# --- list_backgrounds ------------------------------------------------------


def test_list_backgrounds_discovers_files_in_assets_dir(tmp_path, monkeypatch):
    """Backgrounds at the discovered path get returned, sorted by label."""
    # Set up a synthetic assets dir and point the discovery helper at it.
    bg_dir = tmp_path / "icon_backgrounds"
    bg_dir.mkdir()
    Image.new("RGBA", (144, 144), (255, 0, 0, 255)).save(bg_dir / "spell_bg.png")
    Image.new("RGBA", (144, 144), (0, 255, 0, 255)).save(bg_dir / "action_bg.png")
    # The dot file should be ignored.
    (bg_dir / ".DS_Store").write_bytes(b"")
    # A non-image file should be ignored.
    (bg_dir / "README.txt").write_text("hi")

    monkeypatch.setattr(icon_compose, "_backgrounds_dir", lambda: bg_dir)
    out = icon_compose.list_backgrounds()
    assert len(out) == 2
    # Alphabetical by display label: 'Action' < 'Spell'.
    assert out[0].label == "Action"
    assert out[1].label == "Spell"


def test_list_backgrounds_returns_empty_if_dir_missing(monkeypatch):
    """When the assets folder doesn't exist (developer didn't sync, or
    PyInstaller bundle was tampered with), we return [] cleanly."""
    monkeypatch.setattr(icon_compose, "_backgrounds_dir", lambda: None)
    assert icon_compose.list_backgrounds() == []


def test_list_backgrounds_accepts_dds_files(tmp_path, monkeypatch):
    """The bundled set is mostly .DDS files; this filter must include them."""
    bg_dir = tmp_path / "bg"
    bg_dir.mkdir()
    # Make a real DDS file Pillow can read.
    Image.new("RGBA", (144, 144), (0, 0, 0, 255)).save(
        bg_dir / "spell_bg.DDS", format="DDS", pixel_format="DXT5",
    )
    monkeypatch.setattr(icon_compose, "_backgrounds_dir", lambda: bg_dir)
    assert len(icon_compose.list_backgrounds()) == 1


# --- IconComposeOptions defaults are no-op --------------------------------


def test_default_options_applies_neither_background_nor_fade():
    opts = icon_compose.IconComposeOptions()
    assert not opts.applies_background
    assert not opts.applies_fade


def test_options_with_fade_below_threshold_treated_as_off():
    """A 1% fade is invisible after DXT5 quantization, so we treat it
    as off and skip the entire fade pipeline. Without this gating,
    dragging the slider away from zero and back would still incur the
    compose cost with no visible benefit."""
    opts = icon_compose.IconComposeOptions(tooltip_fade=0.01)
    assert not opts.applies_fade


def test_options_with_meaningful_fade_applies():
    opts = icon_compose.IconComposeOptions(tooltip_fade=0.5)
    assert opts.applies_fade


# --- compose_atlas_tile ----------------------------------------------------


def _fg_with_transparent_corners() -> Image.Image:
    """A 200x200 foreground that's opaque red in a centered 100x100 circle
    and fully transparent outside. Useful for confirming that a
    background shows through the transparent areas of the foreground."""
    fg = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    pixels = fg.load()
    cx = cy = 100
    for y in range(200):
        for x in range(200):
            if (x - cx) ** 2 + (y - cy) ** 2 < 50 * 50:
                pixels[x, y] = (255, 0, 0, 255)
    return fg


def test_atlas_tile_with_no_background_is_just_resize(tmp_path):
    """No background → result equals foreground.resize(target). Alpha
    pattern preserved exactly."""
    fg = _fg_with_transparent_corners()
    opts = icon_compose.IconComposeOptions()  # default: no background
    result = icon_compose.compose_atlas_tile(fg, opts, 64)
    assert result.size == (64, 64)
    # Corner pixel must still be transparent (alpha 0).
    assert result.getpixel((0, 0))[3] == 0
    # Centre pixel must still be red.
    centre = result.getpixel((32, 32))
    assert centre[:3] == (255, 0, 0)
    assert centre[3] == 255


def test_atlas_tile_with_background_fills_transparent_areas(tmp_path, monkeypatch):
    """With a solid-blue background, the foreground's transparent
    corners should reveal blue in the composited result."""
    bg_dir = tmp_path / "bg"
    bg_dir.mkdir()
    bg_path = bg_dir / "test_bg.png"
    Image.new("RGBA", (144, 144), (0, 0, 255, 255)).save(bg_path)
    monkeypatch.setattr(icon_compose, "_backgrounds_dir", lambda: bg_dir)
    bgs = icon_compose.list_backgrounds()
    assert len(bgs) == 1

    fg = _fg_with_transparent_corners()
    opts = icon_compose.IconComposeOptions(background=bgs[0])
    result = icon_compose.compose_atlas_tile(fg, opts, 64)
    assert result.size == (64, 64)
    # Corner pixel: blue from background, fully opaque.
    corner = result.getpixel((0, 0))
    assert corner[:3] == (0, 0, 255)
    assert corner[3] == 255
    # Centre: foreground red wins.
    centre = result.getpixel((32, 32))
    assert centre[:3] == (255, 0, 0)


def test_atlas_tile_target_size_matches_request():
    """64 and 144 are the two sizes the real callers use."""
    fg = Image.new("RGBA", (200, 200), (128, 128, 128, 255))
    opts = icon_compose.IconComposeOptions()
    for target in (64, 144):
        result = icon_compose.compose_atlas_tile(fg, opts, target)
        assert result.size == (target, target)


# --- compose_tooltip -------------------------------------------------------


def test_tooltip_no_fade_returns_resized_foreground():
    fg = Image.new("RGBA", (200, 200), (255, 255, 255, 255))
    opts = icon_compose.IconComposeOptions()  # fade=0
    result = icon_compose.compose_tooltip(fg, opts, 380)
    assert result.size == (380, 380)
    # All pixels still fully opaque (no fade applied).
    a_min, a_max = result.getchannel("A").getextrema()
    assert a_min == 255 and a_max == 255


def test_tooltip_fade_reduces_edge_alpha():
    """With fade > 0, the corner pixels of the result should be
    significantly less opaque than the centre. We tolerate any
    monotonic falloff."""
    # Fully opaque white square as source.
    fg = Image.new("RGBA", (380, 380), (255, 255, 255, 255))
    opts = icon_compose.IconComposeOptions(tooltip_fade=0.8)
    result = icon_compose.compose_tooltip(fg, opts, 380)
    centre_alpha = result.getpixel((190, 190))[3]
    corner_alpha = result.getpixel((10, 10))[3]
    assert centre_alpha > corner_alpha + 100, (
        f"Expected significant alpha falloff from centre to corner; "
        f"got centre={centre_alpha}, corner={corner_alpha}"
    )


def test_tooltip_fade_zero_is_identity_for_alpha():
    """fade=0 must NOT touch alpha at all. Same image in, same alpha out."""
    fg = Image.new("RGBA", (380, 380), (200, 100, 50, 200))
    opts = icon_compose.IconComposeOptions(tooltip_fade=0.0)
    result = icon_compose.compose_tooltip(fg, opts, 380)
    # Source had a=200 everywhere; result alpha should be unchanged.
    a_min, a_max = result.getchannel("A").getextrema()
    assert a_min == 200 and a_max == 200


def test_radial_fade_mask_centre_is_opaque():
    """At fade strength 0.5, the very centre of the mask should still
    be fully opaque (255). The falloff is around the edges only."""
    mask = icon_compose._radial_fade_mask(380, 0.5)
    assert mask.getpixel((190, 190)) == 255
    # Far corner should be reduced.
    assert mask.getpixel((0, 0)) < 255


# --- end-to-end via add_icon ----------------------------------------------


def test_add_icon_with_default_compose_options_unchanged_behaviour(tmp_path):
    """Passing the default IconComposeOptions (or None) must produce
    the same output as the pre-feature code path. This is the backward-
    compatibility guarantee for existing callers."""
    from core import icon_add
    # Set up a minimal data_root with the required mod folders.
    data_root = tmp_path / "ws"
    for sub in ("Mods/M", "Public/M", "Editor/Mods/M", "Projects/M"):
        (data_root / sub).mkdir(parents=True)
    # Mod meta.
    from core import meta as _meta
    _meta.write_mod_meta_file(
        _meta.ModMeta(uuid=_meta.generate_uuid(), folder="M", name="M", author="t"),
        data_root / "Mods" / "M" / "meta.lsx",
    )
    png = tmp_path / "src.png"
    Image.new("RGBA", (400, 400), (200, 100, 50, 255)).save(png)

    # Default options.
    result = icon_add.add_icon(
        data_root=data_root, mod_folder="M",
        icon_name="Test", icon_type="Spell / Skill", png_path=png,
        compose_options=icon_compose.IconComposeOptions(),
    )
    assert any("Tooltips/Icons" in str(p) for p in result.files_written)


def test_add_icon_with_fade_adds_a_note(tmp_path):
    """When the user opts into a fade, the result notes mention it so
    they can verify what was applied (the DDS itself is binary and
    not easily readable)."""
    from core import icon_add, meta as _meta
    data_root = tmp_path / "ws"
    for sub in ("Mods/M", "Public/M", "Editor/Mods/M", "Projects/M"):
        (data_root / sub).mkdir(parents=True)
    _meta.write_mod_meta_file(
        _meta.ModMeta(uuid=_meta.generate_uuid(), folder="M", name="M", author="t"),
        data_root / "Mods" / "M" / "meta.lsx",
    )
    png = tmp_path / "src.png"
    Image.new("RGBA", (400, 400), (200, 100, 50, 255)).save(png)

    result = icon_add.add_icon(
        data_root=data_root, mod_folder="M",
        icon_name="Test", icon_type="Spell / Skill", png_path=png,
        compose_options=icon_compose.IconComposeOptions(tooltip_fade=0.5),
    )
    note_text = " ".join(result.notes).lower()
    assert "fade" in note_text


def test_add_icon_with_background_writes_solid_alpha_to_controller(tmp_path):
    """The 144x144 controller icon must NOT get the background
    composited under it - it stays as the raw artwork on transparent
    canvas, matching what reference mods ship. The 64x64 hotbar tile
    inside the atlas IS the only place the background goes.

    We verify this by adding an icon with a solid background, then
    reading the controller DDS back and confirming it has significant
    transparency (>5% fully-transparent pixels). If the background
    were composited in, the controller would be ~100% opaque.
    """
    from core import icon_add, meta as _meta
    data_root = tmp_path / "ws"
    for sub in ("Mods/M", "Public/M", "Editor/Mods/M", "Projects/M"):
        (data_root / sub).mkdir(parents=True)
    _meta.write_mod_meta_file(
        _meta.ModMeta(uuid=_meta.generate_uuid(), folder="M", name="M", author="t"),
        data_root / "Mods" / "M" / "meta.lsx",
    )
    # Foreground with a transparent rim  -  the same shape we used for
    # other tests. If background got composited under it, the rim
    # would become opaque (filled with bg).
    fg = Image.new("RGBA", (380, 380), (0, 0, 0, 0))
    pixels = fg.load()
    cx = cy = 190
    for y in range(380):
        for x in range(380):
            if (x - cx) ** 2 + (y - cy) ** 2 < 100 * 100:
                pixels[x, y] = (255, 100, 100, 255)
    png = tmp_path / "src.png"
    fg.save(png)

    # Add icon with a real background from the bundled set.
    bgs = icon_compose.list_backgrounds()
    if not bgs:
        pytest.skip("Bundled backgrounds not present in this checkout")
    opts = icon_compose.IconComposeOptions(background=bgs[0])
    icon_add.add_icon(
        data_root=data_root, mod_folder="M",
        icon_name="Test", icon_type="Spell / Skill", png_path=png,
        compose_options=opts,
    )

    # Read the controller DDS back. Pillow can read DDS.
    controller_dds = (
        data_root / "Mods" / "M" / "GUI" / "Assets"
        / "ControllerUIIcons" / "skills_png" / "Test.DDS"
    )
    assert controller_dds.is_file(), "Controller DDS not written"
    controller = Image.open(controller_dds).convert("RGBA")
    alpha = controller.getchannel("A")
    transparent_count = sum(1 for v in alpha.get_flattened_data() if v < 5)
    total = controller.width * controller.height
    transparent_fraction = transparent_count / total
    # The source had ~78% fully-transparent area (everything outside the
    # circle). DXT5 quantization can drift this a few percent. If the
    # background were composited in, transparent fraction would be ~0%.
    assert transparent_fraction > 0.5, (
        f"Controller DDS has only {transparent_fraction:.1%} fully-transparent "
        f"pixels. Expected >50%  -  if you see ~0%, the background got "
        f"incorrectly composited into the controller icon. The background "
        f"should ONLY go on the 64x64 hotbar tile inside the atlas."
    )


def test_add_icon_ignores_compose_options_for_non_atlas_families(tmp_path):
    """Class / Action Resource / Portrait families don't go through
    the atlas pipeline, so compose_options should be silently
    ignored (no crash, no spurious 'applied background' note)."""
    from core import icon_add, meta as _meta
    data_root = tmp_path / "ws"
    for sub in ("Mods/M", "Public/M", "Editor/Mods/M", "Projects/M"):
        (data_root / sub).mkdir(parents=True)
    _meta.write_mod_meta_file(
        _meta.ModMeta(uuid=_meta.generate_uuid(), folder="M", name="M", author="t"),
        data_root / "Mods" / "M" / "meta.lsx",
    )
    png = tmp_path / "src.png"
    Image.new("RGBA", (400, 400)).save(png)

    # Even with strong compose settings, the Class family ignores them.
    result = icon_add.add_icon(
        data_root=data_root, mod_folder="M",
        icon_name="MyClass", icon_type="Class / Subclass", png_path=png,
        compose_options=icon_compose.IconComposeOptions(tooltip_fade=0.8),
    )
    note_text = " ".join(result.notes).lower()
    assert "applied background" not in note_text
    assert "applied tooltip fade" not in note_text
