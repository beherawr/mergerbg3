"""Qt sub-dialog for forging BG3-style spell/skill icons.

Opens from the AddIconDialog's "Forge Icon..." button. The user
either searches the web (Openverse, free CC-licensed art) or loads
their own image, picks a color and glow strength, then clicks "Use
this". At that point the dialog writes the stylized result to a
temp PNG and returns the path. The AddIconDialog treats that path
exactly as if the user had picked it via "Browse...", so the rest of
the icon-add pipeline (cosmetic background, fade, atlas, TextureBank,
metadata) just works.

The stylization algorithm lives in ``core.icon_forge``. This file is
only chrome: layout, threading for network calls, signal wiring.

Openverse access uses stdlib ``urllib.request`` rather than pulling
in ``requests`` as a new bundled dependency. Slight more code, zero
new ship weight.
"""
from __future__ import annotations

import io
import json
import tempfile
import threading
import traceback
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from PIL import Image
from PySide6.QtCore import Qt, QObject, Signal, QSize, QTimer
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QColorDialog, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton,
    QScrollArea, QSlider, QVBoxLayout, QWidget,
)

from core import icon_forge


OPENVERSE_URL = "https://api.openverse.org/v1/images/"
UA = "BG3IconForge/1.0 (icon authoring tool)"


# --- Stdlib HTTP helpers (no requests dependency) --------------------------


def _http_get_json(url: str, params: dict, timeout: float = 25.0) -> dict:
    """GET with query params, return parsed JSON. Raises on non-200
    or network failure (caller handles)."""
    qs = urllib.parse.urlencode(params)
    full = f"{url}?{qs}"
    req = urllib.request.Request(full, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status} from {full}")
        return json.loads(r.read().decode("utf-8"))


def _http_get_image(url: str, timeout: float = 30.0) -> Image.Image:
    """Fetch a URL and return a PIL RGBA image. Caller handles errors."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _search_openverse(query: str, page_size: int = 12) -> list[dict]:
    """Run an Openverse search. We append 'line art silhouette icon' to
    the user's query if they didn't include 'line' themselves, which
    biases results toward the kind of art that styles well (clip art,
    not photographs)."""
    extra = " line art silhouette icon" if "line" not in query.lower() else ""
    params = {
        "q": query + extra,
        "page_size": page_size,
        "license_type": "all",
    }
    return _http_get_json(OPENVERSE_URL, params).get("results", [])


# --- Qt-side glue ----------------------------------------------------------


def _pil_to_qpixmap(img: Image.Image) -> QPixmap:
    """Convert a PIL RGBA image to QPixmap. Same trick as
    icon_preview.py: go through QImage.copy() so Qt doesn't keep a
    dangling reference to the soon-to-be-freed bytes buffer."""
    rgba = img.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimg = QImage(data, rgba.width, rgba.height, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


class _SearchSignals(QObject):
    """Carrier for worker-thread to Qt-main-thread results.

    We can't update Qt widgets from background threads, so the search
    and image-fetch workers emit signals that the dialog's slots
    handle on the main thread. Each signal carries either a successful
    payload or an error string; the dialog branches accordingly.
    """
    search_done = Signal(list)     # list of openverse result dicts
    search_failed = Signal(str)
    thumb_loaded = Signal(int, object, dict)  # idx, PIL.Image or None, item
    source_loaded = Signal(object, dict)      # PIL.Image, item metadata
    source_failed = Signal(str)


class IconForgeDialog(QDialog):
    """Modal dialog. Caller patterns:

        dlg = IconForgeDialog(parent)
        if dlg.exec() == QDialog.Accepted:
            path = dlg.result_path()  # Path to temp PNG
            # ... use path as the source PNG for icon_add ...

    The temp PNG is cleaned up when the dialog is destroyed.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Forge Icon")
        self.setMinimumSize(1000, 640)
        self.resize(1140, 720)

        # State
        self._source: Optional[Image.Image] = None    # current raw input
        self._styled: Optional[Image.Image] = None    # last stylize() output
        self._result_path: Optional[Path] = None      # temp PNG on Accept
        # Workers populate _thumb_refs to keep QPixmap objects alive while
        # they're displayed in the results grid.
        self._thumb_refs: list[QPixmap] = []

        # Worker signals
        self._signals = _SearchSignals()
        self._signals.search_done.connect(self._on_search_done)
        self._signals.search_failed.connect(self._on_search_failed)
        self._signals.thumb_loaded.connect(self._on_thumb_loaded)
        self._signals.source_loaded.connect(self._on_source_loaded)
        self._signals.source_failed.connect(self._on_source_failed)

        # Debouncing slider drags so we don't re-render 60 times per second
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(150)
        self._render_timer.timeout.connect(self._render)

        self._build_ui()
        self._render_placeholder()

    # ---- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        body = QHBoxLayout()
        body.addLayout(self._build_search_column(), 1)
        body.addLayout(self._build_preview_column(), 2)
        body.addLayout(self._build_controls_column(), 1)
        root.addLayout(body, 1)

        # Bottom action row: Cancel / Use this
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #888; padding-right: 12px;")
        actions.addWidget(self.status_label)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.use_btn = QPushButton("Use this")
        self.use_btn.setDefault(True)
        self.use_btn.setEnabled(False)  # disabled until something's stylized
        self.use_btn.clicked.connect(self._accept_styled)
        actions.addWidget(self.use_btn)
        root.addLayout(actions)

    def _build_search_column(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.addWidget(self._heading("1. Find art"))
        col.addWidget(self._muted(
            "Search free, CC-licensed clip / line art on Openverse,\n"
            "or load your own image."
        ))

        # Search bar.
        bar = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("e.g. lightning bolt, skull, rune")
        self.search_edit.returnPressed.connect(self._do_search)
        bar.addWidget(self.search_edit, 1)
        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._do_search)
        bar.addWidget(search_btn)
        col.addLayout(bar)

        # Load own.
        own_btn = QPushButton("Load my own image...")
        own_btn.clicked.connect(self._load_own)
        col.addWidget(own_btn)

        # Scrollable results panel.
        self.results_scroll = QScrollArea()
        self.results_scroll.setWidgetResizable(True)
        self.results_container = QWidget()
        self.results_layout = QVBoxLayout(self.results_container)
        self.results_layout.setSpacing(4)
        self.results_layout.addStretch(1)
        self.results_scroll.setWidget(self.results_container)
        col.addWidget(self.results_scroll, 1)
        return col

    def _build_preview_column(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.addWidget(self._heading("2. Preview"))
        # The preview is a centered fixed-size label so the styled image
        # is always visible at its native 380x380.
        self.preview_label = QLabel()
        self.preview_label.setFixedSize(380, 380)
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setStyleSheet(
            "QLabel { background: #15131a; border: 1px solid #444; }"
        )
        preview_row = QHBoxLayout()
        preview_row.addStretch(1)
        preview_row.addWidget(self.preview_label)
        preview_row.addStretch(1)
        col.addLayout(preview_row)

        self.caption_label = QLabel("")
        self.caption_label.setStyleSheet("color: #888;")
        self.caption_label.setAlignment(Qt.AlignCenter)
        col.addWidget(self.caption_label)
        col.addStretch(1)
        return col

    def _build_controls_column(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.addWidget(self._heading("3. Stylize"))

        # Color presets: grid of clickable colored buttons.
        col.addWidget(self._muted("Color"))
        preset_grid = QGridLayout()
        preset_grid.setSpacing(4)
        for i, (name, hex_color) in enumerate(icon_forge.PRESETS):
            btn = QPushButton()
            btn.setFixedSize(QSize(50, 28))
            btn.setStyleSheet(
                f"QPushButton {{ background: {hex_color}; border: 1px solid #333; }}"
                f"QPushButton:hover {{ border: 2px solid white; }}"
            )
            btn.setToolTip(name)
            btn.clicked.connect(lambda _=False, h=hex_color: self._set_color(h))
            preset_grid.addWidget(btn, i // 4, i % 4)
        preset_widget = QWidget()
        preset_widget.setLayout(preset_grid)
        col.addWidget(preset_widget)

        # Current color + Custom...
        cur_row = QHBoxLayout()
        self.color_swatch = QLabel()
        self.color_swatch.setFixedSize(30, 22)
        self._color_hex = "#39C5FF"
        self._update_swatch()
        cur_row.addWidget(self.color_swatch)
        custom_btn = QPushButton("Custom...")
        custom_btn.clicked.connect(self._pick_custom_color)
        cur_row.addWidget(custom_btn)
        cur_row.addStretch(1)
        col.addLayout(cur_row)

        # Three sliders.
        self.glow_slider = self._make_slider(col, "Glow intensity",
                                             50, 400, 220)
        self.glow_size_slider = self._make_slider(col, "Glow size",
                                                  2, 18, 6)
        self.contrast_slider = self._make_slider(col, "Line contrast",
                                                  60, 220, 115)

        # "Lines" toggle. The auto-detect heuristic usually picks the
        # right thing, but for tricky inputs the user can force a mode.
        col.addWidget(self._muted("Lines (auto-detect inverts dark-on-light)"))
        lines_row = QHBoxLayout()
        self.lines_group = QButtonGroup(self)
        for label, val in [("Auto", "auto"), ("Dark src", "dark"),
                           ("Light src", "light")]:
            rb = QRadioButton(label)
            rb.setProperty("forge_val", val)
            if val == "auto":
                rb.setChecked(True)
            rb.toggled.connect(self._debounce_render)
            self.lines_group.addButton(rb)
            lines_row.addWidget(rb)
        lines_row.addStretch(1)
        col.addLayout(lines_row)

        col.addStretch(1)
        return col

    def _heading(self, text: str) -> QLabel:
        lbl = QLabel(f"<b>{text}</b>")
        return lbl

    def _muted(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #888;")
        return lbl

    def _make_slider(
        self, parent_layout, label: str, lo: int, hi: int, initial: int,
    ) -> QSlider:
        """Add a horizontal slider with a text label above. Returns the
        slider so the caller can read its value. We store integer
        ranges and divide-by-100 in the render path because QSlider
        only supports ints."""
        parent_layout.addWidget(self._muted(label))
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(initial)
        s.valueChanged.connect(self._debounce_render)
        parent_layout.addWidget(s)
        return s

    # ---- Color handling ----------------------------------------------------

    def _set_color(self, hex_color: str) -> None:
        self._color_hex = hex_color
        self._update_swatch()
        self._debounce_render()

    def _update_swatch(self) -> None:
        self.color_swatch.setStyleSheet(
            f"QLabel {{ background: {self._color_hex}; border: 1px solid #333; }}"
        )

    def _pick_custom_color(self) -> None:
        c = QColorDialog.getColor(QColor(self._color_hex), self,
                                  "Pick glow color")
        if c.isValid():
            self._set_color(c.name())

    # ---- Source loading: own file ----------------------------------------

    def _load_own(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an image", "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*)",
        )
        if not path:
            return
        try:
            self._source = Image.open(path).convert("RGBA")
            self.caption_label.setText(f"Source: {Path(path).name}")
            self._set_status("Loaded local image")
            self._render()
        except Exception as e:
            QMessageBox.warning(
                self, "Couldn't open image",
                f"Couldn't open the file:\n{e}"
            )

    # ---- Source loading: web search ---------------------------------------

    def _do_search(self) -> None:
        q = self.search_edit.text().strip()
        if not q:
            return
        self._set_status("Searching...")
        # Clear previous results.
        self._clear_results_grid()
        self._thumb_refs.clear()
        # Spawn worker thread; results come back via signals.
        threading.Thread(
            target=self._search_worker, args=(q,), daemon=True,
        ).start()

    def _clear_results_grid(self) -> None:
        # Remove all items except the trailing stretch.
        while self.results_layout.count() > 1:
            item = self.results_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _search_worker(self, query: str) -> None:
        """Background-thread search. Emits signals; never touches Qt
        widgets directly."""
        try:
            results = _search_openverse(query)
            self._signals.search_done.emit(results)
        except Exception as e:
            self._signals.search_failed.emit(str(e))

    def _on_search_done(self, results: list) -> None:
        if not results:
            self._set_status("No results")
            self.results_layout.insertWidget(
                0,
                self._muted("No results. Try different words."),
            )
            return
        self._set_status(f"{len(results)} results, fetching thumbs...")
        # Spawn one thumb-loader thread per result.
        for idx, item in enumerate(results):
            thumb_url = item.get("thumbnail") or item.get("url")
            if not thumb_url:
                continue
            threading.Thread(
                target=self._thumb_worker, args=(idx, thumb_url, item),
                daemon=True,
            ).start()

    def _on_search_failed(self, msg: str) -> None:
        self._set_status("Search failed")
        QMessageBox.warning(
            self, "Search failed",
            f"Couldn't reach Openverse:\n{msg}\n\n"
            f"Check your internet connection. You can still use "
            f"'Load my own image...' to forge an icon from a local file."
        )

    def _thumb_worker(self, idx: int, url: str, item: dict) -> None:
        try:
            im = _http_get_image(url)
            im.thumbnail((118, 118))
            self._signals.thumb_loaded.emit(idx, im, item)
        except Exception:
            self._signals.thumb_loaded.emit(idx, None, item)

    def _on_thumb_loaded(
        self, idx: int, im: Optional[Image.Image], item: dict,
    ) -> None:
        # Build a clickable card for this result.
        card = QFrame()
        card.setStyleSheet(
            "QFrame { background: #272231; border-radius: 4px; }"
            "QFrame:hover { background: #34303f; }"
        )
        card.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(card)
        row.setContentsMargins(4, 4, 4, 4)

        if im is not None:
            pix = _pil_to_qpixmap(im)
            self._thumb_refs.append(pix)
            img_lbl = QLabel()
            img_lbl.setPixmap(pix)
        else:
            img_lbl = QLabel("(no preview)")
            img_lbl.setFixedSize(118, 118)
            img_lbl.setAlignment(Qt.AlignCenter)
        row.addWidget(img_lbl)

        meta = QVBoxLayout()
        title = (item.get("title") or "untitled")[:34]
        title_lbl = QLabel(title)
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet("color: #e9e4f0;")
        meta.addWidget(title_lbl)
        lic = (item.get("license") or "?").upper()
        chip_text = ("CC0 / public" if ("cc0" in lic.lower() or lic == "PDM")
                     else f"CC: {lic}")
        chip = QLabel(chip_text)
        chip.setStyleSheet("color: #39C5FF; font-weight: bold;")
        meta.addWidget(chip)
        meta.addStretch(1)
        row.addLayout(meta, 1)

        full_url = item.get("url") or item.get("thumbnail")

        # Make the whole card clickable. mousePressEvent assignment is
        # the lightest-weight way to do this on a QFrame without a
        # custom subclass.
        def on_click(evt, u=full_url, it=item):
            self._choose_web_image(u, it)

        card.mousePressEvent = on_click

        # Insert before the stretch.
        self.results_layout.insertWidget(self.results_layout.count() - 1, card)

    def _choose_web_image(self, url: str, item: dict) -> None:
        self._set_status("Loading image...")
        threading.Thread(
            target=self._source_worker, args=(url, item), daemon=True,
        ).start()

    def _source_worker(self, url: str, item: dict) -> None:
        try:
            im = _http_get_image(url)
            self._signals.source_loaded.emit(im, item)
        except Exception as e:
            self._signals.source_failed.emit(str(e))

    def _on_source_loaded(self, im: Image.Image, item: dict) -> None:
        self._source = im
        title = (item.get("title") or "web image")[:40]
        lic = (item.get("license") or "?").upper()
        self.caption_label.setText(f"Source: {title}  (CC: {lic})")
        self._render()

    def _on_source_failed(self, msg: str) -> None:
        self._set_status("Couldn't load image")
        QMessageBox.warning(
            self, "Couldn't load image",
            f"The selected image couldn't be downloaded:\n{msg}"
        )

    # ---- Rendering ---------------------------------------------------------

    def _debounce_render(self) -> None:
        """Restart the 150ms debounce timer. Last edit wins."""
        self._render_timer.start()

    def _selected_lines_mode(self) -> Optional[bool]:
        """Map the radio-button selection to force_invert's tri-state."""
        for btn in self.lines_group.buttons():
            if btn.isChecked():
                val = btn.property("forge_val")
                if val == "auto":
                    return None
                if val == "dark":
                    return True
                if val == "light":
                    return False
        return None

    def _render(self) -> None:
        if self._source is None:
            return
        try:
            opts = icon_forge.ForgeOptions(
                color_hex=self._color_hex,
                glow=self.glow_slider.value() / 100.0,
                glow_size=self.glow_size_slider.value() / 100.0,
                contrast=self.contrast_slider.value() / 100.0,
                force_invert=self._selected_lines_mode(),
            )
            self._styled = icon_forge.stylize(self._source, opts, out_size=380)
            # Composite on a checker so transparent areas are visible.
            checker = icon_forge.checker(380).convert("RGBA")
            display = Image.alpha_composite(checker, self._styled)
            self.preview_label.setPixmap(_pil_to_qpixmap(display))
            self.use_btn.setEnabled(True)
            self._set_status("Stylized")
        except Exception as e:
            self._set_status("Render error")
            traceback.print_exc()
            QMessageBox.warning(
                self, "Render error",
                f"Couldn't apply stylization:\n{e}"
            )

    def _render_placeholder(self) -> None:
        # Show a checker with an instructional caption when nothing's loaded.
        checker = icon_forge.checker(380).convert("RGBA")
        self.preview_label.setPixmap(_pil_to_qpixmap(checker))
        self.caption_label.setText("Load or search an image to begin")

    # ---- Result delivery ---------------------------------------------------

    def _accept_styled(self) -> None:
        """Write the styled image to a temp PNG and close the dialog."""
        if self._styled is None:
            return
        try:
            tmpdir = tempfile.mkdtemp(prefix="bg3_forge_")
            out_path = Path(tmpdir) / "forged_icon.png"
            self._styled.save(out_path, format="PNG")
            self._result_path = out_path
            self.accept()
        except Exception as e:
            QMessageBox.warning(
                self, "Couldn't save",
                f"Couldn't write the forged icon to a temp file:\n{e}"
            )

    def result_path(self) -> Optional[Path]:
        """After exec() returns Accepted, this gives the caller the
        path to the temp PNG containing the stylized icon. The caller
        is responsible for feeding it into icon_add or wherever."""
        return self._result_path

    # ---- Status line helper -----------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
