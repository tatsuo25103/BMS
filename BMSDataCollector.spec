# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[
        ('C:\\Users\\lf.wu\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\DLLs\\_tkinter.pyd', '.'),
        ('C:\\Users\\lf.wu\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\DLLs\\tcl86t.dll', '.'),
        ('C:\\Users\\lf.wu\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\DLLs\\tk86t.dll', '.'),
    ],
    datas=[
        ('C:\\Users\\lf.wu\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\tcl\\tcl8.6', '_tcl_data'),
        ('C:\\Users\\lf.wu\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\tcl\\tk8.6', '_tk_data'),
        ('assets', 'assets'),
    ],
    hiddenimports=['tkinter', '_tkinter'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Codex bundled Python can run Tkinter through the virtual environment, while
# PyInstaller's automatic Tcl probe rejects the base runtime. Add the standard
# library Tkinter modules explicitly so the frozen application remains complete.
tkinter_root = 'C:\\Users\\lf.wu\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\Lib\\tkinter'
for source in sorted(Path(tkinter_root).glob('*.py')):
    module_name = 'tkinter' if source.name == '__init__.py' else f'tkinter.{source.stem}'
    a.pure.append((module_name, str(source), 'PYMODULE'))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BMSDataCollector',
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
    version='packaging/version_info_app.txt',
    codesign_identity=None,
    entitlements_file=None,
)
