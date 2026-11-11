# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

BASE_DIR = Path.cwd()

block_cipher = None

# Data files to include: dashboard.html, static/, src/security.py, src/db_store.py, src/logging_config.py, config
datas = [
    (str(BASE_DIR / "dashboard.html"), "."),
    (str(BASE_DIR / "static"), "static"),
    (str(BASE_DIR / "src" / "security.py"), "src"),
    (str(BASE_DIR / "src" / "db_store.py"), "src"),
    (str(BASE_DIR / "src" / "logging_config.py"), "src"),
    (str(BASE_DIR / "config"), "config"),
]

# Hidden imports for Flask-SocketIO and related
hiddenimports = [
    "flask_socketio",
    "engineio.async_drivers.threading",
    "engineio.async_drivers.eventlet",
    "prometheus_client",
    "cryptography.fernet",
    "alembic",
    "yaml",
    "rq",
    "redis",
]

a = Analysis(
    ["src/ghost_server.py"],
    pathex=[str(BASE_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
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
    name="ghost-data-suite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Show console for backend logs; use False for windowed
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Optionally: icon="assets/icon.ico"
)
