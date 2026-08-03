# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project = Path(SPECPATH)

a = Analysis(
    ["launcher.py"],
    pathex=[str(project)],
    binaries=[],
    datas=[
        (str(project / "templates"), "templates"),
        (str(project / "static"), "static"),
    ],
    hiddenimports=[],
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
    name="LANStream",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="LANStream",
)
