# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for LazyCloud CLI
# This file configures how PyInstaller builds the standalone binary

from pathlib import Path

# Get the parent directory (apps/cli) from this spec file's location
spec_dir = Path(SPECPATH)
cli_dir = spec_dir.parent

block_cipher = None

a = Analysis(
    [str(cli_dir / 'src' / 'cli' / 'main.py')],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # Workspace packages
        'models',
        'api_requests',
        'responses',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='lazycloud',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
