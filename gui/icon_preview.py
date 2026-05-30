"""Cosmetic options panel for the Add Icon dialog.

Lives in its own module so the wizard.py file doesn't sprawl further.
Provides:

  IconCosmeticPanel: a QWidget containing the background dropdown,
                     fade slider, and live preview of the three sizes
                     (64x64 hotbar, 144x144 controller, 380x380 tooltip).

The panel emits no Qt signal of its own; the caller polls
``options()`` when assembling the add_icon parameters. We update the
preview internally whenever the user changes any control or sets a
new source PNG via ``set_source_png()``.

Defaults: background = None, fade = 0. These produce no-op
composition, matching the pre-feature behaviour. The user has to
explicitly opt in to either treatment.

The panel auto-hides itself when the parent dialog switches to an
icon type that doesn't go through the atlas pipeline (Class /
Subclass, Action Resource, Portrait). Call ``set_visible_for_family()``
from the parent's type-change handler.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QSlider,
    QVBoxLayout, QWidget,
)

from core import icon_compose
from core.icon_add import IconFamily


def _pil_to_qpixmap(img: Image.Image) -> QPixmap:
    """Convert a PIL RGBA image to a QPixmap.

    Pillow stores pixels as bytes in RGBA order; QImage has a matching
    format. We go through QImage.copy() because the underlying bytes
    buffer needs to outlive the function (QImage doesn't copy from
    raw bytes by default).
    """
    rgba = img.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimg = QImage(data, rgba.width, rgba.height, QImage.Format_RGBA8888)
    # .copy() ensures Qt doesn't keep a reference to the soon-to-be-
    # garbage-collected bytes buffer.
    return QPixmap.fromImage(qimg.copy())


class IconCosmeticPanel(QGroupBox):
    """Background + tooltip-fade controls with live preview thumbnails.

    Layout (left to right): the controls column on the left (background
    dropdown, fade slider, percent readout); the preview row on the
    right (hotbar tile, controller icon, tooltip image side by side,
    each labelled). Updating the source PNG, the background, or the
    fade strength immediately re-renders all three previews.
    """

    # Special user-facing label for the "no background" option. We
    # don't use None as the dropdown data because QComboBox prefers
    # a real string key; this sentinel signals "no background" to
    # the options() builder.
    _NO_BG_LABEL = "(None — no background)"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Cosmetic options (preview)", parent)
        self.setCheckable(False)  # always visible when shown; toggled by parent
        self._source: Optional[Image.Image] = None
        self._backgrounds: list[icon_compose.BackgroundChoice] = []

        layout = QHBoxLayout(self)

        # --- Controls column (left) -------------------------------------
        controls_col = QVBoxLayout()
        controls_form = QFormLayout()

        self.bg_combo = QComboBox()
        # First entry is the "no background" sentinel. The dropdown's
        # currentData() returns None for it; non-None for real choices.
        self.bg_combo.addItem(self._NO_BG_LABEL, None)
        for choice in icon_compose.list_backgrounds():
            self._backgrounds.append(choice)
            self.bg_combo.addItem(choice.label, choice)
        self.bg_combo.currentIndexChanged.connect(self._refresh_preview)
        controls_form.addRow("Background:", self.bg_combo)

        # Fade strength 0..100 (presented as percent). Internally we
        # divide by 100 before handing to IconComposeOptions.
        self.fade_slider = QSlider(Qt.Horizontal)
        self.fade_slider.setRange(0, 100)
        self.fade_slider.setValue(0)
        self.fade_slider.setTickPosition(QSlider.TicksBelow)
        self.fade_slider.setTickInterval(25)
        self.fade_slider.valueChanged.connect(self._on_fade_changed)
        self.fade_value = QLabel("0%  (off)")
        self.fade_value.setMinimumWidth(70)
        fade_row = QHBoxLayout()
        fade_row.addWidget(self.fade_slider, 1)
        fade_row.addWidget(self.fade_value)
        fade_widget = QWidget()
        fade_widget.setLayout(fade_row)
        controls_form.addRow("Tooltip fade:", fade_widget)

        controls_col.addLayout(controls_form)

        # Hint about scope: makes it clear what each control does.
        hint = QLabel(
            "<i>Background composites under the 64x64 hotbar and 144x144 "
            "controller icons. Fade applies a soft radial edge to the "
            "380x380 tooltip image. Both default off.</i>"
        )
        hint.setWordWrap(True)
        controls_col.addWidget(hint)
        controls_col.addStretch(1)
        layout.addLayout(controls_col, 1)

        # --- Preview column (right) -------------------------------------
        preview_col = QVBoxLayout()
        preview_col.addWidget(QLabel("<b>Preview</b>"))

        previews_row = QHBoxLayout()
        # Hotbar (64x64): label above, image below. Same for controller
        # and tooltip. We display all three at their NATURAL sizes so
        # the user sees the real proportions (the hotbar tile really is
        # tiny next to the tooltip).
        for size_px, label_text, attr_name in (
            (64, "Hotbar (64)", "_hotbar_preview"),
            (144, "Controller (144)", "_controller_preview"),
            (190, "Tooltip (380→190)", "_tooltip_preview"),
            # Tooltip displayed at half-size (190) so all three fit on
            # screen on a modest window. The actual DDS written is
            # still 380; we just shrink the preview here for layout.
        ):
            col = QVBoxLayout()
            col.addWidget(QLabel(label_text, alignment=Qt.AlignCenter))
            lbl = QLabel()
            lbl.setFixedSize(size_px, size_px)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(
                # Checkerboard-style placeholder so transparent areas
                # are visible. Subtle so it doesn't dominate.
                "QLabel { background: #2a2a2a; "
                "border: 1px solid #555; }"
            )
            setattr(self, attr_name, lbl)
            col.addWidget(lbl)
            col.addStretch(1)
            previews_row.addLayout(col)

        preview_col.addLayout(previews_row)
        preview_col.addStretch(1)
        layout.addLayout(preview_col, 2)

        # Initial: empty previews, no source loaded.
        self._refresh_preview()

    # --- Public API used by AddIconDialog -------------------------------

    def set_source_png(self, png_path: Optional[Path]) -> None:
        """Load (or clear) the source PNG and re-render previews."""
        if png_path is None or not png_path.is_file():
            self._source = None
        else:
            try:
                self._source = Image.open(png_path).convert("RGBA")
            except Exception:
                # Stay silent — the dialog will fail more loudly when
                # the user clicks Add and the same load fails inside
                # icon_add.add_icon. Showing a load error here would
                # double-up.
                self._source = None
        self._refresh_preview()

    def options(self) -> icon_compose.IconComposeOptions:
        """Snapshot the current control values as an IconComposeOptions.

        Called by AddIconDialog when assembling the add_icon call.
        Returns a default-constructed instance when no choices have
        been made, which makes the add_icon call a no-op for
        composition (identical to pre-feature behaviour).
        """
        bg = self.bg_combo.currentData()  # BackgroundChoice or None
        fade = self.fade_slider.value() / 100.0
        return icon_compose.IconComposeOptions(
            background=bg if isinstance(bg, icon_compose.BackgroundChoice) else None,
            tooltip_fade=fade,
        )

    def set_visible_for_family(self, family: IconFamily) -> None:
        """Hide the panel for icon families that don't go through the
        atlas/tooltip pipeline (Class/ActionResource/Portrait). Their
        DDS writes don't benefit from a background or fade because
        they write to different paths entirely."""
        self.setVisible(family is IconFamily.ATLAS)

    # --- Internals ------------------------------------------------------

    def _on_fade_changed(self, val: int) -> None:
        if val == 0:
            self.fade_value.setText("0%  (off)")
        else:
            self.fade_value.setText(f"{val}%")
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        """Re-render all three preview thumbnails for the current
        source PNG + control state. Cheap: each composition is a few
        Pillow operations, well under 50ms even at 380px."""
        opts = self.options()
        # No source: clear thumbs to empty (the styled placeholder
        # background shows through).
        if self._source is None:
            for attr in ("_hotbar_preview", "_controller_preview", "_tooltip_preview"):
                getattr(self, attr).setPixmap(QPixmap())
            return

        # Hotbar (64) and controller (144) get background if selected.
        hotbar = icon_compose.compose_atlas_tile(self._source, opts, 64)
        controller = icon_compose.compose_atlas_tile(self._source, opts, 144)
        # Tooltip is 380 internally; we downscale to 190 for the
        # preview pane. The DDS that ends up on disk is still 380.
        tooltip_full = icon_compose.compose_tooltip(self._source, opts, 380)
        tooltip_preview = tooltip_full.resize((190, 190), Image.Resampling.LANCZOS)

        self._hotbar_preview.setPixmap(_pil_to_qpixmap(hotbar))
        self._controller_preview.setPixmap(_pil_to_qpixmap(controller))
        self._tooltip_preview.setPixmap(_pil_to_qpixmap(tooltip_preview))
