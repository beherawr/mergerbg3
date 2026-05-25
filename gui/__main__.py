"""Application entry point.

Run with ``python -m gui`` from the repo root, or via the bundled
.exe produced by PyInstaller.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from gui.wizard import MergeWizard


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("BG3 Mod Merger")
    app.setOrganizationName("bg3_mod_merger")

    wizard = MergeWizard()
    wizard.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
