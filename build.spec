# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спецификация: onedir, windowed, без абсолютных путей.

Путь к проекту берётся ТОЛЬКО из SPECPATH (глобальная переменная PyInstaller);
__file__ в spec-файле не определён.
"""

from pathlib import Path

ROOT = Path(SPECPATH)

# winrt импортируется лениво внутри функций — перечисляем явно.
HIDDEN_IMPORTS = [
    "winrt",
    "winrt.runtime",
    "winrt.system",
    "winrt._winrt",
    "winrt.windows",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.media",
    "winrt.windows.media.control",
    "winrt.windows.storage",
    "winrt.windows.storage.streams",
]

datas = []
icon_path = ROOT / "assets" / "icon.ico"
if icon_path.is_file():
    datas.append((str(icon_path), "assets"))

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NowPlayingWidget",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(icon_path) if icon_path.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="NowPlayingWidget",
)
