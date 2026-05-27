"""GUI for the BG3 Mod Merger.

A PySide6-based wizard that drives the engine in ``core/``. The GUI is
deliberately thin: it gathers inputs, calls ``core.merger.merge``, and
renders the result. No merge logic lives here.

Entry point: ``python -m gui`` (or the bundled .exe via PyInstaller).
"""
