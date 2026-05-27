"""Application entry point.

Run with ``python -m gui`` from the repo root, or via the bundled
.exe produced by PyInstaller.

The entrypoint loop:
    main()
      ↳ apply the RPG-fantasy stylesheet to the QApplication
      ↳ create + show the wizard
      ↳ when the wizard closes after a *successful* merge, spawn a
        fresh instance of the same process and exit. The user almost
        always wants to do another merge right after, so this saves
        the explicit re-launch step.

Why spawn a new process instead of just re-creating the wizard?
    Qt's QWizard doesn't have a clean "reset everything and start over"
    API — pages have stale state, the worker thread might still be
    holding resources, settings might have changed between sessions.
    A clean process-restart sidesteps all of that. The cost is ~1s of
    PyInstaller boot time, which is fine.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication

from gui.style import stylesheet
from gui.wizard import MergeWizard


def _relaunch_args() -> tuple[str, list[str]]:
    """Return the (program, args) tuple to relaunch this app.

    Two cases:
    - **Frozen** (PyInstaller bundle): ``sys.executable`` IS the .exe;
      no additional args needed. PyInstaller sets ``sys.frozen`` to
      ``True`` so we can detect this reliably.
    - **Dev mode** (``python -m gui``): ``sys.executable`` is the
      Python interpreter; we need ``-m gui`` to re-enter the package.
    """
    if getattr(sys, "frozen", False):
        return sys.executable, []
    return sys.executable, ["-m", "gui"]


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("BG3 Mod Merger")
    app.setOrganizationName("bg3_mod_merger")

    # Apply the fantasy theme app-wide. Any QDialog/QMessageBox the
    # wizard pops up inherits this automatically.
    app.setStyleSheet(stylesheet())

    wizard = MergeWizard()
    wizard.show()

    exit_code = app.exec()

    # Relaunch when the user completed a successful merge — the common
    # case is they want to immediately do another. ``relaunch_after_exit``
    # is set on the wizard by the ResultPage's "Merge another" button or
    # by the wizard's accepted-signal hook on a clean Finish.
    if getattr(wizard, "relaunch_after_exit", False):
        prog, args = _relaunch_args()
        # detachable so the new process outlives this one.
        # Use startDetached classmethod for cross-version Qt compatibility.
        QProcess.startDetached(prog, args, os.getcwd())

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
