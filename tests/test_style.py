"""Tests for the RPG-fantasy stylesheet.

The QSS itself is mostly visual, but a few invariants matter:
1. Every token in the template gets substituted (no ``{foo}`` left over,
   which would render as literal text in Qt and break the selector).
2. Every color value is a valid 6-digit hex string.
3. The stylesheet applies to a real ``QApplication`` without throwing.

Visual correctness (does it actually look RPG-fantasy?) is a human
judgement call and not testable here.
"""

from __future__ import annotations

import os
import re

import pytest

# Headless Qt: needed before importing PySide6 widgets.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from gui.style import COLORS, stylesheet


def test_all_color_tokens_resolved():
    """The template uses ``{token}`` placeholders that get replaced
    from COLORS at build time. Any unresolved token would render as
    literal text in Qt and silently break the selector. Catch it here.
    """
    qss = stylesheet()
    unresolved = re.findall(r"\{([a-z_]+)\}", qss)
    assert not unresolved, (
        f"stylesheet has unresolved tokens (typos in COLORS keys?): "
        f"{sorted(set(unresolved))}"
    )


def test_color_palette_uses_valid_hex():
    """Every color in COLORS is a 6-digit lowercase hex string. This
    rules out typos like missing leading ``#`` or 3-digit shorthand
    that Qt accepts but is less readable for the palette."""
    for name, value in COLORS.items():
        assert re.fullmatch(r"#[0-9a-f]{6}", value), (
            f"COLORS[{name!r}] = {value!r} is not a 6-digit lowercase "
            f"hex color"
        )


def test_required_selectors_present():
    """Make sure the QSS covers the widget types the app actually uses.
    A regression here would mean a particular widget shows up unstyled
    (looks like raw OS chrome amid the RPG theme).
    """
    qss = stylesheet()
    required = [
        "QWizard", "QWizardPage", "QGroupBox",
        "QPushButton", "QLineEdit", "QListWidget",
        "QRadioButton", "QCheckBox", "QProgressBar",
        "QPlainTextEdit", "QTextEdit", "QComboBox",
        "QScrollBar", "QMessageBox", "QLabel",
    ]
    for sel in required:
        assert sel in qss, f"stylesheet missing rules for {sel}"


def test_stylesheet_applies_without_qt_warnings(qapp):
    """Applying the stylesheet to a real QApplication should not raise.
    Qt logs QSS parser warnings to stderr but doesn't raise: we can't
    easily intercept those, but at minimum the call must complete.
    """
    qapp.setStyleSheet(stylesheet())
    # Round-trip: Qt should accept the QSS and report it back via
    # styleSheet(). Empty string back would suggest catastrophic
    # rejection (Qt actually doesn't do this in practice; it falls
    # back to silently ignoring broken rules).
    assert qapp.styleSheet() == stylesheet()
    # Reset so we don't leak the style into other tests' state.
    qapp.setStyleSheet("")


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
