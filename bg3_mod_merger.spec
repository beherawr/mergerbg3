# PyInstaller spec for the BG3 Mod Merger GUI.
#
# Build on Windows with:
#     pip install pyinstaller PySide6 lxml
#     pyinstaller bg3_mod_merger.spec
#
# Produces ``dist/bg3_mod_merger.exe`` (one-file mode). The launcher
# bundles Python + PySide6 + lxml + the entire ``core/`` and ``gui/``
# packages, AND LSLib (Norbyte's divine.exe + dependencies) when present
# at ``vendor/lslib/``. Compressed size with LSLib is ~40-55 MB.
#
# Distribution notes:
# - ``upx=False``: UPX-packed binaries trigger Windows Defender and
#   other antivirus false positives constantly. Disabling UPX makes the
#   exe a few MB larger but dramatically reduces "this app might be
#   unsafe" warnings. Worth the trade.
# - ``console=False``: no black console window pops up next to the GUI.
#   For debugging, flip to True temporarily so Python tracebacks appear
#   in a terminal alongside the wizard.
# - ``version='version_info.txt'``: embeds proper Windows file-version
#   metadata. SmartScreen treats exes with valid version info less
#   suspiciously than those without.
# - LSLib is included under its MIT license. Its full source and
#   license live at https://github.com/Norbyte/lslib. We ship the
#   release-zip contents verbatim (divine.exe + LSLib.dll +
#   dependencies) under vendor/lslib/. CI downloads them before the
#   PyInstaller step; locally, drop the unzipped LSLib release at
#   vendor/lslib/ before building.

# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None

# Bundle LSLib if it's been placed under vendor/lslib/ (typically by
# CI's "Download LSLib release" step before build, or manually by a
# developer who unzipped Norbyte's release there). At runtime, LSLib
# lives next to the entrypoint under tools/lslib/divine.exe, and
# core.divine._bundled_divine_path() finds it from sys._MEIPASS.
#
# If the directory doesn't exist (e.g. build-from-fresh-checkout
# without CI), we still produce a working exe  -  divine just won't be
# bundled. core.divine.find_divine falls through to PATH lookup and
# the user can configure their own divine.exe in Settings, the way
# the pre-bundle release behaved.
_vendor_lslib = os.path.join('vendor', 'lslib')
_bundled_datas = []
if os.path.isdir(_vendor_lslib):
    # PyInstaller datas tuples are (source, dest_dir_in_bundle).
    # Recursively include every file under vendor/lslib/. Each file
    # ends up at <bundle>/tools/lslib/<filename> at runtime.
    for root, _dirs, files in os.walk(_vendor_lslib):
        for name in files:
            src = os.path.join(root, name)
            # Preserve any subdir structure inside vendor/lslib.
            rel = os.path.relpath(root, _vendor_lslib)
            dest_dir = 'tools/lslib' if rel in ('.', '') else f'tools/lslib/{rel.replace(os.sep, "/")}'
            _bundled_datas.append((src, dest_dir))

# Icon backgrounds for the Add Icon dialog's cosmetic options. These
# live alongside the GUI code at gui/assets/icon_backgrounds/ in
# source, and end up at the same relative path under sys._MEIPASS
# inside the bundled exe. core.icon_compose._backgrounds_dir() looks
# them up at runtime in both layouts.
_bg_assets = os.path.join('gui', 'assets', 'icon_backgrounds')
if os.path.isdir(_bg_assets):
    for name in os.listdir(_bg_assets):
        src = os.path.join(_bg_assets, name)
        if os.path.isfile(src):
            _bundled_datas.append((src, 'gui/assets/icon_backgrounds'))

# Bundled game-icons.net set (~4180 256x256 1-bit PNGs, CC BY 3.0).
# Same layout pattern as icon_backgrounds: shipped under gui/assets/
# in source, ends up at the same relative path inside the exe via
# sys._MEIPASS. The runtime lookup in gui/game_icons_search.py
# resolves both layouts. The _index.json file in the same folder is
# the search corpus (icon name + author per entry).
_gi_assets = os.path.join('gui', 'assets', 'game_icons')
if os.path.isdir(_gi_assets):
    for name in os.listdir(_gi_assets):
        src = os.path.join(_gi_assets, name)
        if os.path.isfile(src):
            _bundled_datas.append((src, 'gui/assets/game_icons'))

a = Analysis(
    ['gui/__main__.py'],
    pathex=[],
    binaries=[],
    datas=_bundled_datas,
    hiddenimports=[
        # lxml has C-extension submodules PyInstaller's static analysis
        # sometimes misses.
        'lxml.etree',
        'lxml._elementpath',
        # PySide6 platform-integration plugins are usually picked up by
        # PySide6's PyInstaller hook, but listing the core widget
        # modules explicitly avoids surprises if the hook changes.
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        # Pillow's image-format plugins are imported lazily by name, so
        # PyInstaller's static analysis can miss them. The Add-Icon
        # feature reads PNGs and writes BC3/DXT5 DDS, so make sure both
        # plugins are bundled.
        'PIL.PngImagePlugin',
        'PIL.DdsImagePlugin',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Don't bundle the test suite or example fixtures.
        'tests',
        'pytest',
        # tkinter saves ~5MB; we use PySide6, not Tk.
        'tkinter',
        # Qt modules we don't use: saves ~50MB combined.
        'PySide6.QtNetwork',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtQml',
        'PySide6.QtQuick',
        'PySide6.Qt3DCore',
        'PySide6.QtMultimedia',
        'PySide6.QtBluetooth',
        'PySide6.QtCharts',
        'PySide6.QtDataVisualization',
        'PySide6.QtLocation',
        'PySide6.QtPositioning',
        'PySide6.QtSensors',
        'PySide6.QtSerialPort',
        'PySide6.QtSql',
        'PySide6.QtTest',
        # Numerical libraries Python ships with that we don't touch.
        'numpy',
        'matplotlib',
        # NOTE: PIL/Pillow was previously excluded to save space, but the
        # Add-Icon feature now needs it for PNG->DDS conversion, so it
        # must be bundled. Do NOT add 'PIL' here.
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Look for an optional version_info.txt next to this spec; if present,
# embed it in the exe. CI generates this on every build from the git tag.
_version_file = 'version_info.txt' if os.path.exists('version_info.txt') else None
# Same idea for an optional icon.ico.
_icon_file = 'icon.ico' if os.path.exists('icon.ico') else None

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='BG3 Mod Merger',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                   # see header comment: AV false positives
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon_file,
    version=_version_file,
)
