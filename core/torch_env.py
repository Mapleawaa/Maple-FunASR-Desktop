"""PyTorch 运行环境探测与加速版安装方案。

用途
----
1. 在**不导入 torch** 的前提下（importlib.metadata）读取版本号与构建类型，
   避免启动时几百毫秒到数秒的阻塞；
2. 区分 CPU-only / CUDA / ROCm / XPU 四种构建，识别 "装了显卡但 torch 是 CPU 版"
   这种最常见的问题；
3. 根据探测到的显卡给出可以直接执行的 pip 安装命令（含 index-url）。
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────── 常量 ───────────────────────────

BUILD_LABELS = {
    "cpu": "CPU-only",
    "cuda": "CUDA",
    "rocm": "ROCm",
    "xpu": "Intel XPU",
    "mps": "Apple MPS",
    "unknown": "未知",
}

PYTORCH_INDEX = "https://download.pytorch.org/whl/{tag}"

# 由驱动侧 CUDA 版本推断可用的 wheel tag（从新到旧匹配）
_CUDA_WHEEL_TAGS = ("cu128", "cu126", "cu124", "cu121", "cu118")

# ROCm wheel tag（官方仅 Linux 提供）
_ROCM_WHEEL_TAGS = ("rocm6.4", "rocm6.3", "rocm6.2", "rocm6.1")


# ─────────────────────────── 数据结构 ───────────────────────────


@dataclass
class TorchEnv:
    """当前解释器下 PyTorch 的安装情况。"""

    installed: bool = False
    version: str = ""            # 2.14.1+cpu
    release: str = ""            # 2.14.1
    local_tag: str = ""          # cpu / cu126 / rocm6.2 / xpu
    build: str = "unknown"       # cpu / cuda / rocm / xpu / unknown
    runtime: str = ""            # 12.6（CUDA runtime）或 6.2（ROCm runtime）
    torchaudio_version: str = ""
    python_version: str = ""
    executable: str = ""
    frozen: bool = False
    error: str = ""

    @property
    def build_label(self) -> str:
        return BUILD_LABELS.get(self.build, self.build)

    @property
    def is_cpu_only(self) -> bool:
        return self.installed and self.build == "cpu"

    @property
    def version_text(self) -> str:
        if not self.installed:
            return "未安装"
        return self.version or self.release

    @property
    def torchaudio_mismatch(self) -> bool:
        """torch / torchaudio 主次版本是否一致。"""
        if not self.torchaudio_version or not self.release:
            return False
        a = ".".join(self.release.split(".")[:2])
        b = ".".join(self.torchaudio_version.split(".")[:2])
        return a != b


@dataclass
class InstallPlan:
    """一次加速版 PyTorch 安装建议。"""

    build: str                     # cuda / rocm / xpu
    index_url: str
    packages: tuple[str, ...] = ("torch", "torchaudio")
    supported: bool = True         # 当前平台是否官方支持
    reason: str = ""               # 为什么推荐这个版本
    note: str = ""                 # 额外注意事项

    @property
    def command(self) -> list[str]:
        return [
            sys.executable, "-m", "pip", "install",
            "--upgrade", "--force-reinstall", *self.packages,
            "--index-url", self.index_url,
        ]

    @property
    def command_text(self) -> str:
        return " ".join(_quote(p) for p in self.command)

    def manual_command(self) -> str:
        """给用户复制到终端执行的命令（不依赖当前解释器路径）。"""
        return "python -m pip install --upgrade --force-reinstall " \
               f"{' '.join(self.packages)} --index-url {self.index_url}"


# ─────────────────────────── 工具函数 ───────────────────────────


def _quote(part: str) -> str:
    if not part:
        return '""'
    if re.search(r'[\s"&|<>^]', part):
        return f'"{part}"'
    return part


def _dist_version(name: str) -> str:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return ""
    except Exception:  # 损坏的元数据不应影响启动
        return ""


def _split_local(version: str) -> tuple[str, str]:
    """2.14.1+cpu -> ("2.14.1", "cpu")"""
    if "+" in version:
        release, local = version.split("+", 1)
        return release, local
    return version, ""


def _build_from_local_tag(local: str) -> tuple[str, str]:
    """由本地版本标签推断构建类型与运行时版本。"""
    tag = (local or "").lower()
    if not tag:
        return "unknown", ""
    if tag.startswith("cpu"):
        return "cpu", ""
    m = re.match(r"^cu(\d)(\d+)", tag)
    if m:
        return "cuda", f"{m.group(1)}.{m.group(2)}"
    m = re.match(r"^rocm(\d+)\.?(\d*)", tag)
    if m:
        runtime = m.group(1) + (("." + m.group(2)) if m.group(2) else "")
        return "rocm", runtime
    if tag.startswith("xpu"):
        return "xpu", ""
    return "unknown", ""


_torch_lock = threading.Lock()
_torch_module = None
_torch_env_cache: Optional[TorchEnv] = None


def import_torch():
    """延迟导入 torch（较慢，仅在实际推理/检测时调用）。"""
    global _torch_module
    with _torch_lock:
        if _torch_module is None:
            import torch  # noqa: WPS433
            _torch_module = torch
        return _torch_module


def get_torch_env(refresh: bool = False) -> TorchEnv:
    """读取 PyTorch 环境信息（带缓存，不触发 torch 导入）。"""
    global _torch_env_cache
    if _torch_env_cache is not None and not refresh:
        return _torch_env_cache

    env = TorchEnv(
        python_version=platform.python_version(),
        executable=sys.executable,
        frozen=bool(getattr(sys, "frozen", False)),
    )

    version = _dist_version("torch")
    if not version:
        env.installed = False
        env.error = "未检测到 torch，请先执行 pip install torch torchaudio"
        _torch_env_cache = env
        return env

    env.installed = True
    env.version = version
    env.release, env.local_tag = _split_local(version)
    env.build, env.runtime = _build_from_local_tag(env.local_tag)
    env.torchaudio_version = _dist_version("torchaudio")

    # 元数据里的 local tag 有时缺失（例如从源码编译），再用 torch 运行时兜底
    if env.build in ("unknown", "cpu"):
        runtime_build, runtime_ver = _build_from_runtime()
        if runtime_build != "unknown":
            env.build, env.runtime = runtime_build, runtime_ver or env.runtime

    _torch_env_cache = env
    return env


def _build_from_runtime() -> tuple[str, str]:
    """通过 torch.version / torch.xpu 判定真实构建类型。"""
    try:
        torch = import_torch()
    except Exception as e:  # pragma: no cover - torch 导入失败
        return "unknown", str(e)

    try:
        hip = getattr(torch.version, "hip", None)
        if hip:
            return "rocm", str(hip)
        cuda = getattr(torch.version, "cuda", None)
        if cuda:
            return "cuda", str(cuda)
        xpu_ver = getattr(torch.version, "xpu", None)
        if xpu_ver:
            return "xpu", str(xpu_ver)
        if hasattr(torch, "xpu"):
            try:
                if torch.xpu.device_count() > 0:
                    return "xpu", ""
            except Exception:
                pass
        if getattr(torch.backends, "mps", None) is not None:
            try:
                if torch.backends.mps.is_available():
                    return "mps", ""
            except Exception:
                pass
        return "cpu", ""
    except Exception:
        return "unknown", ""


# ─────────────────────────── 安装方案 ───────────────────────────


def cuda_driver_version() -> str:
    """从 nvidia-smi 读取驱动支持的最高 CUDA 版本，例如 12.6。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    try:
        out = subprocess.run(
            [exe], capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return ""
    m = re.search(r"CUDA Version:\s*([0-9]+)\.([0-9]+)", out)
    return f"{m.group(1)}.{m.group(2)}" if m else ""


def _cuda_tag_for(driver: str) -> str:
    if not driver:
        return "cu126"
    try:
        major, minor = (int(x) for x in driver.split(".")[:2])
    except Exception:
        return "cu126"
    driver_num = major * 10 + minor
    for tag in _CUDA_WHEEL_TAGS:
        m = re.match(r"cu(\d)(\d+)", tag)
        if not m:
            continue
        need = int(m.group(1)) * 10 + int(m.group(2))
        if driver_num >= need:
            return tag
    return "cu118"


def _rocm_tag() -> str:
    return _ROCM_WHEEL_TAGS[0]


def suggest_install_plan(vendor: str, env: Optional[TorchEnv] = None) -> Optional[InstallPlan]:
    """根据显卡厂商给出加速版安装方案。

    vendor: nvidia / amd / intel / other
    """
    env = env or get_torch_env()
    is_windows = sys.platform.startswith("win")

    if vendor == "nvidia":
        driver = cuda_driver_version()
        tag = _cuda_tag_for(driver)
        reason = f"检测到 NVIDIA 显卡，推荐安装 CUDA {tag[2:]} 版本"
        if driver:
            reason += f"（驱动最高支持 CUDA {driver}）"
        return InstallPlan(
            build="cuda",
            index_url=PYTORCH_INDEX.format(tag=tag),
            reason=reason,
            note="安装包约 2.5 GB，完成后需要重启本程序才能生效。",
        )

    if vendor == "intel":
        return InstallPlan(
            build="xpu",
            index_url=PYTORCH_INDEX.format(tag="xpu"),
            reason="检测到 Intel 显卡，推荐安装 XPU 版本（无需再装 intel-extension-for-pytorch）",
            note="需要 Intel 显卡驱动 32.0.101.6078 或更新版本；安装后需重启本程序。",
        )

    if vendor == "amd":
        if is_windows:
            return InstallPlan(
                build="rocm",
                index_url=PYTORCH_INDEX.format(tag=_rocm_tag()),
                supported=False,
                reason="检测到 AMD 显卡，但 PyTorch 官方 ROCm 轮子目前仅支持 Linux",
                note="Windows 下建议：① 使用 WSL2 + ROCm，或 ② 继续用 CPU 推理。"
                     "也可关注 https://rocm.docs.amd.com 的 Windows 预览版。",
            )
        return InstallPlan(
            build="rocm",
            index_url=PYTORCH_INDEX.format(tag=_rocm_tag()),
            reason="检测到 AMD 显卡，推荐安装 ROCm 版本",
            note="需要系统已安装对应版本的 ROCm 运行时；安装后需重启本程序。",
        )

    return None


def pip_available() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0
    except Exception:
        return False


def run_pip_install(plan: InstallPlan, on_output=None):
    """执行安装命令，逐行回调输出。返回 (成功?, 错误信息)。

    仅在源码运行模式可用：打包成 exe 后 sys.executable 是应用本身而非 python。
    """
    if getattr(sys, "frozen", False):
        return False, "打包版本无法自动安装依赖，请复制命令到终端手动执行"

    def emit(line: str):
        if on_output:
            try:
                on_output(line)
            except Exception:
                pass

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    emit("$ " + plan.command_text)
    try:
        proc = subprocess.Popen(
            plan.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        return False, f"无法启动 pip: {e}"

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            emit(line.rstrip())
        code = proc.wait()
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        return False, f"安装过程出错: {e}"

    if code != 0:
        return False, f"pip 退出码 {code}，请检查上方日志"
    # 清掉缓存，让界面读到新版本
    global _torch_env_cache, _torch_module
    _torch_env_cache = None
    _torch_module = None
    return True, ""


def summarize(env: TorchEnv) -> list[str]:
    """生成用于界面展示的简短描述行。"""
    lines = []
    if not env.installed:
        lines.append("PyTorch 未安装")
        return lines
    lines.append(f"PyTorch {env.version}（{env.build_label}）")
    if env.torchaudio_version:
        lines.append(f"torchaudio {env.torchaudio_version}")
    if env.torchaudio_mismatch:
        lines.append("⚠ torch 与 torchaudio 版本不一致，可能导致导入失败")
    lines.append(f"Python {env.python_version}")
    return lines
