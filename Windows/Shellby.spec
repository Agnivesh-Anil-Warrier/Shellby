# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification file for Shellby (Windows Command Prompt Edition).

Build with:
    pyinstaller Shellby.spec
"""

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Collect all submodules and data files for reportlab, pyte, and google-genai
hiddenimports = [
    'shellby',
    'shellby_heuristics',
    'shellby_markdown',
    'shellby_curator',
    'shellby_pdf',
    'shellby_parser',
    'typer',
    'rich',
    'pyte',
    'reportlab',
    'reportlab.platypus',
    'reportlab.lib',
    'reportlab.pdfgen',
    'google.genai',
]

try:
    hiddenimports += collect_submodules('reportlab')
except Exception:
    pass

try:
    hiddenimports += collect_submodules('pyte')
except Exception:
    pass

datas = [
    ('logo/logo.ico', 'logo'),
]
try:
    datas += collect_data_files('reportlab')
except Exception:
    pass

logo_ico = Path('logo') / 'logo.ico'
icon_path = str(logo_ico) if logo_ico.exists() else None

a = Analysis(
    ['shellby_qt2.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Shellby',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # Keeps console capability for host terminal recording; GUI mode hides it automatically
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='logo/logo.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Shellby',
)
