# -*- mode: python ; coding: utf-8 -*-
#
# Build:  python -m PyInstaller PDMA-v2.3.spec --noconfirm
#         Use the python.org Python 3.11 (AppData/Local/Programs/Python/Python311),
#         NOT a conda env: conda PyInstaller fails to bundle tcl86t.dll/tk86t.dll
#         and the .exe dies at start with "DLL load failed while importing _tkinter".
#
# Writes only dist/PDMA-v2.3.exe. Do NOT use build_exe.bat for this: that
# script does `rmdir /s /q dist`, which would delete every previous
# PDMA/PawseyUploader release kept in dist/.


a = Analysis(
    ['pawsey_uploader.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PDMA-v2.3',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
