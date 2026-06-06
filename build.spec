# -*- mode: python ; coding: utf-8 -*-
import sys
import os

block_cipher = None

resources_dir = os.path.join(os.getcwd(), "ui", "resources")
ffmpeg_path = os.path.join(resources_dir, "ffmpeg.exe")
icon_path = os.path.join(resources_dir, "project-icon.jpg")
license_path = os.path.join(resources_dir, "LICENSE.ffmpeg")

datas = []
if os.path.isfile(ffmpeg_path):
    datas.append((ffmpeg_path, "ui/resources"))
if os.path.isfile(icon_path):
    datas.append((icon_path, "ui/resources"))
if os.path.isfile(license_path):
    datas.append((license_path, "ui/resources"))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "funasr",
        "funasr.models",
        "funasr.auto.auto_model",
        "qfluentwidgets",
        "PyQt6",
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "torch",
        "torchaudio",
        "requests",
        "core.hardware_checker",
        "core.transcriber",
        "core.downloader",
        "core.exporter",
        "app.settings",
        "app.utils",
        "app.worker",
        "ui.tabs.transcribe_tab",
        "ui.tabs.hardware_tab",
        "ui.tabs.models_tab",
        "ui.tabs.logs_tab",
        "ui.dialogs.about_dialog",
        "ui.dialogs.settings_dialog",
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
    name='Maple-FunASR-Desktop',
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
    icon=os.path.join(resources_dir, "project-icon.jpg") if os.path.isfile(os.path.join(resources_dir, "project-icon.jpg")) else None,
)
