# PyInstaller spec for the BG3 Mod Merger GUI.
#
# Build on Windows with:
#     pip install pyinstaller PySide6 lxml
#     pyinstaller bg3_mod_merger.spec
#
# Produces ``dist/bg3_mod_merger.exe`` (one-file mode). The launcher
# bundles Python + PySide6 + lxml + the entire ``core/`` and ``gui/``
# packages. Compressed size is ~35-50 MB.
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
# - We don't bundle divine.exe. Users supply its path in app Settings.
#   Bundling LSLib raises a licensing question; sidestepping for now.

# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None

a = Analysis(
    ['gui/__main__.py'],
    pathex=[],
    binaries=[],
    datas=[
        # If we add a stylesheet, icons, or default settings template,
        # they go here as ('source_path', 'dest_relative_path') tuples.
    ],
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
        'PIL',
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
    name='bg3_mod_merger',
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
