"""Background worker that runs a merge off the UI thread.

The merge can take seconds (Treehome at 3,000+ files takes ~1.5s on this
machine). Running it on the Qt UI thread would freeze the window for the
whole duration. We use a ``QThread`` subclass that owns a ``MergeConfig``
and emits Qt signals as the merge progresses.

Signal semantics:
- ``progress(phase, current, total, detail)``: fired from
  ``MergeConfig.progress_callback`` inside the worker thread; marshalled
  to the main thread via Qt's signal queue.
- ``finished(result)``: fired exactly once on successful completion with
  the ``MergeResult``.
- ``failed(exc)``: fired exactly once on any exception during the merge.
  The wizard's run page renders this as an error dialog rather than
  crashing the app.

Cancellation: not implemented. The merge phases are all short-running and
filesystem I/O bound; the user can close the wizard window if they really
need to abandon a merge in progress (PyInstaller-built apps will just
terminate the worker thread on exit).
"""

from __future__ import annotations

import traceback

from PySide6.QtCore import QThread, Signal

from core import merger


class MergeWorker(QThread):
    """Runs one ``merger.merge(config)`` call and emits Qt signals.

    Usage::

        worker = MergeWorker(config)
        worker.progress.connect(on_progress)   # main thread slot
        worker.finished_with_result.connect(on_done)
        worker.failed.connect(on_failed)
        worker.start()
    """

    # Note: QThread.finished is already a built-in signal (no args).
    # We use a distinct name to avoid shadowing it.
    progress = Signal(str, int, int, str)
    finished_with_result = Signal(object)  # merger.MergeResult
    failed = Signal(str)                   # already-formatted traceback

    def __init__(self, config: merger.MergeConfig) -> None:
        super().__init__()
        # Take the user-passed config and overlay our own progress_callback.
        # We don't mutate the caller's object; we replace the field.
        self._config = merger.MergeConfig(
            **{
                **{f.name: getattr(config, f.name)
                   for f in config.__dataclass_fields__.values()
                   if f.name != "progress_callback"},
                "progress_callback": self._on_progress,
            }
        )

    def _on_progress(self, phase: str, current: int, total: int, detail: str) -> None:
        """Invoked inside the merger on the worker thread; Qt marshals the
        signal back to the main thread for the UI to consume."""
        self.progress.emit(phase, current, total, detail)

    def run(self) -> None:
        try:
            result = merger.merge(self._config)
            self.finished_with_result.emit(result)
        except Exception:
            # Format the traceback while it's still complete; emit as a
            # plain string so the main thread doesn't need exception
            # plumbing or pickling.
            self.failed.emit(traceback.format_exc())
