"""Tests for the GUI layer.

Run headless under ``QT_QPA_PLATFORM=offscreen``. Pytest fixtures handle
the QApplication lifecycle so we don't leak Qt singletons across tests.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

# Force offscreen platform BEFORE importing PySide6 widgets.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QWizard

from core import merger
from core.discover import DiscoveredProject
from core.project import Project
from gui import settings as app_settings
from gui.wizard import (
    AddIconDialog,
    IdentityPage, MergeWizard, PolicyPage, ResultPage, ReviewPage, SelectionPage,
    WizardState, WorkspacePage, _default_folder, _default_author,
)
from gui.worker import MergeWorker

from .helpers import FIXTURES


# --- Qt lifecycle fixture ---------------------------------------------------


@pytest.fixture(scope="session")
def qapp():
    """One QApplication shared across the whole test session."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_app_settings(tmp_path_factory, monkeypatch):
    """Every test gets an empty, throwaway settings file.

    Without this, GUI tests that build a ``MergeWizard()`` pick up the
    real user's settings file from ``~/.config/bg3_mod_merger/``:
    which leads to false failures when ``test_wizard_full_drive_*``
    leaves a workspace_dir saved that later tests inherit.

    Scoped per-test so each test sees a clean slate; the underlying
    directory survives the session but the file path itself is unique
    per test.
    """
    settings_dir = tmp_path_factory.mktemp("app_settings_isolated")
    monkeypatch.setattr(
        app_settings, "SETTINGS_PATH",
        settings_dir / "settings.json",
    )
    yield


# --- Settings ---------------------------------------------------------------


def test_settings_defaults_load_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    assert app_settings.load() == app_settings.Settings()


def test_settings_save_then_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    s = app_settings.Settings(
        divine_path=r"C:\Tools\LSLib\divine.exe",
        workspace_dir="/home/me/workspace",
        default_conflict_policy="prefix",
        default_conflict_prefix="MyMod_",
    )
    app_settings.save(s)
    assert app_settings.load() == s


def test_settings_load_tolerates_corrupt_json(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", path)
    path.write_text("not json")
    assert app_settings.load() == app_settings.Settings()


def test_settings_load_tolerates_missing_keys(tmp_path, monkeypatch):
    """Older saved files (pre-workspace_dir) still load."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", path)
    path.write_text(json.dumps({"divine_path": "/some/path"}))
    s = app_settings.load()
    assert s.divine_path == "/some/path"
    assert s.workspace_dir == ""


# --- Wizard construction ----------------------------------------------------


def test_wizard_constructs_with_seven_pages(qapp):
    """The redesigned wizard has 7 pages: workspace, selection, identity,
    policy, review, run, result."""
    w = MergeWizard()
    # Title has leading spaces to keep Windows from clipping the
    # trailing "|" with the close button.
    assert w.windowTitle() == "    | BG3 Mod Merger | by For_Kiramay |"
    assert len(w.pageIds()) == 7
    # With clean settings (the autouse isolation fixture gives every
    # test an empty file), the workspace isn't configured yet: first
    # run lands on WorkspacePage.
    assert w.startId() == w.PAGE_WORKSPACE


def test_wizard_skips_workspace_when_settings_already_configured(
    qapp, tmp_path,
):
    """Returning users with a saved workspace that still exists should
    land directly on SelectionPage. The "Settings…" button on that page
    is the way back to re-edit WorkspacePage."""
    # Pre-populate a saved settings file pointing at an existing dir.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    s = app_settings.Settings(workspace_dir=str(workspace))
    app_settings.save(s)
    w = MergeWizard()
    assert w.startId() == w.PAGE_SELECTION


def test_wizard_falls_back_to_workspace_when_saved_dir_is_missing(
    qapp, tmp_path,
):
    """If the saved workspace_dir no longer exists (drive unplugged,
    folder renamed), don't dump the user onto SelectionPage where the
    scan would fail. Send them back to WorkspacePage to fix it first.
    """
    s = app_settings.Settings(workspace_dir=str(tmp_path / "does_not_exist"))
    app_settings.save(s)
    w = MergeWizard()
    assert w.startId() == w.PAGE_WORKSPACE


def test_selection_page_has_settings_button(qapp, tmp_path):
    """The Settings button is the *only* way back to WorkspacePage on a
    returning launch (since Back is disabled on the start page). It
    must exist on SelectionPage and clicking it should not raise."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    s = app_settings.Settings(workspace_dir=str(workspace))
    app_settings.save(s)
    w = MergeWizard()
    # We're on SELECTION as the start page.
    assert w.startId() == w.PAGE_SELECTION
    page = w.page(w.PAGE_SELECTION)
    assert hasattr(page, "settings_button"), (
        "SelectionPage must expose a settings_button for re-entry into WorkspacePage"
    )
    # Click it. After click, the wizard's startId should point at
    # WorkspacePage (we used restart() with a new startId).
    page._open_settings()
    assert w.startId() == w.PAGE_WORKSPACE


def test_wizard_relaunch_flag_defaults_false(qapp):
    """Without a completed merge, closing the wizard shouldn't trigger
    a relaunch. (The flag is the signal __main__.py checks after
    app.exec returns.)"""
    w = MergeWizard()
    assert w.relaunch_after_exit is False


def test_wizard_relaunch_flag_set_on_successful_merge(qapp):
    """A successful Finish click flips relaunch_after_exit so __main__.py
    spawns a fresh process. Simulated by populating the state and
    firing the accepted signal directly. We use a non-None sentinel for
    merge_result rather than building a full MergeResult: the flag
    check is just ``is not None``."""
    w = MergeWizard()
    # Pretend a merge ran and succeeded. The accepted-signal handler
    # only inspects ``state.merge_result is not None`` and
    # ``not state.merge_error``.
    w.state.merge_result = object()  # sentinel; anything non-None works
    w.state.merge_error = ""
    # Trigger the slot QWizard would call on Finish.
    w._on_accepted()
    assert w.relaunch_after_exit is True


def test_wizard_relaunch_flag_not_set_when_merge_failed(qapp):
    """A failed merge that the user clicked Finish past shouldn't trigger
    a relaunch: they might want to step away and debug, not loop right
    back into a broken state."""
    w = MergeWizard()
    w.state.merge_result = None
    w.state.merge_error = "Something went wrong"
    w._on_accepted()
    assert w.relaunch_after_exit is False


def test_wizard_state_is_shared_across_all_pages(qapp):
    """All pages must share one WizardState instance: that's how
    merge_mode flows from SelectionPage to RunPage."""
    w = MergeWizard()
    states = {id(w.page(pid).state) for pid in w.pageIds()}
    assert len(states) == 1


# --- WorkspacePage ----------------------------------------------------------


def test_workspace_page_isComplete_requires_existing_directory(qapp, tmp_path):
    state = WizardState(settings=app_settings.Settings())
    page = WorkspacePage(state)
    page.initializePage()
    assert page.isComplete() is False  # empty

    page.workspace_edit.setText(str(tmp_path / "doesnt_exist"))
    assert page.isComplete() is False  # path doesn't exist

    real_dir = tmp_path / "exists"
    real_dir.mkdir()
    page.workspace_edit.setText(str(real_dir))
    assert page.isComplete() is True


def test_workspace_page_saves_to_settings_on_validate(qapp, tmp_path, monkeypatch):
    """Validate immediately persists, so closing without finishing the
    merge still leaves the workspace setting saved."""
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", settings_file)

    state = WizardState(settings=app_settings.Settings())
    page = WorkspacePage(state)
    page.workspace_edit.setText(str(tmp_path))

    assert page.validatePage() is True
    assert state.settings.workspace_dir == str(tmp_path)
    assert settings_file.is_file()
    reloaded = json.loads(settings_file.read_text())
    assert reloaded["workspace_dir"] == str(tmp_path)


def test_workspace_page_preserves_divine_override_through_validate(qapp, tmp_path, monkeypatch):
    """The divine path UI was removed (LSLib is bundled), but the
    settings field still exists and is honoured by the runtime when
    set. A power user who edits settings.json directly must NOT have
    their override silently wiped just by walking through the wizard."""
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", settings_file)

    state = WizardState(settings=app_settings.Settings(
        divine_path="/my/custom/divine.exe",
    ))
    page = WorkspacePage(state)
    page.workspace_edit.setText(str(tmp_path))

    assert page.validatePage() is True
    assert state.settings.divine_path == "/my/custom/divine.exe"


def test_workspace_page_initializes_from_settings(qapp, tmp_path):
    """Returning users see their saved workspace pre-filled."""
    settings = app_settings.Settings(
        workspace_dir=str(tmp_path),
        divine_path="/tools/divine.exe",  # set but no longer surfaced in UI
    )
    state = WizardState(settings=settings)
    page = WorkspacePage(state)
    page.initializePage()
    assert page.workspace_edit.text() == str(tmp_path)


# --- SelectionPage ----------------------------------------------------------


def test_selection_page_discovers_workspace_projects(qapp):
    """Pointing at the fixtures workspace finds all five projects."""
    settings = app_settings.Settings(workspace_dir=str(FIXTURES))
    state = WizardState(settings=settings)
    page = SelectionPage(state)
    page._rescan()
    assert page.list_a.count() == 5
    assert page.list_b.count() == 5
    assert len(state.discovered) == 5
    names = sorted(d.mod_name for d in state.discovered)
    assert "Shadow Dance" in names
    assert "Treehome - Persistent Player Housing" in names


def test_selection_page_isComplete_requires_two_different_mods(qapp):
    settings = app_settings.Settings(workspace_dir=str(FIXTURES))
    state = WizardState(settings=settings)
    page = SelectionPage(state)
    page._rescan()
    assert page.isComplete() is False

    page.list_a.setCurrentRow(0)
    page.list_b.setCurrentRow(0)
    page._on_selection_changed()
    assert page.isComplete() is False  # same mod both sides

    page.list_b.setCurrentRow(1)
    page._on_selection_changed()
    assert page.isComplete() is True


def test_selection_page_validate_loads_full_projects(qapp):
    """validatePage transitions from lightweight DiscoveredProject to a
    fully loaded Project (walks every file in the tree)."""
    settings = app_settings.Settings(workspace_dir=str(FIXTURES))
    state = WizardState(settings=settings)
    page = SelectionPage(state)
    page._rescan()
    for i in range(page.list_a.count()):
        if page.list_a.item(i).data(Qt.UserRole).mod_name == "Shadow Dance":
            page.list_a.setCurrentRow(i)
    for i in range(page.list_b.count()):
        if page.list_b.item(i).data(Qt.UserRole).mod_name == "Shadowdancer":
            page.list_b.setCurrentRow(i)
    page._on_selection_changed()
    assert page.validatePage() is True
    assert state.project_a.mod_meta.name == "Shadow Dance"
    assert state.project_b.mod_meta.name == "Shadowdancer"


def test_selection_page_combine_mode_locks_identity_to_a(qapp):
    """In 'Combine B into A' mode, the merged mod's identity is taken
    from A and output_dir is A's project root."""
    settings = app_settings.Settings(workspace_dir=str(FIXTURES))
    state = WizardState(settings=settings)
    page = SelectionPage(state)
    page._rescan()
    for i in range(page.list_a.count()):
        if page.list_a.item(i).data(Qt.UserRole).mod_name == "Shadow Dance":
            page.list_a.setCurrentRow(i)
    for i in range(page.list_b.count()):
        if page.list_b.item(i).data(Qt.UserRole).mod_name == "Shadowdancer":
            page.list_b.setCurrentRow(i)
    page._on_selection_changed()
    page.mode_combine.setChecked(True)
    assert page.validatePage() is True
    assert state.merge_mode == "combine_b_into_a"
    assert state.new_uuid == state.project_a.mod_meta.uuid
    assert state.new_folder == state.project_a.mod_folder_name
    assert state.new_name == state.project_a.mod_meta.name
    assert state.output_dir == str(state.project_a.root)


def test_selection_page_no_workspace_shows_error(qapp):
    """If workspace_dir is unset, the page surfaces a clear error rather
    than crashing."""
    state = WizardState(settings=app_settings.Settings())  # no workspace
    page = SelectionPage(state)
    page._rescan()
    assert page.list_a.count() == 0
    assert "No workspace" in page.status_label.text()


def test_selection_page_finds_canonical_workspace_mods(qapp, tmp_path):
    """The picker should list mods from a canonical Toolkit workspace,
    where each mod is a subfolder under Editor/Mods, Mods, Public,
    Projects rather than a self-contained directory at workspace root."""
    # Build a small canonical workspace from two fixtures.
    for fix in ["ShadowDance", "Shadowdancer"]:
        src = FIXTURES / fix
        for bucket in ["Editor/Mods", "Mods", "Public", "Projects"]:
            src_bucket = src / bucket
            if not src_bucket.exists():
                continue
            for sub in src_bucket.iterdir():
                if sub.is_dir():
                    dst = tmp_path / bucket / sub.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(sub, dst, symlinks=False)

    settings = app_settings.Settings(workspace_dir=str(tmp_path))
    state = WizardState(settings=settings)
    page = SelectionPage(state)
    page._rescan()
    assert page.list_a.count() == 2
    names = sorted(state.discovered[i].mod_name for i in range(2))
    assert names == ["Shadow Dance", "Shadowdancer"]
    # Both mods share data_root (the workspace) but have different
    # identity keys.
    a = state.discovered[0]
    b = state.discovered[1]
    assert a.data_root == b.data_root
    assert a.identity_key != b.identity_key


def test_selection_page_validates_with_shared_data_root(qapp, tmp_path):
    """Two mods in the same canonical workspace share data_root.
    SelectionPage's validate must use identity_key, not project_root,
    or it would (incorrectly) decide they're the same mod."""
    for fix in ["ShadowDance", "Shadowdancer"]:
        src = FIXTURES / fix
        for bucket in ["Editor/Mods", "Mods", "Public", "Projects"]:
            src_bucket = src / bucket
            if not src_bucket.exists():
                continue
            for sub in src_bucket.iterdir():
                if sub.is_dir():
                    dst = tmp_path / bucket / sub.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(sub, dst, symlinks=False)

    settings = app_settings.Settings(workspace_dir=str(tmp_path))
    state = WizardState(settings=settings)
    page = SelectionPage(state)
    page._rescan()
    page.list_a.setCurrentRow(0)
    page.list_b.setCurrentRow(1)
    page._on_selection_changed()
    assert page.isComplete() is True
    assert page.validatePage() is True
    # Project.load worked despite the shared workspace path.
    assert state.project_a is not None
    assert state.project_b is not None
    assert state.project_a.mod_meta.name != state.project_b.mod_meta.name


# --- Conditional flow: IdentityPage skipped in combine mode -----------------


def test_wizard_skips_identity_page_in_combine_mode(qapp):
    """When merge_mode is combine_b_into_a, SelectionPage's next page is
    PolicyPage (IdentityPage is bypassed)."""
    w = MergeWizard()
    w.show()  # required for currentId() to work; offscreen plugin is fine
    try:
        w.state.merge_mode = "combine_b_into_a"
        w.setCurrentId(MergeWizard.PAGE_SELECTION)
        assert w.nextId() == MergeWizard.PAGE_POLICY
    finally:
        w.close()


def test_wizard_shows_identity_page_in_new_mod_mode(qapp):
    w = MergeWizard()
    w.show()
    try:
        w.state.merge_mode = "new_mod"
        w.setCurrentId(MergeWizard.PAGE_SELECTION)
        assert w.nextId() == MergeWizard.PAGE_IDENTITY
    finally:
        w.close()


# --- IdentityPage (unchanged from before, sanity coverage) ------------------


def test_identity_page_initialize_fills_defaults(qapp, tmp_path):
    state = WizardState(settings=app_settings.Settings(workspace_dir=str(tmp_path)))
    state.project_a = Project.load(FIXTURES / "ShadowDance")
    state.project_b = Project.load(FIXTURES / "Shadowdancer")
    page = IdentityPage(state)
    page.initializePage()
    assert len(page.uuid_edit.text()) == 36
    assert "Shadow Dance" in page.name_edit.text()
    assert page.folder_edit.text()
    # Output preview shows the workspace + folder name.
    assert str(tmp_path) in page.output_preview.text()
    assert page.folder_edit.text() in page.output_preview.text()


def test_identity_page_isComplete_requires_required_fields(qapp, tmp_path):
    state = WizardState(settings=app_settings.Settings(workspace_dir=str(tmp_path)))
    state.project_a = Project.load(FIXTURES / "ShadowDance")
    state.project_b = Project.load(FIXTURES / "Shadowdancer")
    page = IdentityPage(state)
    page.initializePage()
    assert page.isComplete() is True
    page.name_edit.setText("")
    assert page.isComplete() is False


def test_policy_page_prefix_field_disabled_unless_prefix_picked(qapp):
    state = WizardState(settings=app_settings.Settings())
    page = PolicyPage(state)
    page.initializePage()
    assert not page.prefix_edit.isEnabled()
    idx = page.policy_combo.findData("prefix")
    page.policy_combo.setCurrentIndex(idx)
    assert page.prefix_edit.isEnabled()


# --- Defaults derived from inputs -------------------------------------------


def test_default_folder_uses_sanitized_display_name_and_full_uuid(qapp):
    """Default folder name is '<SanitizedDisplayName>_<FullUUID>',
    derived from 'A.name + B.name' as the implied display name.
    Anything non-alphanumeric (the '+' and spaces) collapses to a
    single underscore and the FULL UUID with dashes is appended."""
    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Shadowdancer")
    uuid = "11111111-2222-3333-4444-555555555555"
    folder = _default_folder(a, b, uuid)
    # Folder ends with the full dashed UUID (not a truncated 8-char hash).
    assert folder.endswith(f"_{uuid}")
    # The sanitized-name portion comes from "<a.name> + <b.name>".
    # The exact A/B display names depend on fixtures, but the format
    # is "Sanitized_Plus_Sanitized_<uuid>" or similar  -  we assert the
    # shape rather than exact contents.
    name_part = folder[: -(len(uuid) + 1)]
    assert name_part, "Folder name should have a non-empty name prefix"
    # No characters that would be illegal as a Windows path component.
    import re
    assert re.fullmatch(r"[A-Za-z0-9_]+", name_part), \
        f"Folder name prefix should only contain [A-Za-z0-9_]: {name_part!r}"


def test_sanitize_for_folder_name_collapses_special_chars(qapp):
    """Verify the sanitizer turns "WeaponsOfWar + FantasyWeapons" into
    "WeaponsOfWar_FantasyWeapons"  -  runs of non-alphanumeric become a
    single underscore and leading/trailing underscores are stripped."""
    from gui.wizard import _sanitize_for_folder_name
    assert _sanitize_for_folder_name("WeaponsOfWar + FantasyWeapons") == "WeaponsOfWar_FantasyWeapons"
    assert _sanitize_for_folder_name("My Mod (v2)!") == "My_Mod_v2"
    assert _sanitize_for_folder_name("alreadyclean") == "alreadyclean"
    assert _sanitize_for_folder_name("  spaces  ") == "spaces"
    assert _sanitize_for_folder_name("!!!") == ""
    assert _sanitize_for_folder_name("") == ""


def test_derive_folder_name_falls_back_when_display_name_empty(qapp):
    """If the user clears the display name or types only special
    characters, the folder must still be a valid identifier (we use
    'Mod' as the fallback prefix)."""
    from gui.wizard import _derive_folder_name
    uuid = "abcdef01-2345-6789-abcd-ef0123456789"
    assert _derive_folder_name("", uuid) == f"Mod_{uuid}"
    assert _derive_folder_name("!!!", uuid) == f"Mod_{uuid}"
    assert _derive_folder_name("My Mod", uuid) == f"My_Mod_{uuid}"


def test_default_author_unifies_when_same(qapp):
    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Bloodfang")
    assert _default_author(a, b) == "For_Kiramay"


def test_default_author_named_wins_over_blank(qapp):
    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Shadowdancer")
    assert _default_author(a, b) == "For_Kiramay"


def test_default_author_blank_when_multiple_distinct(qapp):
    a = Project.load(FIXTURES / "ShadowDance")
    b = Project.load(FIXTURES / "Shadowdancer")
    b.mod_meta.author = "Someone Else"
    assert _default_author(a, b) == ""


# --- Worker integration -----------------------------------------------------


def test_worker_runs_a_real_merge_to_completion(qapp, tmp_path):
    out = tmp_path / "out"
    config = merger.MergeConfig(
        inputs=[
            Project.load(FIXTURES / "ShadowDance"),
            Project.load(FIXTURES / "Shadowdancer"),
        ],
        output_dir=out,
        new_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        new_folder="WorkerTestX",
        new_name="Worker Test",
        conflict_policy="skip",
    )

    events = {"progress": 0, "finished": False, "failed": False}
    captured = []

    worker = MergeWorker(config)
    worker.progress.connect(lambda *_: events.__setitem__("progress", events["progress"] + 1))
    worker.finished_with_result.connect(lambda r: (events.__setitem__("finished", True), captured.append(r)))
    worker.failed.connect(lambda _: events.__setitem__("failed", True))
    worker.start()

    deadline_ms = 15000
    elapsed = 0
    while not (events["finished"] or events["failed"]) and elapsed < deadline_ms:
        qapp.processEvents()
        worker.wait(20)
        elapsed += 20
    worker.wait()

    assert events["failed"] is False
    assert events["finished"] is True
    assert events["progress"] > 5
    assert captured
    assert (out / "Mods" / "WorkerTestX" / "meta.lsx").is_file()


def test_worker_surfaces_merge_error_via_failed_signal(qapp, tmp_path):
    """The worker should surface MergeError through the 'failed' signal
    rather than crashing the thread silently."""
    out = tmp_path / "out"
    # Pre-create the bucket subfolder we're about to write to: triggers
    # the "already exists" collision check.
    (out / "Mods" / "X").mkdir(parents=True)
    (out / "Mods" / "X" / "meta.lsx").write_text("<lsx/>")

    config = merger.MergeConfig(
        inputs=[
            Project.load(FIXTURES / "ShadowDance"),
            Project.load(FIXTURES / "Shadowdancer"),
        ],
        output_dir=out,
        new_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        new_folder="X", new_name="X", conflict_policy="skip",
    )

    events = {"finished": False, "failed_message": ""}
    worker = MergeWorker(config)
    worker.finished_with_result.connect(lambda _: events.__setitem__("finished", True))
    worker.failed.connect(lambda tb: events.__setitem__("failed_message", tb))
    worker.start()
    elapsed = 0
    while not (events["finished"] or events["failed_message"]) and elapsed < 5000:
        qapp.processEvents()
        worker.wait(20)
        elapsed += 20
    worker.wait()

    assert events["finished"] is False
    assert "MergeError" in events["failed_message"]


# --- End-to-end wizard drives -----------------------------------------------


def test_wizard_full_drive_new_mod_mode(qapp, tmp_path):
    """Walk through every configuration page in 'Make new mod' mode."""
    w = MergeWizard()

    workspace = w.page(MergeWizard.PAGE_WORKSPACE)
    workspace.workspace_edit.setText(str(FIXTURES))
    assert workspace.isComplete()
    assert workspace.validatePage()

    sel = w.page(MergeWizard.PAGE_SELECTION)
    sel._rescan()
    for i in range(sel.list_a.count()):
        if sel.list_a.item(i).data(Qt.UserRole).mod_name == "Shadow Dance":
            sel.list_a.setCurrentRow(i)
    for i in range(sel.list_b.count()):
        if sel.list_b.item(i).data(Qt.UserRole).mod_name == "Shadowdancer":
            sel.list_b.setCurrentRow(i)
    sel._on_selection_changed()
    sel.mode_new.setChecked(True)
    assert sel.validatePage()
    assert w.state.merge_mode == "new_mod"

    identity = w.page(MergeWizard.PAGE_IDENTITY)
    identity.initializePage()
    # Make the folder name unique to avoid collision-check rejection.
    identity.folder_edit.setText("WizardDriveTestNew")
    assert identity.validatePage()

    policy = w.page(MergeWizard.PAGE_POLICY)
    policy.initializePage()
    assert policy.validatePage()

    review = w.page(MergeWizard.PAGE_REVIEW)
    review.initializePage()
    review._build_summary()
    text = review.summary.toPlainText()
    assert "Shadow Dance" in text
    assert "Shadowdancer" in text
    assert "MAKE NEW MOD" in text


def test_wizard_full_drive_combine_mode_shows_inplace_summary(qapp, tmp_path):
    """In Combine B into A mode, the ReviewPage summary mentions the
    in-place behavior and the wizard's nextId() skips IdentityPage."""
    ws = tmp_path / "ws"
    ws.mkdir()
    shutil.copytree(FIXTURES / "ShadowDance", ws / "ShadowDance", symlinks=False)
    shutil.copytree(FIXTURES / "Shadowdancer", ws / "Shadowdancer", symlinks=False)

    w = MergeWizard()
    workspace = w.page(MergeWizard.PAGE_WORKSPACE)
    workspace.workspace_edit.setText(str(ws))
    assert workspace.validatePage()

    sel = w.page(MergeWizard.PAGE_SELECTION)
    sel._rescan()
    for i in range(sel.list_a.count()):
        if sel.list_a.item(i).data(Qt.UserRole).mod_name == "Shadow Dance":
            sel.list_a.setCurrentRow(i)
    for i in range(sel.list_b.count()):
        if sel.list_b.item(i).data(Qt.UserRole).mod_name == "Shadowdancer":
            sel.list_b.setCurrentRow(i)
    sel._on_selection_changed()
    sel.mode_combine.setChecked(True)
    assert sel.validatePage()
    assert w.state.merge_mode == "combine_b_into_a"

    # nextId from SelectionPage skips IdentityPage. Need to show() so
    # currentId() is established.
    w.show()
    try:
        w.setCurrentId(MergeWizard.PAGE_SELECTION)
        assert w.nextId() == MergeWizard.PAGE_POLICY
    finally:
        w.close()

    policy = w.page(MergeWizard.PAGE_POLICY)
    policy.initializePage()
    assert policy.validatePage()

    review = w.page(MergeWizard.PAGE_REVIEW)
    review.initializePage()
    review._build_summary()
    text = review.summary.toPlainText()
    assert "COMBINE INTO A" in text
    assert "modified in place" in text


def test_result_page_names_specific_definition_collisions(qapp, tmp_path):
    """The ResultPage validation summary must name the *specific* colliding
    stat name / UUID and where it was defined, not just a count.

    Regression for user feedback (2026-05): the summary said
    "[stat_name]: 1 collision(s)" with no indication of WHICH stat, so the
    user couldn't act on it. We now print the identifier value and its
    definition locations.
    """
    from pathlib import Path as _Path

    from core import validate
    from core.references import IdentifierEntry, IdKind, Location

    state = WizardState(settings=app_settings.Settings())

    # Minimal fake merge result: ResultPage only reads output_dir,
    # emissions, conflicts, skipped_files off it for the summary header.
    state.merge_result = merger.MergeResult(
        output_dir=tmp_path / "out",
        new_project=None,
    )

    # Hand-build a validation report with one stat_name collision and one
    # uuid collision, each carrying its definition locations: exactly the
    # shape validate.validate() would produce.
    report = validate.ValidationReport()
    report.definition_collisions = {
        "stat_name": [
            IdentifierEntry(
                kind=IdKind.STAT_NAME,
                value="SDancer_Shadow_Step",
                definitions=[
                    Location(file=_Path("Public/A/Stats/Generated/Data/Spell.txt"),
                             hint="stats SDancer_Shadow_Step"),
                    Location(file=_Path("Public/B/Stats/Generated/Data/Spell.txt"),
                             hint="stats SDancer_Shadow_Step"),
                ],
            ),
        ],
        "uuid": [
            IdentifierEntry(
                kind=IdKind.UUID,
                value="d21296e6-898c-4072-8c24-4c5a26f249f0",
                definitions=[
                    Location(file=_Path("Public/A/RootTemplates/_merged.lsf.lsx"),
                             hint="root template MapKey"),
                    Location(file=_Path("Public/B/RootTemplates/_merged.lsf.lsx"),
                             hint="root template MapKey"),
                ],
            ),
        ],
    }
    state.validation_report = report

    page = ResultPage(state)
    page.initializePage()
    text = page.summary.toPlainText()

    # The specific colliding identifiers must appear by name.
    assert "SDancer_Shadow_Step" in text, (
        f"colliding stat name not surfaced in summary:\n{text}"
    )
    assert "d21296e6-898c-4072-8c24-4c5a26f249f0" in text, (
        f"colliding UUID not surfaced in summary:\n{text}"
    )
    # And the definition locations should be shown so the user can find them.
    assert "Public/A/Stats/Generated/Data/Spell.txt" in text
    assert "Public/B/RootTemplates/_merged.lsf.lsx" in text
    # The count line is still present.
    assert "collision(s)" in text


def test_result_page_clean_validation_says_no_issues(qapp, tmp_path):
    """Sanity: with a clean report, the summary shows the no-issues line
    and does NOT print any collision detail."""
    from core import validate

    state = WizardState(settings=app_settings.Settings())
    state.merge_result = merger.MergeResult(
        output_dir=tmp_path / "out", new_project=None,
    )
    state.validation_report = validate.ValidationReport()  # empty == clean

    page = ResultPage(state)
    page.initializePage()
    text = page.summary.toPlainText()

    assert "No issues found." in text
    assert "collision" not in text.lower()


def test_wizard_default_size_is_larger(qapp):
    """First-run window (no saved geometry) opens at the roomier default
    size, not the old cramped 820x600. The autouse settings-isolation
    fixture guarantees there's no saved geometry to override it."""
    w = MergeWizard()
    # Default is 1024x760. We assert a floor rather than exact equality so
    # a future bump doesn't break the test, but the old 820x600 fails.
    assert w.width() >= 1024
    assert w.height() >= 760


def test_wizard_has_minimize_and_maximize_buttons(qapp):
    """The window must expose minimize and maximize controls, not just
    the close (X) button that QDialog-derived windows get by default."""
    from PySide6.QtCore import Qt
    w = MergeWizard()
    flags = w.windowFlags()
    assert flags & Qt.WindowMinimizeButtonHint, "missing minimize button"
    assert flags & Qt.WindowMaximizeButtonHint, "missing maximize button"
    assert flags & Qt.WindowCloseButtonHint, "missing close button"


def test_wizard_has_sensible_minimum_size(qapp):
    """A minimum size keeps the user from shrinking the window to where
    the lists and wizard buttons get clipped."""
    w = MergeWizard()
    assert w.minimumWidth() >= 800
    assert w.minimumHeight() >= 580


def test_wizard_persists_window_geometry(qapp, tmp_path, monkeypatch):
    """Resizing the window and closing it should save the geometry, and a
    fresh wizard should restore it. This is what makes window-size
    adjustments stick across runs.

    We assert that a geometry blob is written on close and consumed on
    the next construction; we don't assert exact pixel dimensions because
    the headless 'offscreen' Qt platform clamps window sizes (there's no
    real screen). The save/restore round-trip itself is the contract.
    """
    import json

    # Point settings at a clean temp file (the autouse fixture already
    # isolates, but we want to read the file back here explicitly).
    settings_path = tmp_path / "geom_settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", settings_path)

    w1 = MergeWizard()
    w1.resize(1200, 900)
    w1.close()  # triggers closeEvent -> saveGeometry -> settings.save

    assert settings_path.exists(), "settings file not written on close"
    saved = json.loads(settings_path.read_text())
    assert saved.get("window_geometry"), (
        "window_geometry was not persisted on close"
    )
    # The saved geometry must be stamped with the current version so the
    # next launch honors it.
    assert saved.get("geometry_version") == app_settings.GEOMETRY_VERSION

    # A new wizard should pick up and apply that saved geometry without
    # raising. (Exact restored size is screen-dependent; we assert the
    # restore path runs cleanly and the blob is consumed.)
    w2 = MergeWizard()
    assert w2.state.settings.window_geometry == saved["window_geometry"]


def test_wizard_ignores_stale_geometry_from_older_version(qapp, tmp_path, monkeypatch):
    """A geometry saved under an OLDER geometry_version must be ignored on
    launch so a newly-changed default size actually appears. This is the
    fix for 'I changed the default size but the window still opens small'
    when a user has an old saved geometry from a previous build.
    """
    settings_path = tmp_path / "stale_geom.json"
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", settings_path)

    # Simulate a pre-existing settings file from an older build: it has a
    # geometry blob but an older version number.
    stale = app_settings.Settings(
        window_geometry="c29tZS1vbGQtYmxvYg==",  # arbitrary base64
        geometry_version=app_settings.GEOMETRY_VERSION - 1,
    )
    app_settings.save(stale)

    w = MergeWizard()
    # Because the version mismatches, the stale geometry is NOT restored;
    # the new default size applies.
    assert w.width() >= 1024
    assert w.height() >= 760


def test_wizard_maximum_size_is_not_fixed(qapp):
    """The window must not report a fixed/small maximum size, or Windows
    grays out the maximize button. We assert the maximum is effectively
    unbounded (Qt's QWIDGETSIZE_MAX sentinel)."""
    w = MergeWizard()
    # 16777215 is Qt's "no maximum" value. Anything near a real window
    # size here would mean the maximize button gets disabled.
    assert w.maximumWidth() >= 16777215
    assert w.maximumHeight() >= 16777215


# --- Add-Icon dialog --------------------------------------------------------


def _fake_discovered(data_root, folder="ModA", name="Mod A"):
    from core.discover import DiscoveredProject
    return DiscoveredProject(
        data_root=data_root, mod_folder_name=folder,
        mod_name=name, mod_uuid="u1", author="Me", description="",
    )


def test_add_icon_dialog_lists_mods_and_types(qapp, tmp_path):
    mods = [_fake_discovered(tmp_path / "ws", "ModA", "Mod A"),
            _fake_discovered(tmp_path / "ws", "ModB", "Mod B")]
    dlg = AddIconDialog(mods)
    assert dlg.mod_combo.count() == 2
    # All icon types from the core module are offered.
    from core import icon_add
    assert dlg.type_combo.count() == len(icon_add.ICON_TYPES)


def test_add_icon_dialog_add_disabled_until_name_and_png(qapp, tmp_path):
    mods = [_fake_discovered(tmp_path / "ws")]
    dlg = AddIconDialog(mods)
    assert not dlg.add_button.isEnabled()
    dlg.name_edit.setText("MyIcon")
    assert not dlg.add_button.isEnabled()  # still no PNG
    # Simulate choosing a PNG.
    png = tmp_path / "icon.png"
    from PIL import Image
    Image.new("RGBA", (256, 256), (1, 2, 3, 255)).save(png)
    dlg._png_path = png
    dlg._update_ok_enabled()
    assert dlg.add_button.isEnabled()


def test_add_icon_dialog_generates_assets_on_add(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from PIL import Image

    # Silence modal dialogs so the test runs headlessly.
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    data_root = tmp_path / "ws"
    # The new layout writes to BOTH Mods/<Mod> and Public/<Mod>.
    (data_root / "Public" / "ModA").mkdir(parents=True)
    (data_root / "Mods" / "ModA").mkdir(parents=True)
    png = tmp_path / "icon.png"
    Image.new("RGBA", (512, 512), (200, 100, 40, 255)).save(png)

    dlg = AddIconDialog([_fake_discovered(data_root, "ModA", "Mod A")])
    dlg.type_combo.setCurrentText("Spell / Skill")
    dlg.name_edit.setText("DialogSpell")
    dlg._png_path = png
    dlg.png_edit.setText(str(png))
    dlg._update_ok_enabled()
    dlg._on_add_clicked()

    # Atlas is at Public, named newAtlas.dds (toolkit default convention).
    atlas = data_root / "Public" / "ModA" / "Assets" / "Textures" / "Icons" / "newAtlas.dds"
    assert atlas.exists()
    # Tooltip is at Mods/.../GUI/Assets/...
    tooltip = data_root / "Mods" / "ModA" / "GUI" / "Assets" / "Tooltips" / "Icons" / "DialogSpell.DDS"
    assert tooltip.exists()
    # metadata.lsf was written.
    meta = data_root / "Mods" / "ModA" / "GUI" / "metadata.lsf.lsx"
    assert meta.exists()
    # Form is cleared so the user can add another without double-adding.
    assert dlg.name_edit.text() == ""
    assert dlg._png_path is None


def test_add_icon_dialog_type_hint_changes_per_family(qapp, tmp_path):
    """Each family produces a distinct hint."""
    dlg = AddIconDialog([_fake_discovered(tmp_path / "ws")])
    dlg.type_combo.setCurrentText("Spell / Skill")
    atlas_hint = dlg.type_hint.text()
    dlg.type_combo.setCurrentText("Class / Subclass")
    class_hint = dlg.type_hint.text()
    dlg.type_combo.setCurrentText("Action Resource")
    ar_hint = dlg.type_hint.text()
    dlg.type_combo.setCurrentText("Portrait")
    portrait_hint = dlg.type_hint.text()
    # ATLAS hint is intentionally empty  -  the cosmetic-options panel
    # and preview right below the form already make it obvious what'll
    # be written, and the earlier prose was overexplaining a simple
    # case. The non-ATLAS families still have informative hints
    # because their pipelines write to less-obvious paths.
    assert atlas_hint == ""
    # The three remaining hints are distinct and mention their family.
    assert len({class_hint, ar_hint, portrait_hint}) == 3
    assert "class" in class_hint.lower()
    assert "action resource" in ar_hint.lower()
    assert "portrait" in portrait_hint.lower()


def test_add_icon_dialog_offers_portrait_with_distinct_hint(qapp, tmp_path):
    """Portrait is a selectable type. The hint should cover both use
    cases (own-mod portrait with any clean name; base-game NPC override
    with the exact GUID-prefixed filename) and mention the metadata
    registration."""
    dlg = AddIconDialog([_fake_discovered(tmp_path / "ws")])
    labels = [dlg.type_combo.itemText(i) for i in range(dlg.type_combo.count())]
    assert "Portrait" in labels
    dlg.type_combo.setCurrentText("Portrait")
    hint = dlg.type_hint.text().lower()
    assert "152" in hint
    assert "metadata" in hint
    assert "override" in hint
