"""运行环境探测：WSL / WSLg / 图形平台可用性。

GUI 启动前用它决定：

1. 当前是不是 WSL，有没有 WSLg（XWayland / Wayland 是否可用）；
2. Qt 应该用哪个 platform 插件（``xcb`` / ``wayland`` / 默认）；
3. 如果 GUI 起不来，就切到 Web 控制台。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RuntimeInfo:
    is_windows: bool = False
    is_linux: bool = False
    is_macos: bool = False
    is_wsl: bool = False
    distro: str = ""
    wsl_version: str = ""
    is_wslg: bool = False
    display: str = ""
    wayland_display: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.is_wsl:
            name = f"WSL{'g' if self.is_wslg else ''}"
            if self.distro:
                name += f" · {self.distro}"
            return name
        if self.is_windows:
            return "Windows"
        if self.is_macos:
            return "macOS"
        return "Linux"


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _proc_version() -> str:
    return _read_text("/proc/version")


def is_wsl_env() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    if os.path.isdir("/run/WSL"):
        return True
    version = _proc_version().lower()
    return "microsoft" in version or "wsl" in version


def wslg_available() -> bool:
    """WSLg 是否就绪：需要 WSL2 + WSLg 的各种挂载与显示变量。"""
    if not is_wsl_env():
        return False
    if os.path.isdir("/mnt/wslg"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def detect_runtime() -> RuntimeInfo:
    info = RuntimeInfo(
        is_windows=os.name == "nt",
        is_linux=sys.platform.startswith("linux"),
        is_macos=sys.platform == "darwin",
        display=os.environ.get("DISPLAY", ""),
        wayland_display=os.environ.get("WAYLAND_DISPLAY", ""),
    )
    info.is_wsl = is_wsl_env()
    if info.is_wsl:
        info.distro = os.environ.get("WSL_DISTRO_NAME", "")
        version = _proc_version().lower()
        if "wsl2" in version or os.path.isdir("/mnt/wslg"):
            info.wsl_version = "2"
        elif "microsoft" in version:
            info.wsl_version = "1" if "4.4.0" in version else "2"
        info.is_wslg = wslg_available()

    if info.is_linux and not info.is_wsl:
        info.notes.append("Linux 原生环境")
    if info.is_wsl and not info.is_wslg:
        info.notes.append("未检测到 WSLg，无法显示 GUI")
    return info


# ─────────────────────────── Qt 平台选择 ───────────────────────────


def gui_platform_candidates(info: Optional[RuntimeInfo] = None) -> list[Optional[str]]:
    """按优先级返回可尝试的 QT_QPA_PLATFORM 列表；None 表示用 Qt 默认值。"""
    info = info or detect_runtime()

    if info.is_wsl:
        if not info.is_wslg:
            return []
        candidates: list[Optional[str]] = []
        if info.display:
            candidates.append("xcb")          # WSLg 自带 XWayland，兼容性最好
        if info.wayland_display:
            candidates.append("wayland")
        if info.display:
            candidates.append("xcb")          # 软件渲染再试一次（见 extra_env）
        return candidates or ["xcb"]

    if info.is_windows or info.is_macos:
        return [None]

    # 原生 Linux：先默认，再显式尝试 wayland
    candidates = [None]
    if info.wayland_display:
        candidates.append("wayland")
    return candidates


def platform_extra_env(platform: Optional[str], attempt: int) -> dict[str, str]:
    """针对不同尝试次数附加环境变量（第二次起强制软件渲染）。"""
    env: dict[str, str] = {}
    if platform:
        env["QT_QPA_PLATFORM"] = platform
    if attempt >= 2:
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        env["QT_QUICK_BACKEND"] = "software"
    if platform == "wayland":
        env.setdefault("QT_WAYLAND_DISABLE_WINDOWDECORATION", "1")
    return env


_PROBE_CODE = (
    "import sys\n"
    "from PyQt6.QtWidgets import QApplication\n"
    "app = QApplication([])\n"
    "app.processEvents()\n"
    "print('QT_PROBE_OK')\n"
)


def probe_qt(platform: Optional[str], attempt: int = 1,
             timeout: float = 25.0) -> tuple[bool, str]:
    """在子进程里真正初始化一次 Qt，判断该平台插件能否工作。

    Qt 的 platform 插件加载失败通常会直接 abort 整个进程，
    因此必须在子进程里探测，避免把主进程带走。
    """
    env = dict(os.environ)
    env.update(platform_extra_env(platform, attempt))
    env["PYTHONUNBUFFERED"] = "1"
    try:
        result = subprocess.run(
            [sys.executable, "-c", _PROBE_CODE],
            capture_output=True, text=True, timeout=timeout, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return False, "Qt 初始化超时（可能是显示服务无响应）"
    except Exception as e:
        return False, f"无法启动探测进程：{e}"

    if "QT_PROBE_OK" in (result.stdout or ""):
        return True, ""
    detail = (result.stderr or result.stdout or "").strip()
    return False, detail[-600:] if detail else f"退出码 {result.returncode}"


def choose_gui_platform(info: Optional[RuntimeInfo] = None,
                        verbose: bool = False) -> tuple[Optional[str], str]:
    """返回 (platform, 失败原因)。platform 为 None 且原因非空表示 GUI 不可用。"""
    info = info or detect_runtime()
    candidates = gui_platform_candidates(info)
    if not candidates:
        reason = "未检测到可用的显示服务（WSLg / DISPLAY / WAYLAND_DISPLAY 均不可用）"
        return None, reason

    attempt = 0
    last_error = ""
    for platform in candidates:
        attempt += 1
        ok, detail = probe_qt(platform, attempt)
        if verbose:
            print(f"[env] Qt 平台 {platform or 'default'} 探测：{'OK' if ok else '失败'}")
        if ok:
            return platform, ""
        last_error = detail
    return None, last_error or "Qt 无法初始化图形界面"


def is_wsl_path(path: str) -> bool:
    return path.startswith("/mnt/") or path.startswith("\\\\wsl$")


def to_wsl_path(windows_path: str) -> str:
    """C:\\Users\\me\\a.wav → /mnt/c/Users/me/a.wav（在 WSL 里访问 Windows 文件）。"""
    if not windows_path:
        return windows_path
    if windows_path.startswith("/"):
        return windows_path
    drive, _, rest = windows_path.partition(":")
    if len(drive) == 1 and rest:
        return "/mnt/" + drive.lower() + rest.replace("\\", "/")
    return windows_path.replace("\\", "/")


def to_windows_path(linux_path: str) -> str:
    """在 Windows 侧按 UNC 访问 WSL 里的文件。"""
    if not linux_path.startswith("/"):
        return linux_path
    distro = os.environ.get("WSL_DISTRO_NAME", "")
    if distro:
        return f"\\\\wsl$\\{distro}" + linux_path.replace("/", "\\")
    return linux_path
