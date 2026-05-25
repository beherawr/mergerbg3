"""The merge wizard: a multi-step QWizard that gathers inputs, runs the
merge, and shows the result.

Page flow:

    WorkspacePage   Pick the workspace folder (the user's /data dir that
                    holds all their Toolkit projects) and the optional
                    path to divine.exe. Settings persist to disk so this
                    is a "click Next" for returning users.

    SelectionPage   List every Toolkit project found in the workspace
                    (lightweight scan; only reads each meta.lsx). The
                    user picks Mod A on the left, Mod B on the right,
                    then chooses a merge mode:
                      - Make new mod   → produces a fresh merged project
                                         (current behavior; IdentityPage
                                          configures the new identity)
                      - Combine B into A → in-place merge: mod A keeps
                                         its identity and gets B's content
                                         folded in. IdentityPage is
                                         skipped in this mode.

    IdentityPage    (Make-new-mod only.) New mod identity for the merged
                    output: UUID (auto-generated, user can regenerate),
                    folder name, display name, author, description.
                    Plus the output directory.

    PolicyPage      Conflict policy: skip / prefix / fail, plus the
                    prefix string for the prefix policy.

    ReviewPage      Build reference indexes for both inputs, run
                    find_clashes, show what the merge will do. The user
                    confirms here before any files get written.

    RunPage         Kicks off the worker thread, shows a progress bar
                    and a streaming log. The user can't go back from
                    here while the merge is running.

    ResultPage      Summary of what happened: files written, conflicts,
                    validation findings. Includes "Open output folder"
                    button.

Communication between pages: each page reads/writes a shared
``MergeWizardState`` attached to the wizard instance. QWizard's built-in
``registerField`` system was awkward for a mixed bag of strings + Project
objects + MergeResult, so we use a plain dataclass as a session bag.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSizePolicy,
    QSpacerItem, QTextEdit, QVBoxLayout, QWidget, QWizard, QWizardPage,
)

from core import merger, validate
from core.discover import DiscoveredProject, DiscoveryError, discover_projects
from core.meta import generate_uuid
from core.project import Project

from . import settings as app_settings
from .worker import MergeWorker


# ---------------------------------------------------------------------------
# Shared wizard state
# ---------------------------------------------------------------------------


@dataclass
class WizardState:
    """Everything the pages share. Created once per wizard run.

    Lifecycle: WorkspacePage writes the workspace + divine paths.
    SelectionPage scans the workspace, populates ``discovered``, and the
    user picks ``selected_a`` / ``selected_b`` + ``merge_mode``. The full
    ``Project.load`` happens at the boundary into IdentityPage (where it
    matters for display) or ReviewPage (where it matters for the engine).
    """
    settings: app_settings.Settings

    # Discovery state — populated by SelectionPage when it scans.
    discovered: list[DiscoveredProject] = field(default_factory=list)
    discovery_errors: list[DiscoveryError] = field(default_factory=list)

    # User's selection.
    selected_a: DiscoveredProject | None = None
    selected_b: DiscoveredProject | None = None
    # One of: "new_mod" (creates a fresh merged project; current behavior)
    #         "combine_b_into_a" (in-place merge; mod A is modified to
    #                             include B's content)
    merge_mode: str = "new_mod"

    # Full loaded projects — populated at the SelectionPage→IdentityPage
    # or SelectionPage→ReviewPage boundary, depending on mode.
    project_a: Project | None = None
    project_b: Project | None = None
    project_a_path: str = ""
    project_b_path: str = ""

    new_uuid: str = ""
    new_folder: str = ""
    new_name: str = ""
    new_author: str = ""
    new_description: str = ""
    output_dir: str = ""

    conflict_policy: str = "skip"
    conflict_prefix: str = "Merged_"

    merge_result: merger.MergeResult | None = None
    validation_report: validate.ValidationReport | None = None
    merge_error: str = ""


# ---------------------------------------------------------------------------
# Page 1: pick inputs
# ---------------------------------------------------------------------------


class WorkspacePage(QWizardPage):
    """First-run setup: where is the workspace folder, and where is divine.exe?

    The workspace is the directory that holds all the user's Toolkit
    project subdirectories (typically the BG3 Toolkit ``Data`` folder
    or a sibling). Once configured, both paths persist to
    ``settings.json`` and subsequent runs zip through this page.

    divine.exe is optional today — the merger doesn't require it for
    clean-union merges — but we collect it now so it's ready when later
    sessions add LSF round-trip features.
    """

    def __init__(self, state: WizardState) -> None:
        super().__init__()
        self.state = state
        self.setTitle("Workspace setup")
        self.setSubTitle(
            "Tell the app where your mod projects live. "
            "Your settings persist between runs."
        )

        form = QFormLayout(self)

        # --- Workspace directory ---
        self.workspace_edit = QLineEdit()
        self.workspace_edit.setPlaceholderText(
            "e.g. C:\\Users\\You\\Documents\\Larian Studios\\Baldur's Gate 3\\Mods"
        )
        self.workspace_edit.textChanged.connect(lambda _: self.completeChanged.emit())
        ws_browse = QPushButton("Browse…")
        ws_browse.clicked.connect(self._browse_workspace)
        ws_row = QHBoxLayout()
        ws_row.addWidget(self.workspace_edit, 1)
        ws_row.addWidget(ws_browse)
        ws_widget = QWidget()
        ws_widget.setLayout(ws_row)
        form.addRow("Workspace folder:", ws_widget)

        ws_hint = QLabel(
            "<i>Your BG3 Data folder "
            "ex. ...\Baldurs Gate 3\Data"
            "The wizard will scan it for mods you can merge.</i>"
        )
        ws_hint.setWordWrap(True)
        form.addRow("", ws_hint)

        # --- divine.exe path (optional) ---
        self.divine_edit = QLineEdit()
        self.divine_edit.setPlaceholderText(
            "Optional — e.g. C:\\Tools\\LSLib\\divine.exe"
        )
        div_browse = QPushButton("Browse…")
        div_browse.clicked.connect(self._browse_divine)
        div_row = QHBoxLayout()
        div_row.addWidget(self.divine_edit, 1)
        div_row.addWidget(div_browse)
        div_widget = QWidget()
        div_widget.setLayout(div_row)
        form.addRow("divine.exe path:", div_widget)

        div_hint = QLabel(
            "<i>Path to divine.exe from LSLib"
            "https://github.com/Norbyte/lslib/releases</i>"
        )
        div_hint.setWordWrap(True)
        form.addRow("", div_hint)

    def initializePage(self) -> None:
        """Restore from settings so returning users don't have to refill."""
        self.workspace_edit.setText(self.state.settings.workspace_dir)
        self.divine_edit.setText(self.state.settings.divine_path)

    def _browse_workspace(self) -> None:
        start = self.state.settings.workspace_dir or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Pick your workspace folder", start
        )
        if chosen:
            self.workspace_edit.setText(chosen)

    def _browse_divine(self) -> None:
        # Different OS conventions: on Windows we want a .exe; elsewhere
        # any executable works. We don't restrict the filter; the user
        # might have it under any name.
        start = (
            self.state.settings.divine_path
            or self.state.settings.workspace_dir
            or str(Path.home())
        )
        # Use the file dialog rather than directory dialog.
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Pick divine.exe", start,
            "Executable files (*.exe);;All files (*)"
        )
        if chosen:
            self.divine_edit.setText(chosen)

    def isComplete(self) -> bool:
        # Workspace is required; divine.exe is optional.
        ws = self.workspace_edit.text().strip()
        if not ws:
            return False
        # Soft validation: workspace must be an existing directory. We
        # don't require it to actually contain projects — an empty folder
        # is fine (the user might be setting up before any mods exist).
        return Path(ws).is_dir()

    def validatePage(self) -> bool:
        """Save to settings on Next."""
        ws = self.workspace_edit.text().strip()
        dv = self.divine_edit.text().strip()
        if dv and not Path(dv).is_file():
            ok = QMessageBox.question(
                self, "divine.exe not found",
                f"{dv} doesn't seem to exist. Use it anyway?\n\n"
                "(You can change the path later from this page.)",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ok != QMessageBox.Yes:
                return False
        self.state.settings.workspace_dir = ws
        self.state.settings.divine_path = dv
        # Persist immediately so even if the user closes the wizard
        # without finishing a merge, the next launch remembers.
        app_settings.save(self.state.settings)
        return True


# ---------------------------------------------------------------------------
# Page 2: pick two mods from the workspace + choose merge mode
# ---------------------------------------------------------------------------


class SelectionPage(QWizardPage):
    """List every Toolkit project found in the workspace; the user picks
    two and chooses a merge mode.

    Modes:
    - **Make new mod from A + B** (default) — current behavior, output
      goes to a new directory the user names on the IdentityPage.
    - **Combine B into A** — in-place merge. Mod A keeps its identity;
      mod B's content gets folded in. IdentityPage is skipped because
      there's nothing for the user to configure.

    The discovery scan is lightweight (only reads each project's
    meta.lsx) so even workspaces with dozens of projects open instantly.
    """

    def __init__(self, state: WizardState) -> None:
        super().__init__()
        self.state = state
        self.setTitle("Pick two mods to merge")
        self.setSubTitle(
            "Select mod A on the left and mod B on the right, "
            "then choose what to do with them."
        )

        layout = QVBoxLayout(self)

        # --- Status line: how many mods were found, plus rescan button ---
        status_row = QHBoxLayout()
        self.status_label = QLabel("Scanning workspace…")
        status_row.addWidget(self.status_label, 1)
        self.rescan_button = QPushButton("Rescan")
        self.rescan_button.clicked.connect(self._rescan)
        status_row.addWidget(self.rescan_button)
        layout.addLayout(status_row)

        # --- Two side-by-side list widgets ---
        lists_row = QHBoxLayout()

        a_box = QGroupBox("Mod A")
        a_layout = QVBoxLayout(a_box)
        self.list_a = QListWidget()
        self.list_a.itemSelectionChanged.connect(self._on_selection_changed)
        a_layout.addWidget(self.list_a)
        lists_row.addWidget(a_box, 1)

        b_box = QGroupBox("Mod B")
        b_layout = QVBoxLayout(b_box)
        self.list_b = QListWidget()
        self.list_b.itemSelectionChanged.connect(self._on_selection_changed)
        b_layout.addWidget(self.list_b)
        lists_row.addWidget(b_box, 1)

        layout.addLayout(lists_row, 1)

        # --- Selected-pair preview ---
        self.preview = QLabel("<i>Pick mod A and mod B above.</i>")
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.RichText)
        self.preview.setStyleSheet(
            "QLabel { padding: 6px; background: palette(alternate-base); "
            "border: 1px solid palette(mid); border-radius: 4px; }"
        )
        layout.addWidget(self.preview)

        # --- Merge mode ---
        mode_box = QGroupBox("What should I do with them?")
        mode_layout = QVBoxLayout(mode_box)
        self.mode_group = QButtonGroup(self)
        self.mode_new = QRadioButton(
            "Make a new mod combining A and B  (A and B are not modified)"
        )
        self.mode_new.setChecked(True)
        self.mode_combine = QRadioButton(
            "Combine B into A  (mod A is modified in place; B is untouched)"
        )
        self.mode_group.addButton(self.mode_new, 0)
        self.mode_group.addButton(self.mode_combine, 1)
        mode_layout.addWidget(self.mode_new)
        mode_layout.addWidget(self.mode_combine)
        layout.addWidget(mode_box)

        self.mode_new.toggled.connect(self._on_mode_changed)

    # --- Discovery scan -------------------------------------------------

    def initializePage(self) -> None:
        """Trigger the workspace scan as soon as we land on the page.
        We call _rescan synchronously rather than deferring via
        QTimer.singleShot — the deferred call could fire after the page
        is destroyed (which actually happened in the test suite). The
        _rescan implementation already calls processEvents internally
        so the "Scanning..." status update paints before disk I/O.
        """
        self._rescan()

    def _rescan(self) -> None:
        ws = self.state.settings.workspace_dir
        if not ws:
            self.status_label.setText(
                "<b style='color:#a44;'>No workspace configured.</b> "
                "Go back and set one."
            )
            self.list_a.clear()
            self.list_b.clear()
            return

        self.status_label.setText(f"Scanning {ws}…")
        # Force a paint so the user sees the status update before we
        # block on disk I/O (the scan is fast — meta.lsx only — but
        # for very large workspaces the progress message is reassuring).
        QApplication = type(self).__mro__[-1]  # dummy to avoid import
        try:
            from PySide6.QtWidgets import QApplication as _QApp
            _QApp.processEvents()
        except Exception:
            pass

        found, errors = discover_projects(ws)
        self.state.discovered = found
        self.state.discovery_errors = errors

        # Populate the lists.
        self.list_a.clear()
        self.list_b.clear()
        for d in found:
            item_a = QListWidgetItem(d.display_label)
            item_a.setData(Qt.UserRole, d)
            item_a.setToolTip(_project_tooltip(d))
            self.list_a.addItem(item_a)
            item_b = QListWidgetItem(d.display_label)
            item_b.setData(Qt.UserRole, d)
            item_b.setToolTip(_project_tooltip(d))
            self.list_b.addItem(item_b)

        # Restore previous selection if we had one (e.g. user clicked Back).
        if self.state.selected_a:
            _select_matching(self.list_a, self.state.selected_a)
        if self.state.selected_b:
            _select_matching(self.list_b, self.state.selected_b)

        # Status line.
        msg = f"Found <b>{len(found)}</b> mod{'s' if len(found) != 1 else ''}"
        if errors:
            msg += f" &nbsp;|&nbsp; <span style='color:#a44;'>" \
                   f"{len(errors)} folder(s) skipped (hover for details)</span>"
        self.status_label.setText(msg)
        if errors:
            tip = "\n".join(f"{e.folder.name}: {e.reason}" for e in errors)
            self.status_label.setToolTip(tip)
        else:
            self.status_label.setToolTip("")

        self._on_selection_changed()

    # --- Selection handling --------------------------------------------

    def _on_selection_changed(self) -> None:
        a_item = self.list_a.currentItem()
        b_item = self.list_b.currentItem()
        self.state.selected_a = (
            a_item.data(Qt.UserRole) if a_item and a_item.isSelected() else None
        )
        self.state.selected_b = (
            b_item.data(Qt.UserRole) if b_item and b_item.isSelected() else None
        )

        if self.state.selected_a and self.state.selected_b:
            # Identity key handles the canonical-workspace case where
            # multiple mods share the same data_root (the workspace).
            if self.state.selected_a.identity_key == self.state.selected_b.identity_key:
                self.preview.setText(
                    "<span style='color:#a44;'><b>Same mod selected on both sides.</b> "
                    "Pick two different mods.</span>"
                )
            else:
                self._render_preview()
        elif self.state.selected_a or self.state.selected_b:
            self.preview.setText("<i>Now pick the other side.</i>")
        else:
            self.preview.setText("<i>Pick mod A and mod B above.</i>")

        self.completeChanged.emit()

    def _render_preview(self) -> None:
        a = self.state.selected_a
        b = self.state.selected_b
        if self.mode_combine.isChecked():
            verb = (f"will be modified to include B's content. "
                    f"Mod B remains untouched.")
            heading = f"<b>{_escape(a.mod_name)}</b> &larr; <b>{_escape(b.mod_name)}</b>"
        else:
            verb = ("will produce a new mod that you'll name on the next "
                    "screen. A and B are not modified.")
            heading = f"<b>{_escape(a.mod_name)}</b> + <b>{_escape(b.mod_name)}</b>"
        self.preview.setText(f"{heading}<br>{verb}")

    def _on_mode_changed(self) -> None:
        # Re-render the preview to reflect the new mode wording.
        if self.state.selected_a and self.state.selected_b:
            self._render_preview()

    # --- Wizard contract -----------------------------------------------

    def isComplete(self) -> bool:
        a = self.state.selected_a
        b = self.state.selected_b
        return (a is not None and b is not None
                and a.identity_key != b.identity_key)

    def validatePage(self) -> bool:
        """Load the full projects from disk on Next.

        The full Project.load() walks every file in the tree (3,000+
        for Treehome). It's the moment we can't defer any longer
        because IdentityPage / ReviewPage / RunPage all need the
        complete Project. We do it here and surface any load error
        with a clear message rather than crashing later.
        """
        a = self.state.selected_a
        b = self.state.selected_b
        if a is None or b is None:
            return False

        # Persist the merge mode into state.
        self.state.merge_mode = (
            "combine_b_into_a" if self.mode_combine.isChecked() else "new_mod"
        )

        # Load both projects. For canonical-workspace mods we MUST
        # pass mod_folder_name to disambiguate (the workspace contains
        # multiple mods and auto-detect would refuse).
        try:
            self.state.project_a = Project.load(
                a.data_root, mod_folder_name=a.mod_folder_name,
            )
            self.state.project_a_path = str(a.data_root)
        except Exception as e:
            QMessageBox.critical(
                self, "Failed to load Mod A",
                f"{type(e).__name__}: {e}",
            )
            return False
        try:
            self.state.project_b = Project.load(
                b.data_root, mod_folder_name=b.mod_folder_name,
            )
            self.state.project_b_path = str(b.data_root)
        except Exception as e:
            QMessageBox.critical(
                self, "Failed to load Mod B",
                f"{type(e).__name__}: {e}",
            )
            return False

        # If in-place mode, lock the identity fields to A's values so
        # the engine knows to keep them. (IdentityPage is skipped in
        # this mode, see MergeWizard.nextId.) Use the loaded Project's
        # ``root`` so the path matches Project.load's resolution exactly
        # (Project.load resolves symlinks; the DiscoveredProject path
        # may not be resolved if the workspace was a symlink itself).
        if self.state.merge_mode == "combine_b_into_a":
            mm = self.state.project_a.mod_meta
            self.state.new_uuid = mm.uuid
            self.state.new_folder = self.state.project_a.mod_folder_name
            self.state.new_name = mm.name
            self.state.new_author = mm.author
            self.state.new_description = mm.description
            self.state.output_dir = str(self.state.project_a.root)
        return True


def _project_tooltip(d: DiscoveredProject) -> str:
    """Long tooltip shown on hover over a list item — full identity."""
    parts = [
        f"Name:    {d.mod_name}",
        f"Author:  {d.author or '(unknown)'}",
        f"Folder:  {d.mod_folder_name}",
        f"UUID:    {d.mod_uuid}",
        f"Path:    {d.project_root}",
    ]
    if d.description:
        # Truncate long descriptions to avoid wall-of-text tooltips.
        desc = d.description.strip().replace("\n", " ")
        if len(desc) > 200:
            desc = desc[:200] + "…"
        parts.append("")
        parts.append(desc)
    return "\n".join(parts)


def _select_matching(list_widget: QListWidget, target: DiscoveredProject) -> None:
    """Re-select the list item whose DiscoveredProject is the same mod
    as ``target`` (used to restore selection after a rescan). Compares
    by ``identity_key`` so it works for canonical-workspace mods that
    share a data_root."""
    for i in range(list_widget.count()):
        item = list_widget.item(i)
        data = item.data(Qt.UserRole)
        if data and data.identity_key == target.identity_key:
            list_widget.setCurrentItem(item)
            return


# ---------------------------------------------------------------------------
# Page 2: identity for the merged mod
# ---------------------------------------------------------------------------


class IdentityPage(QWizardPage):
    """The merged mod's identity: UUID, folder name, display name, author,
    description, plus the output directory.

    Auto-suggests sensible defaults when the page is first shown so the
    user can usually just click Next.
    """

    def __init__(self, state: WizardState) -> None:
        super().__init__()
        self.state = state
        self.setTitle("Identity for the merged mod")
        self.setSubTitle(
            "Pick a name, folder, and UUID for the new mod. "
            "Defaults are pre-filled — you can usually accept them."
        )

        form = QFormLayout(self)

        # UUID with regenerate button.
        self.uuid_edit = QLineEdit()
        self.uuid_edit.setReadOnly(True)
        self.uuid_edit.textChanged.connect(lambda _: self.completeChanged.emit())
        uuid_regen = QPushButton("New UUID")
        uuid_regen.clicked.connect(
            lambda: self.uuid_edit.setText(generate_uuid())
        )
        uuid_row = QHBoxLayout()
        uuid_row.addWidget(self.uuid_edit, 1)
        uuid_row.addWidget(uuid_regen)
        uuid_widget = QWidget()
        uuid_widget.setLayout(uuid_row)
        form.addRow("UUID:", uuid_widget)

        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(lambda _: self.completeChanged.emit())
        form.addRow("Display name:", self.name_edit)

        self.folder_edit = QLineEdit()
        self.folder_edit.textChanged.connect(lambda _: self.completeChanged.emit())
        form.addRow("Folder name:", self.folder_edit)

        self.author_edit = QLineEdit()
        form.addRow("Author:", self.author_edit)

        self.description_edit = QPlainTextEdit()
        self.description_edit.setMaximumHeight(70)
        form.addRow("Description:", self.description_edit)

        # Output preview. The new mod always lands in the user's
        # configured workspace (so the Toolkit can see it). We don't
        # offer a free-form output dir picker any more — the wizard's
        # workspace setup is authoritative.
        self.output_preview = QLabel()
        self.output_preview.setWordWrap(True)
        self.output_preview.setTextFormat(Qt.RichText)
        self.output_preview.setStyleSheet(
            "QLabel { padding: 6px; background: palette(alternate-base); "
            "border: 1px solid palette(mid); border-radius: 4px; "
            "font-family: monospace; font-size: 11px; }"
        )
        form.addRow("Will be created at:", self.output_preview)

    def initializePage(self) -> None:
        """Called by QWizard when the page is shown. Fills defaults
        derived from the projects picked on the previous page."""
        a, b = self.state.project_a, self.state.project_b
        if a is None or b is None:
            return

        if not self.state.new_uuid:
            self.state.new_uuid = generate_uuid()
        self.uuid_edit.setText(self.state.new_uuid)

        suggested_folder = self.state.new_folder or _default_folder(a, b, self.state.new_uuid)
        self.folder_edit.setText(suggested_folder)

        suggested_name = self.state.new_name or f"{a.mod_meta.name} + {b.mod_meta.name}"
        self.name_edit.setText(suggested_name)

        self.author_edit.setText(self.state.new_author or _default_author(a, b))
        self.description_edit.setPlainText(self.state.new_description)

        # Output is always the workspace (where the Toolkit can see it).
        # Use mod A's data_root since A and B share a workspace in the
        # common case. If they don't (e.g. one self-contained + one
        # canonical), prefer the workspace from settings.
        if self.state.settings.workspace_dir:
            self.state.output_dir = self.state.settings.workspace_dir
        else:
            self.state.output_dir = str(a.root)

        self._refresh_output_preview()
        # Live preview update as the folder name changes.
        self.folder_edit.textChanged.connect(lambda _: self._refresh_output_preview())

    def _refresh_output_preview(self) -> None:
        folder = self.folder_edit.text().strip() or "&lt;your folder name&gt;"
        ws = self.state.output_dir
        self.output_preview.setText(
            f"{_escape(ws)}/Editor/Mods/<b>{folder}</b>/<br>"
            f"{_escape(ws)}/Mods/<b>{folder}</b>/<br>"
            f"{_escape(ws)}/Public/<b>{folder}</b>/<br>"
            f"{_escape(ws)}/Projects/<b>{folder}</b>/"
        )

    def isComplete(self) -> bool:
        return (
            bool(self.uuid_edit.text().strip())
            and bool(self.name_edit.text().strip())
            and bool(self.folder_edit.text().strip())
        )

    def validatePage(self) -> bool:
        """Called when the user clicks Next. Persists the page's fields
        into the shared state and checks the new folder name doesn't
        collide with an existing mod in the workspace."""
        self.state.new_uuid = self.uuid_edit.text().strip()
        self.state.new_name = self.name_edit.text().strip()
        self.state.new_folder = self.folder_edit.text().strip()
        self.state.new_author = self.author_edit.text().strip()
        self.state.new_description = self.description_edit.toPlainText().strip()
        # output_dir was set in initializePage; preserve it here.

        # Collision check: the new mod's bucket subfolders shouldn't
        # exist already.
        od = Path(self.state.output_dir)
        collisions = []
        for bucket in ("Editor/Mods", "Mods", "Public", "Projects"):
            target = od / bucket / self.state.new_folder
            if target.exists():
                collisions.append(str(target))
        if collisions:
            QMessageBox.critical(
                self, "Mod folder already exists",
                f"The folder name {self.state.new_folder!r} is already used "
                f"by another mod in your workspace:\n\n"
                + "\n".join(collisions)
                + "\n\nPick a different folder name."
            )
            return False
        return True


# ---------------------------------------------------------------------------
# Page 3: conflict policy
# ---------------------------------------------------------------------------


class PolicyPage(QWizardPage):
    """Pick how identifier clashes get resolved."""

    def __init__(self, state: WizardState) -> None:
        super().__init__()
        self.state = state
        self.setTitle("Conflict policy")
        self.setSubTitle(
            "What should happen when both projects define the same identifier?"
        )

        layout = QVBoxLayout(self)

        self.policy_combo = QComboBox()
        self.policy_combo.addItem("Skip — Project A wins, Project B's drops", "skip")
        self.policy_combo.addItem("Prefix — rename Project B's identifier", "prefix")
        self.policy_combo.addItem("Fail — abort the merge on any clash", "fail")
        self.policy_combo.currentIndexChanged.connect(self._on_policy_changed)

        row = QHBoxLayout()
        row.addWidget(QLabel("Policy:"))
        row.addWidget(self.policy_combo, 1)
        layout.addLayout(row)

        self.prefix_label = QLabel("Prefix string for renamed identifiers:")
        layout.addWidget(self.prefix_label)
        self.prefix_edit = QLineEdit()
        layout.addWidget(self.prefix_edit)

        explanation = QLabel(
            "<i>"
            "Most real-world merges between non-overlapping mods produce no "
            "conflicts; the policy only kicks in for the genuinely shared "
            "identifiers. CanMerge-flagged treasure tables are handled "
            "automatically and aren't affected by this setting."
            "</i>"
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        layout.addStretch(1)

    def initializePage(self) -> None:
        policy = self.state.conflict_policy or self.state.settings.default_conflict_policy
        idx = self.policy_combo.findData(policy)
        self.policy_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.prefix_edit.setText(
            self.state.conflict_prefix or self.state.settings.default_conflict_prefix
        )
        self._on_policy_changed()

    def _on_policy_changed(self) -> None:
        is_prefix = self.policy_combo.currentData() == "prefix"
        self.prefix_label.setEnabled(is_prefix)
        self.prefix_edit.setEnabled(is_prefix)

    def validatePage(self) -> bool:
        self.state.conflict_policy = self.policy_combo.currentData()
        self.state.conflict_prefix = self.prefix_edit.text().strip()
        if self.state.conflict_policy == "prefix" and not self.state.conflict_prefix:
            QMessageBox.warning(
                self, "Prefix required",
                "Prefix policy needs a non-empty prefix string.",
            )
            return False
        # Save as new defaults for next time.
        self.state.settings.default_conflict_policy = self.state.conflict_policy
        if self.state.conflict_prefix:
            self.state.settings.default_conflict_prefix = self.state.conflict_prefix
        return True


# ---------------------------------------------------------------------------
# Page 4: review
# ---------------------------------------------------------------------------


class ReviewPage(QWizardPage):
    """Build reference indexes for both inputs, run clash detection, and
    show the user what will happen before any files are written."""

    def __init__(self, state: WizardState) -> None:
        super().__init__()
        self.state = state
        self.setTitle("Review")
        self.setSubTitle(
            "A summary of what the merge will do. "
            "Click Next to start writing files."
        )

        layout = QVBoxLayout(self)
        self.summary = QTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setFont(QFont("monospace"))
        layout.addWidget(self.summary)

    def initializePage(self) -> None:
        # Defer the heavy work (index build) by a tick so the UI thread
        # paints the page header first. For our fixture sizes this is
        # ~50ms even on the largest projects so it's tolerable inline,
        # but the QTimer hop keeps the wizard feeling snappy.
        self.summary.setPlainText("Building reference index for input A…")
        QTimer.singleShot(0, self._build_summary)

    def _build_summary(self) -> None:
        from core.references import ReferenceIndex, find_clashes
        a, b = self.state.project_a, self.state.project_b
        if a is None or b is None:
            self.summary.setPlainText("(missing projects — go back and re-pick)")
            return

        ia = ReferenceIndex.build(a)
        ib = ReferenceIndex.build(b)
        clashes = find_clashes(ia, ib)

        lines: list[str] = []
        if self.state.merge_mode == "combine_b_into_a":
            lines.append(
                f"Mode: COMBINE INTO A  (mod {a.mod_meta.name!r} will be "
                f"modified in place)"
            )
            lines.append(
                f"  Target folder : {self.state.output_dir}"
            )
            lines.append(
                "  Safety        : merge writes to a temp sibling first; "
                "the target is replaced atomically on success"
            )
        else:
            lines.append(f"Mode: MAKE NEW MOD")
            lines.append(f"  Name    : {self.state.new_name!r}")
            lines.append(f"  UUID    : {self.state.new_uuid}")
            lines.append(f"  Folder  : {self.state.new_folder}")
            lines.append(f"  Output  : {self.state.output_dir}")
        lines.append(
            f"  Policy  : {self.state.conflict_policy}"
            + (f" (prefix={self.state.conflict_prefix!r})"
               if self.state.conflict_policy == "prefix" else "")
        )
        lines.append("")
        lines.append(f"Input A: {a.mod_meta.name}  ({len(a.files)} files)")
        lines.append(f"Input B: {b.mod_meta.name}  ({len(b.files)} files)")
        lines.append("")

        if not clashes:
            lines.append("Identifier clashes: NONE.")
            lines.append("This is a clean union — no user choices required.")
        else:
            lines.append(f"Identifier clashes: {len(clashes)}")
            # Group by kind for readability.
            from collections import defaultdict
            by_kind: dict[str, list[str]] = defaultdict(list)
            for c in clashes:
                by_kind[c.kind.value].append(c.value)
            for kind in sorted(by_kind):
                lines.append(f"  [{kind}]: {len(by_kind[kind])}")
                for v in by_kind[kind][:10]:
                    lines.append(f"      {v}")
                if len(by_kind[kind]) > 10:
                    lines.append(f"      … and {len(by_kind[kind]) - 10} more")

            lines.append("")
            if self.state.conflict_policy == "skip":
                lines.append(
                    "With policy=skip: Project A's definitions win; "
                    "Project B's clashing entries will be dropped."
                )
            elif self.state.conflict_policy == "prefix":
                lines.append(
                    f"With policy=prefix: Project B's clashing identifiers "
                    f"will be renamed with the prefix {self.state.conflict_prefix!r}."
                )
            elif self.state.conflict_policy == "fail":
                lines.append(
                    "With policy=fail: the merge will abort. "
                    "Go back and pick skip or prefix to proceed."
                )

        self.summary.setPlainText("\n".join(lines))


# ---------------------------------------------------------------------------
# Page 5: run
# ---------------------------------------------------------------------------


class RunPage(QWizardPage):
    """Runs the merge in a worker thread. Progress bar + streaming log.

    The user can't navigate while a merge is in progress; once it finishes
    (success or failure) the wizard advances to the result page.
    """

    def __init__(self, state: WizardState) -> None:
        super().__init__()
        self.state = state
        self.setTitle("Merging…")
        self.setSubTitle("Hang on, writing files.")

        layout = QVBoxLayout(self)

        self.phase_label = QLabel("Starting…")
        layout.addWidget(self.phase_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # indeterminate until first signal
        layout.addWidget(self.progress_bar)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)  # cap the log size
        self.log.setFont(QFont("monospace"))
        layout.addWidget(self.log, 1)

        self.worker: MergeWorker | None = None
        self._done = False

    def initializePage(self) -> None:
        self._done = False
        self.log.clear()
        # Disable Back / Next while merging.
        wizard = self.wizard()
        for btn in (QWizard.BackButton, QWizard.NextButton, QWizard.CancelButton):
            b = wizard.button(btn)
            if b is not None:
                b.setEnabled(False)

        # Build MergeConfig and spawn the worker. The merge mode picked
        # on the SelectionPage maps directly onto MergeConfig.in_place.
        # If the user configured a divine.exe path, instantiate a bound
        # Divine and pass it through so LSF round-tripping (currently
        # used for GUI/metadata.lsf structural merge) works.
        divine_obj = None
        divine_path = self.state.settings.divine_path.strip()
        if divine_path:
            try:
                from core.divine import Divine, find_divine
                divine_obj = Divine(exe_path=find_divine(divine_path))
            except Exception:
                # Bad path → silently fall back to no-divine mode. The
                # merger will surface the consequences (e.g. keep-A for
                # GUI metadata) in its summary.
                divine_obj = None

        config = merger.MergeConfig(
            inputs=[self.state.project_a, self.state.project_b],
            output_dir=Path(self.state.output_dir),
            new_uuid=self.state.new_uuid,
            new_folder=self.state.new_folder,
            new_name=self.state.new_name,
            new_author=self.state.new_author,
            new_description=self.state.new_description,
            conflict_policy=self.state.conflict_policy,
            conflict_prefix=self.state.conflict_prefix,
            in_place=(self.state.merge_mode == "combine_b_into_a"),
            divine=divine_obj,
        )
        self.worker = MergeWorker(config)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_with_result.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    # --- Slot handlers ---------------------------------------------------

    def _on_progress(self, phase: str, current: int, total: int, detail: str) -> None:
        self.phase_label.setText(f"<b>{_phase_label(phase)}</b>")
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
        else:
            self.progress_bar.setRange(0, 0)  # indeterminate
        if detail:
            self.log.appendPlainText(f"[{phase}] {detail}")

    def _on_finished(self, result: object) -> None:
        self.state.merge_result = result  # type: ignore[assignment]
        self.state.validation_report = validate.validate(result.new_project)
        self._done = True
        self.log.appendPlainText("")
        self.log.appendPlainText("=== Merge complete ===")
        self.completeChanged.emit()
        # Re-enable navigation; jump forward automatically.
        wizard = self.wizard()
        for btn in (QWizard.BackButton, QWizard.NextButton, QWizard.CancelButton):
            b = wizard.button(btn)
            if b is not None:
                b.setEnabled(True)
        # Auto-advance after a brief pause so the user can see the success.
        QTimer.singleShot(500, wizard.next)

    def _on_failed(self, tb: str) -> None:
        self.state.merge_error = tb
        self._done = True
        self.log.appendPlainText("")
        self.log.appendPlainText("=== Merge FAILED ===")
        self.log.appendPlainText(tb)
        self.completeChanged.emit()
        wizard = self.wizard()
        for btn in (QWizard.BackButton, QWizard.CancelButton):
            b = wizard.button(btn)
            if b is not None:
                b.setEnabled(True)
        # We still let the user proceed to the result page so they can
        # see the error in context.
        next_btn = wizard.button(QWizard.NextButton)
        if next_btn is not None:
            next_btn.setEnabled(True)

    def isComplete(self) -> bool:
        return self._done


# ---------------------------------------------------------------------------
# Page 6: result
# ---------------------------------------------------------------------------


class ResultPage(QWizardPage):
    """Final page — shows summary of what happened plus an Open button."""

    def __init__(self, state: WizardState) -> None:
        super().__init__()
        self.state = state
        self.setTitle("Done")
        self.setSubTitle("")
        self.setFinalPage(True)

        layout = QVBoxLayout(self)
        self.summary = QTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setFont(QFont("monospace"))
        layout.addWidget(self.summary, 1)

        button_row = QHBoxLayout()
        self.open_button = QPushButton("Open output folder")
        self.open_button.clicked.connect(self._open_output)
        button_row.addWidget(self.open_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

    def initializePage(self) -> None:
        if self.state.merge_error:
            self.setSubTitle("Merge failed.")
            self.open_button.setEnabled(False)
            self.summary.setPlainText(
                "The merge did not complete. See the traceback below.\n\n"
                + self.state.merge_error
            )
            return

        result = self.state.merge_result
        if result is None:
            self.summary.setPlainText("(no result — this shouldn't happen)")
            return

        report = self.state.validation_report

        lines: list[str] = []
        lines.append(f"Output directory: {result.output_dir}")
        lines.append(f"Files written   : {len(result.emissions)}")
        lines.append(f"Files skipped   : {len(result.skipped_files)} "
                     f"(compiled-story artifacts the Toolkit will regenerate)")
        lines.append("")

        id_conflicts = [c for c in result.conflicts if c.kind != "file_overlap"]
        file_overlaps = [c for c in result.conflicts if c.kind == "file_overlap"]
        if not id_conflicts and not file_overlaps:
            lines.append("No conflicts: clean union.")
        else:
            if id_conflicts:
                lines.append(f"Identifier conflicts: {len(id_conflicts)}")
                for c in id_conflicts[:20]:
                    lines.append(f"  [{c.kind}] {c.identifier} -> {c.resolution}")
                if len(id_conflicts) > 20:
                    lines.append(f"  … and {len(id_conflicts) - 20} more")
                lines.append("")
            if file_overlaps:
                lines.append(f"File overlaps with different content: {len(file_overlaps)}")
                lines.append("(Project A's version was kept; B's was discarded.)")
                for c in file_overlaps[:20]:
                    lines.append(f"  {c.identifier}")
                if len(file_overlaps) > 20:
                    lines.append(f"  … and {len(file_overlaps) - 20} more")

        if report is not None:
            lines.append("")
            lines.append("--- Validation ---")
            if report.is_clean():
                lines.append("No issues found.")
            else:
                if report.is_blocked():
                    lines.append("WARNING: definition collisions were detected.")
                    for kind, entries in sorted(report.definition_collisions.items()):
                        lines.append(f"  [{kind}]: {len(entries)} collision(s)")
                for kind, entries in sorted(report.orphan_references.items()):
                    lines.append(
                        f"Orphan {kind} references: {len(entries)} "
                        f"(usually base-game refs; not an error)"
                    )
                if report.unreferenced_dependencies:
                    lines.append(
                        f"Unreferenced dependencies: "
                        f"{len(report.unreferenced_dependencies)} (informational)"
                    )

        self.summary.setPlainText("\n".join(lines))

    def _open_output(self) -> None:
        path = Path(self.state.output_dir)
        if not path.exists():
            QMessageBox.warning(
                self, "Output not found",
                f"{path} doesn't exist (perhaps the merge failed).",
            )
            return
        # Platform-appropriate "open in file manager".
        try:
            if sys.platform == "win32":
                # Use os.startfile for Windows file-manager open.
                import os
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except OSError as e:
            QMessageBox.warning(self, "Could not open", str(e))


# ---------------------------------------------------------------------------
# Wizard itself
# ---------------------------------------------------------------------------


class MergeWizard(QWizard):
    """The top-level QWizard. Owns the shared state and persists settings
    on close.

    Page flow:
        0  WorkspacePage    set workspace + divine.exe paths
        1  SelectionPage    pick mods + choose merge mode
        2  IdentityPage     (skipped in "combine B into A" mode)
        3  PolicyPage       conflict policy
        4  ReviewPage       summary before writing
        5  RunPage          progress + log
        6  ResultPage       final summary

    The skip is implemented via ``nextId()`` on SelectionPage so the Back
    button still works correctly (QWizard knows about the conditional
    edge in the page graph).
    """

    # Stable page IDs — these are what we return from nextId() and read
    # from currentId(). Using named constants keeps the conditional flow
    # readable and avoids brittle integer indexing in tests.
    PAGE_WORKSPACE = 0
    PAGE_SELECTION = 1
    PAGE_IDENTITY = 2
    PAGE_POLICY = 3
    PAGE_REVIEW = 4
    PAGE_RUN = 5
    PAGE_RESULT = 6

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("BG3 Mod Merger")
        self.setOption(QWizard.IndependentPages, False)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.resize(820, 600)

        self.state = WizardState(settings=app_settings.load())

        geom = self.state.settings.window_geometry
        if geom:
            try:
                from PySide6.QtCore import QByteArray
                self.restoreGeometry(QByteArray.fromBase64(geom.encode("ascii")))
            except Exception:
                pass

        # setPage(id, page) lets us pin specific IDs to specific pages
        # so the constants above match reality regardless of insertion
        # order or future page additions.
        self.setPage(self.PAGE_WORKSPACE, WorkspacePage(self.state))
        self.setPage(self.PAGE_SELECTION, SelectionPage(self.state))
        self.setPage(self.PAGE_IDENTITY, IdentityPage(self.state))
        self.setPage(self.PAGE_POLICY, PolicyPage(self.state))
        self.setPage(self.PAGE_REVIEW, ReviewPage(self.state))
        self.setPage(self.PAGE_RUN, RunPage(self.state))
        self.setPage(self.PAGE_RESULT, ResultPage(self.state))

        # Entry point.
        self.setStartId(self.PAGE_WORKSPACE)

    def nextId(self) -> int:
        """Override the linear next-page logic to skip IdentityPage when
        the user picked "Combine B into A" — there's nothing for them to
        configure in that mode (A's identity is locked).

        QWizard calls this on the *current* page; we return -1 to signal
        "no more pages" or any valid page ID to go there.
        """
        cur = self.currentId()
        if cur == self.PAGE_SELECTION:
            if self.state.merge_mode == "combine_b_into_a":
                return self.PAGE_POLICY      # skip identity
            return self.PAGE_IDENTITY
        # Default: walk to the next sequential page.
        ordered = [
            self.PAGE_WORKSPACE, self.PAGE_SELECTION, self.PAGE_IDENTITY,
            self.PAGE_POLICY, self.PAGE_REVIEW, self.PAGE_RUN, self.PAGE_RESULT,
        ]
        try:
            i = ordered.index(cur)
        except ValueError:
            return -1
        if i + 1 < len(ordered):
            return ordered[i + 1]
        return -1

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            self.state.settings.window_geometry = bytes(
                self.saveGeometry().toBase64()
            ).decode("ascii")
        except Exception:
            pass
        app_settings.save(self.state.settings)
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PHASE_LABELS = {
    "detect": "Detecting clashes",
    "plan": "Planning remaps",
    "emit": "Writing files",
    "validate": "Validating output",
}


def _phase_label(phase: str) -> str:
    return _PHASE_LABELS.get(phase, phase.capitalize())


def _escape(s: str) -> str:
    """Tiny HTML-escape for label rendering. We don't need a full HTML
    parser — just the four characters that break rich-text labels."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _default_folder(a: Project, b: Project, uuid: str) -> str:
    """A sensible default Toolkit folder name for the merged mod.

    Strategy: take the leading portion of each input's folder (up to the
    first underscore, which is where the Toolkit convention puts the UUID)
    and concatenate with ``_Plus_``, then append a short UUID-prefix so
    the folder name is unique across merges. Falls back to a UUID-only
    folder if either input's name is empty.
    """
    a_root = a.mod_folder_name.split("_")[0] if a.mod_folder_name else "ModA"
    b_root = b.mod_folder_name.split("_")[0] if b.mod_folder_name else "ModB"
    return f"{a_root}_Plus_{b_root}_{uuid.replace('-', '')[:8]}"


def _default_author(a: Project, b: Project) -> str:
    """If a single non-empty author is shared, default to that.
    Otherwise (multiple distinct named authors), leave blank for the user
    to fill in. Empty-string authors are ignored so ``("Alice", "")``
    defaults to ``"Alice"`` rather than blank."""
    authors = {a.mod_meta.author, b.mod_meta.author}
    authors.discard("")
    if len(authors) == 1:
        return authors.pop()
    return ""
