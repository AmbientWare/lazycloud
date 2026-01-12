# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for LazyCloud CLI
# This file configures how PyInstaller builds the standalone binary

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

# Get the parent directory (apps/cli) from this spec file's location
spec_dir = Path(SPECPATH)
cli_dir = spec_dir.parent

block_cipher = None

# Collect pyfiglet fonts
pyfiglet_datas = collect_data_files('pyfiglet')

# Collect textual CSS files (.tcss) for the UI
textual_ui_dir = cli_dir / 'src' / 'cli' / 'ui' / 'textual'
tcss_datas = []
for tcss_file in textual_ui_dir.rglob('*.tcss'):
    # Preserve the directory structure relative to src/cli
    relative_path = tcss_file.relative_to(cli_dir / 'src')
    dest_dir = str(relative_path.parent)
    tcss_datas.append((str(tcss_file), dest_dir))

a = Analysis(
    [str(cli_dir / 'src' / 'cli' / 'main.py')],
    pathex=[],
    binaries=[],
    datas=pyfiglet_datas + tcss_datas,
    hiddenimports=[
        # Workspace packages
        'models',
        'api_requests',
        'responses',
        'pyfiglet.fonts',
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
