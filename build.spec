# -*- mode: python ; coding: utf-8 -*-
import sys
import os

block_cipher = None

IS_WINDOWS = os.name == "nt"

resources_dir = os.path.join(os.getcwd(), "ui", "resources")
icon_path = os.path.join(resources_dir, "project-icon.jpg")
license_path = os.path.join(resources_dir, "LICENSE.ffmpeg")

# Windows 用 ffmpeg.exe，Linux/WSL2 用 ffmpeg；都没有就不打包，
# 应用会在缺少 ffmpeg 时直接识别 wav/flac 并给出安装提示。
ffmpeg_path = ""
for _name in (("ffmpeg.exe", "ffmpeg") if IS_WINDOWS else ("ffmpeg", "ffmpeg.exe")):
    _candidate = os.path.join(resources_dir, _name)
    if os.path.isfile(_candidate):
        ffmpeg_path = _candidate
        break

datas = []

# HardwareMonitor 自带 LibreHardwareMonitorLib 程序集，需要一并打包
try:
    from PyInstaller.utils.hooks import collect_data_files
    datas += collect_data_files("HardwareMonitor", include_py_files=True)
except Exception:
    pass

if ffmpeg_path:
    datas.append((ffmpeg_path, "ui/resources"))
if os.path.isfile(icon_path):
    datas.append((icon_path, "ui/resources"))
if os.path.isfile(license_path):
    datas.append((license_path, "ui/resources"))

# Web 控制台前端资源（WSLg 不可用时的备用入口）
web_console_dir = os.path.join(os.getcwd(), "app", "web_console")
datas += [
    (os.path.join(web_console_dir, "templates"), "app/web_console/templates"),
    (os.path.join(web_console_dir, "static"), "app/web_console/static"),
]

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
        "modelscope",
        "modelscope.hub.snapshot_download",
        "modelscope.hub.callback",
        "modelscope_hub",
        "huggingface_hub",
        "tqdm",
        "core.hardware_checker",
        "core.gpu_probe",
        "core.torch_env",
        "core.hardware_monitor",
        "core.pipeline",
        "app.runtime_env",
        "app.web_console",
        "app.web_console.server",
        "app.web_console.service",
        "flask",
        "jinja2",
        "werkzeug",
        "ui.widgets.monitor_panel",
        "core.transcriber",
        "core.downloader",
        "core.download_worker",
        "core.exporter",
        "app.download_manager",
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
    icon=icon_path if (IS_WINDOWS and os.path.isfile(icon_path)) else None,
)
