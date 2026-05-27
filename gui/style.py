"""RPG-fantasy stylesheet for the wizard.

A single Qt Style Sheet (QSS) applied to the app at startup. Uses warm
tans/browns/golds rather than the default platform theme, so the app
feels of-a-piece with the BG3 community aesthetic rather than like a
generic developer tool.

Why QSS instead of a custom palette? QSS is the only sane way to style
QWizard's banner, buttons, and side-panel together. A QPalette can't
reach into the wizard's internal "watermark" area or the policy/back
button row.

Color palette:
    parchment           #f3e6c4   page background, body
    parchment_dark      #e6d4a3   input fields, list backgrounds (subtle depth)
    aged_paper          #d4be88   group-box backgrounds (one shade deeper)
    ink                 #2d1f0a   primary text
    ink_soft            #5a4423   secondary text, hints
    gold                #a87f2c   borders, accents, headings
    gold_dark           #7a5a1a   focus/hover variants
    gold_bright         #d4a847   hover highlight
    burgundy            #8b3a2a   primary button background
    burgundy_dark       #6b2a1e   primary button hover
    forest              #4a5d3a   used sparingly for accent states (success)

The stylesheet is OS-agnostic — same colors on Windows/macOS/Linux.
Fonts default to a serif family (Palatino Linotype on Windows, Book
Antiqua / Palatino fallbacks elsewhere) for titles, with body kept in
the same serif family but smaller for readability.
"""

from __future__ import annotations


# Color constants — exposed so other modules (or future tests) can
# reference them without duplicating hex codes.
COLORS = {
    "parchment":      "#f3e6c4",
    "parchment_dark": "#e6d4a3",
    "aged_paper":     "#d4be88",
    "ink":            "#2d1f0a",
    "ink_soft":       "#5a4423",
    "gold":           "#a87f2c",
    "gold_dark":      "#7a5a1a",
    "gold_bright":    "#d4a847",
    "burgundy":       "#8b3a2a",
    "burgundy_dark":  "#6b2a1e",
    "forest":         "#4a5d3a",
}


_QSS_TEMPLATE = """
/* ============================================================
 * RPG-fantasy theme for BG3 Mod Merger
 *
 * Cascade: top-level QWizard/QWidget rules set the parchment
 * background + ink text. Per-widget rules layer in the touches
 * that make group boxes feel like framed parchment panels,
 * inputs feel pressed into aged paper, buttons feel like
 * stamped seals.
 * ============================================================ */

/* ---- Base canvas: parchment background, dark ink text ---- */

QWizard,
QWidget {
    background-color: {parchment};
    color: {ink};
    font-family: "Palatino Linotype", "Book Antiqua", Palatino, "Times New Roman", serif;
    font-size: 11pt;
}

/* The QWizard frame itself has a slightly different background area
 * around the page (the "watermark" strip). Pull it into the parchment
 * tone too so the page doesn't look like it's floating on a gray rectangle. */
QWizard > QWidget {
    background-color: {parchment};
}

/* ---- Wizard title strip ----
 * QWizard renders the page title in a banner across the top. We can't
 * easily style the banner background directly, but we can style the
 * labels inside it. The visual goal: title in deep gold serif, subtitle
 * in softer ink. */
QWizardPage {
    background-color: {parchment};
}

/* ---- Headings and labels ---- */

QLabel {
    color: {ink};
    background-color: transparent;
}

/* The wizard's auto-generated title labels live inside QWizard; we tag
 * them with a slightly larger / bolder appearance via the standard
 * label rule. Subtitle/hint italics are set inline in wizard.py. */
QLabel[role="title"] {
    color: {gold_dark};
    font-size: 16pt;
    font-weight: bold;
}

/* ---- Group boxes: framed parchment panels ---- */

QGroupBox {
    background-color: {aged_paper};
    border: 2px solid {gold};
    border-radius: 6px;
    margin-top: 14px;        /* leave room for the title bump */
    padding: 12px 8px 8px 8px;
    font-weight: bold;
    color: {gold_dark};
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    background-color: {parchment};
    color: {gold_dark};
}

/* ---- Text inputs: aged-paper recess with gold border ---- */

QLineEdit,
QPlainTextEdit,
QTextEdit {
    background-color: {parchment_dark};
    color: {ink};
    border: 1px solid {gold};
    border-radius: 3px;
    padding: 4px 6px;
    selection-background-color: {gold_bright};
    selection-color: {ink};
}

QLineEdit:focus,
QPlainTextEdit:focus,
QTextEdit:focus {
    border: 2px solid {gold_dark};
    /* Keep padding stable when border thickens so layout doesn't shift. */
    padding: 3px 5px;
}

QLineEdit:disabled,
QPlainTextEdit:disabled,
QTextEdit:disabled {
    background-color: {aged_paper};
    color: {ink_soft};
}

/* ---- Lists ---- */

QListWidget {
    background-color: {parchment_dark};
    color: {ink};
    border: 1px solid {gold};
    border-radius: 3px;
    padding: 2px;
    outline: 0;             /* kill the dotted focus rectangle */
}

QListWidget::item {
    padding: 4px 6px;
    border-radius: 2px;
}

QListWidget::item:selected {
    background-color: {gold_bright};
    color: {ink};
}

QListWidget::item:hover:!selected {
    background-color: {parchment};
}

/* ---- Buttons: stamped-seal aesthetic ---- */

QPushButton {
    background-color: {aged_paper};
    color: {ink};
    border: 1px solid {gold};
    border-radius: 4px;
    padding: 5px 14px;
    min-height: 22px;
    font-weight: bold;
}

QPushButton:hover {
    background-color: {gold_bright};
    border: 1px solid {gold_dark};
}

QPushButton:pressed {
    background-color: {gold};
    border: 1px solid {gold_dark};
}

QPushButton:disabled {
    background-color: {parchment};
    color: {ink_soft};
    border: 1px solid {ink_soft};
}

/* The wizard's Next/Finish button gets the burgundy primary treatment
 * via objectName matching. QWizard names these buttons internally:
 * "__qt__passive_wizardbutton1" (Next), "__qt__passive_wizardbutton2"
 * (Commit), "__qt__passive_wizardbutton3" (Finish). We use [default]
 * which Qt sets on the wizard's primary action button instead. */
QPushButton:default {
    background-color: {burgundy};
    color: #f5e9c8;
    border: 1px solid {burgundy_dark};
}

QPushButton:default:hover {
    background-color: {burgundy_dark};
    color: #fff5d6;
}

QPushButton:default:pressed {
    background-color: #4f1d12;
}

QPushButton:default:disabled {
    background-color: {aged_paper};
    color: {ink_soft};
    border: 1px solid {ink_soft};
}

/* ---- Radio buttons & checkboxes ---- */

QRadioButton,
QCheckBox {
    color: {ink};
    spacing: 6px;
    background-color: transparent;
}

QRadioButton::indicator,
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid {gold};
    background-color: {parchment_dark};
}

QRadioButton::indicator {
    border-radius: 8px;
}

QCheckBox::indicator {
    border-radius: 2px;
}

QRadioButton::indicator:checked,
QCheckBox::indicator:checked {
    background-color: {gold};
    border: 1px solid {gold_dark};
}

QRadioButton::indicator:hover,
QCheckBox::indicator:hover {
    border: 1px solid {gold_dark};
}

/* ---- ComboBox ---- */

QComboBox {
    background-color: {parchment_dark};
    color: {ink};
    border: 1px solid {gold};
    border-radius: 3px;
    padding: 3px 24px 3px 8px;
}

QComboBox:hover {
    border: 1px solid {gold_dark};
}

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 18px;
    border-left: 1px solid {gold};
}

QComboBox QAbstractItemView {
    background-color: {parchment_dark};
    color: {ink};
    border: 1px solid {gold};
    selection-background-color: {gold_bright};
    selection-color: {ink};
    outline: 0;
}

/* ---- Progress bar ---- */

QProgressBar {
    background-color: {parchment_dark};
    color: {ink};
    border: 1px solid {gold};
    border-radius: 3px;
    text-align: center;
    height: 18px;
}

QProgressBar::chunk {
    background-color: {gold};
    border-radius: 2px;
}

/* ---- Scroll bars: thin parchment-colored, gold thumb ---- */

QScrollBar:vertical {
    background-color: {parchment_dark};
    width: 12px;
    border: 1px solid {gold};
    border-radius: 3px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background-color: {gold};
    min-height: 24px;
    border-radius: 2px;
    margin: 2px;
}

QScrollBar::handle:vertical:hover {
    background-color: {gold_dark};
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;       /* hide the arrow buttons */
}

QScrollBar:horizontal {
    background-color: {parchment_dark};
    height: 12px;
    border: 1px solid {gold};
    border-radius: 3px;
    margin: 0;
}

QScrollBar::handle:horizontal {
    background-color: {gold};
    min-width: 24px;
    border-radius: 2px;
    margin: 2px;
}

QScrollBar::handle:horizontal:hover {
    background-color: {gold_dark};
}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {
    width: 0;
}

/* ---- Dialogs / message boxes ---- */

QDialog,
QMessageBox {
    background-color: {parchment};
    color: {ink};
}

QMessageBox QLabel {
    color: {ink};
    background-color: transparent;
}

/* ---- ToolTips ---- */

QToolTip {
    background-color: {aged_paper};
    color: {ink};
    border: 1px solid {gold};
    padding: 4px 8px;
}
"""


def stylesheet() -> str:
    """Return the QSS string with all color tokens substituted in.

    Tokens like ``{parchment}`` in the template get replaced from the
    ``COLORS`` dict so the palette lives in one place. ``str.format``
    would conflict with CSS's literal ``{`` braces in pseudo-selectors,
    so we do a simple per-token replace instead.
    """
    out = _QSS_TEMPLATE
    for token, color in COLORS.items():
        out = out.replace("{" + token + "}", color)
    return out
