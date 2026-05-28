"""Application entry point.

Run with ``python -m gui`` from the repo root, or via the bundled
.exe produced by PyInstaller.

The entrypoint loop:
    main()
      apply the RPG-fantasy stylesheet to the QApplication
      show a wizard
      when that wizard finishes after a successful merge, show a FRESH
        wizard in the same process (the user usually wants to do another
        merge right away)
      when a wizard is closed/cancelled without relaunch, quit

Why re-create the wizard in-process instead of relaunching the exe?
    The obvious "relaunch" implementation spawns a new copy of the
    program and lets the old one exit. For a PyInstaller *onefile* exe
    that is unreliable: each launch unpacks the app into a temporary
    ``_MEIxxxxx`` folder, and a process that relaunches itself the
    instant it exits races the dying process's temp-folder cleanup
    against the newborn process's temp-folder extraction. In practice
    this surfaces as "Failed to remove temporary directory" plus
    "Failed to start embedded python interpreter" on the second or
    third cycle.

    Building a brand-new ``MergeWizard`` inside the already-running
    QApplication gives the same clean-slate behavior (fresh state, fresh
    settings load, fresh worker thread) with none of the process-restart
    fragility. The QApplication stays up the whole time; only the wizard
    widget is replaced.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from gui.style import stylesheet
from gui.wizard import MergeWizard


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("BG3 Mod Merger")
    app.setOrganizationName("bg3_mod_merger")

    # Apply the fantasy theme app-wide. Any QDialog/QMessageBox the
    # wizard pops up inherits this automatically.
    app.setStyleSheet(stylesheet())

    # We manage quitting ourselves: closing a wizard shouldn't tear down
    # the QApplication, because we may be about to show another wizard.
    # Without this, the app would exit the moment the first wizard closes
    # and we'd never get the chance to relaunch in-process.
    app.setQuitOnLastWindowClosed(False)

    # Hold the current wizard in a mutable cell so the nested callbacks
    # can swap it. A plain local would be captured by-value at definition
    # time; a one-element list (or dict) lets us mutate the reference.
    current: dict[str, MergeWizard | None] = {"wizard": None}

    def show_wizard() -> None:
        """Create, wire up, and show a fresh wizard."""
        wizard = MergeWizard()
        current["wizard"] = wizard
        # ``finished`` fires on BOTH Finish (accepted) and Cancel/close
        # (rejected). We decide what to do in on_finished by inspecting
        # the relaunch flag, which the wizard's own accepted-hook sets
        # only on a successful merge.
        wizard.finished.connect(on_finished)
        wizard.show()

    def on_finished(_result: int) -> None:
        """Called when the current wizard closes for any reason."""
        wizard = current["wizard"]
        relaunch = bool(getattr(wizard, "relaunch_after_exit", False))

        # Let the just-finished wizard tear down cleanly. We drop our
        # reference and ask Qt to delete it once control returns to the
        # event loop (deleteLater is the safe way to dispose a widget
        # from inside one of its own signal handlers).
        if wizard is not None:
            current["wizard"] = None
            wizard.deleteLater()

        if relaunch:
            # Defer creating the next wizard to the next event-loop tick
            # so the old one is fully disposed first. This is the
            # in-process equivalent of "restart", minus the exe churn.
            QTimer.singleShot(0, show_wizard)
        else:
            # Genuine exit: no relaunch requested. Now we let the app go.
            app.quit()

    show_wizard()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
