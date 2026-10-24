"""硬件检测与推理设备选择。

修复要点
--------
1. **不再把 "torch 是 CPU 版" 误判为 "没有显卡"**：先用 :mod:`core.gpu_probe`
   从驱动/注册表读真实硬件，再与 torch 构建类型交叉比对；
2. **XPU 检测不再依赖 intel-extension-for-pytorch**：PyTorch 2.5+ 已内置
   ``torch.xpu``，旧逻辑在没装 ipex 时永远报 "不可用"；
3. **ROCm 与 CUDA 区分**：依据 ``torch.version.hip`` 判定，同时给出正确的
   device 字符串（ROCm 走 ``cuda:0``）；
4. 设备状态三态化（可用 / 有硬件缺加速运行时 / 无硬件），并附带可执行的修复建议。
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.gpu_probe import RawGpu, probe_gpus
from core.torch_env import (
    InstallPlan, TorchEnv, get_torch_env, import_torch, suggest_install_plan,
)


class Backend(str, Enum):
    CUDA = "CUDA"
    ROCM = "ROCm"
    XPU = "Intel XPU"
    MPS = "Apple MPS"
    CPU = "CPU"


class DeviceState(str, Enum):
    READY = "ready"                # 现在就能用
    HARDWARE_ONLY = "hardware"     # 硬件在，但当前 torch 跑不起来
    MISSING = "missing"            # 没有对应硬件


@dataclass
class DeviceInfo:
    backend: Backend
    name: str
    state: DeviceState
    index: int = 0
    vram_gb: float = 0.0
    description: str = ""
    hint: str = ""                       # 不可用时的修复建议
    driver: str = ""
    source: str = ""                     # torch / registry / nvidia-smi ...
    vendor: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.state == DeviceState.READY

    @property
    def detected(self) -> bool:
        return self.state in (DeviceState.READY, DeviceState.HARDWARE_ONLY)

    @property
    def device_string(self) -> str:
        if self.backend == Backend.CUDA:
            return f"cuda:{self.index}"
        if self.backend == Backend.ROCM:
            # ROCm 在 PyTorch 里复用 cuda 接口
            return f"cuda:{self.index}"
        if self.backend == Backend.XPU:
            return f"xpu:{self.index}"
        if self.backend == Backend.MPS:
            return "mps"
        return "cpu"


@dataclass
class SpeedEstimate:
    backend: Backend
    device_name: str
    realtime_factor: float
    estimated_seconds: float
    available: bool


# FunASR 官方基准：A100 80G 上 SenseVoiceSmall 约 170x 实时
# 按架构系数 + 显存规模粗略折算，仅用于方案对比
ARCH_FACTOR = {
    Backend.CUDA: 1.0,
    Backend.ROCM: 0.85,
    Backend.XPU: 0.55,
    Backend.MPS: 0.45,
    Backend.CPU: 0.035,
}

_BACKEND_ORDER = [Backend.CUDA, Backend.ROCM, Backend.XPU, Backend.MPS, Backend.CPU]


# ─────────────────────────── torch 侧枚举 ───────────────────────────


def _torch_devices(env: TorchEnv) -> list[DeviceInfo]:
    devices: list[DeviceInfo] = []
    try:
        torch = import_torch()
    except Exception as e:
        return devices

    # CUDA / ROCm：ROCm 构建同样用 torch.cuda 接口
    try:
        cuda_build = getattr(torch.version, "cuda", None)
        hip_build = getattr(torch.version, "hip", None)
        backend = Backend.ROCM if hip_build else Backend.CUDA
        if cuda_build or hip_build:
            if torch.cuda.is_available():
                count = torch.cuda.device_count()
                for i in range(count):
                    try:
                        props = torch.cuda.get_device_properties(i)
                        vram = props.total_memory / (1024 ** 3)
                        sm = getattr(props, "multi_processor_count", 0)
                        name = torch.cuda.get_device_name(i)
                    except Exception:
                        vram, sm, name = 0.0, 0, f"{backend.value} 设备 {i}"
                    runtime = hip_build or cuda_build
                    desc = f"显存 {vram:.1f} GB"
                    if sm:
                        desc += f" | {sm} SM"
                    desc += f" | torch {backend.value} {runtime}"
                    devices.append(DeviceInfo(
                        backend=backend, name=name, state=DeviceState.READY,
                        index=i, vram_gb=round(vram, 1), description=desc,
                        source="torch", vendor="amd" if hip_build else "nvidia",
                        extra={"runtime": runtime},
                    ))
    except Exception:
        pass

    # Intel XPU：torch 2.5+ 原生支持，不需要 ipex
    try:
        if hasattr(torch, "xpu"):
            if torch.xpu.is_available() and torch.xpu.device_count() > 0:
                for i in range(torch.xpu.device_count()):
                    try:
                        name = torch.xpu.get_device_name(i)
                    except Exception:
                        name = f"Intel GPU {i}"
                    vram = 0.0
                    try:
                        vram = torch.xpu.get_device_properties(i).total_memory / (1024 ** 3)
                    except Exception:
                        pass
                    devices.append(DeviceInfo(
                        backend=Backend.XPU, name=name, state=DeviceState.READY,
                        index=i, vram_gb=round(vram, 1),
                        description=f"显存 {vram:.1f} GB | torch XPU",
                        source="torch", vendor="intel",
                    ))
    except Exception:
        pass

    # Apple MPS
    try:
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            devices.append(DeviceInfo(
                backend=Backend.MPS, name="Apple Silicon GPU", state=DeviceState.READY,
                description="Metal 加速", source="torch", vendor="apple",
            ))
    except Exception:
        pass

    return devices


# ─────────────────────────── 组装结果 ───────────────────────────


def _hint_for(vendor: str, env: TorchEnv) -> tuple[str, Optional[InstallPlan]]:
    """给出"硬件在但用不了"的具体原因与修复方式。"""
    plan = suggest_install_plan(vendor, env)

    if not env.installed:
        return "未安装 PyTorch，请先 pip install torch torchaudio", plan

    if env.is_cpu_only:
        label = {"nvidia": "NVIDIA", "amd": "AMD", "intel": "Intel"}.get(vendor, "该")
        if vendor == "amd" and plan is not None and not plan.supported:
            return (f"已检测到 {label} 显卡，但 Windows 下 PyTorch 官方未提供 ROCm 轮子；"
                    f"如需 GPU 加速请使用 WSL2 + ROCm，或改用 CPU 推理", plan)
        return (f"已检测到 {label} 显卡，但当前 PyTorch 是 CPU-only 构建 "
                f"({env.version})，需要安装支持该厂商的版本", plan)

    if vendor == "nvidia" and env.build == "cuda":
        return "CUDA 构建正常但未发现设备，请检查显卡驱动是否为最新版本，或是否在虚拟机/远程桌面中运行", plan
    if vendor == "intel" and env.build == "xpu":
        return "XPU 构建正常但未发现设备，请检查 Intel 显卡驱动版本", plan
    if vendor == "amd" and env.build == "rocm":
        return "ROCm 构建正常但未发现设备，请确认 ROCm 运行时版本与 wheel 匹配", plan
    return "当前 PyTorch 构建不支持该设备", plan


def _hardware_only_devices(gpus: list[RawGpu], covered: list[str],
                           env: TorchEnv) -> list[DeviceInfo]:
    result: list[DeviceInfo] = []
    for gpu in gpus:
        if gpu.vendor not in ("nvidia", "amd", "intel"):
            continue
        # 已被 torch 枚举到的硬件不再重复展示
        if any(_name_matches(gpu.name, c) for c in covered):
            continue
        backend = {"nvidia": Backend.CUDA, "amd": Backend.ROCM, "intel": Backend.XPU}[gpu.vendor]
        hint, _plan = _hint_for(gpu.vendor, env)
        parts = []
        if gpu.vram_gb:
            parts.append(f"显存 {gpu.vram_gb:.1f} GB")
        if gpu.driver:
            parts.append(f"驱动 {gpu.driver}")
        parts.append("硬件已检测到，当前不可用于加速")
        result.append(DeviceInfo(
            backend=backend, name=gpu.name, state=DeviceState.HARDWARE_ONLY,
            vram_gb=gpu.vram_gb, description=" | ".join(parts),
            hint=hint, driver=gpu.driver, source=gpu.source, vendor=gpu.vendor,
        ))
    return result


def _name_matches(hw_name: str, torch_name: str) -> bool:
    import re

    def norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r"\(r\)|\(tm\)|\(c\)", "", s)
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return " ".join(s.split())

    a, b = norm(hw_name), norm(torch_name)
    if not a or not b:
        return False
    return a in b or b in a


def _cpu_device() -> DeviceInfo:
    name = platform.processor() or platform.machine() or "CPU"
    try:
        import os
        cores = os.cpu_count() or 0
    except Exception:
        cores = 0
    desc = f"{cores} 线程 | 软件解码" if cores else "软件解码"
    return DeviceInfo(
        backend=Backend.CPU, name=name, state=DeviceState.READY,
        description=desc, source="platform", vendor="cpu",
    )


def detect_all(refresh: bool = False) -> list[DeviceInfo]:
    """探测所有可用/潜在可用的推理设备。"""
    env = get_torch_env(refresh=refresh)
    gpus = probe_gpus(refresh=refresh)

    devices = _torch_devices(env)
    covered = [d.name for d in devices]
    devices += _hardware_only_devices(gpus, covered, env)
    devices.append(_cpu_device())

    devices.sort(key=lambda d: (_BACKEND_ORDER.index(d.backend)
                                if d.backend in _BACKEND_ORDER else 99,
                                0 if d.available else 1, d.index))
    return devices


def available_devices(devices: Optional[list[DeviceInfo]] = None) -> list[DeviceInfo]:
    devices = devices if devices is not None else detect_all()
    return [d for d in devices if d.available]


def pick_best_device(devices: list[DeviceInfo]) -> Optional[DeviceInfo]:
    for backend in _BACKEND_ORDER:
        for d in devices:
            if d.backend == backend and d.available:
                return d
    return None


def resolve_device(setting: str = "auto", devices: Optional[list[DeviceInfo]] = None) -> str:
    """把 UI 上的设备选项转换为 FunASR/PyTorch 可接受的 device 字符串。"""
    setting = (setting or "auto").strip()
    if setting == "auto":
        best = pick_best_device(devices if devices is not None else detect_all())
        return best.device_string if best else "cpu"

    low = setting.lower()
    if low in ("cpu", "mps"):
        return low
    if low.startswith("cuda"):
        if ":" in low:
            return low
        return "cuda:0"
    if low.startswith("xpu"):
        return low if ":" in low else "xpu:0"
    return setting


def autodetect_warning(devices: Optional[list[DeviceInfo]] = None) -> Optional[str]:
    """启动时给的提示：有显卡但用不了加速。"""
    devices = devices if devices is not None else detect_all()
    hw_only = [d for d in devices if d.state == DeviceState.HARDWARE_ONLY]
    if hw_only:
        first = hw_only[0]
        return f"{first.name} 暂不可用于加速：{first.hint}"
    return None


def estimate_speeds(devices: list[DeviceInfo],
                    audio_duration_s: float = 1800,
                    ref_realtime: float = 170.0) -> list[SpeedEstimate]:
    estimates = []
    for d in devices:
        if not d.available:
            estimates.append(SpeedEstimate(
                backend=d.backend, device_name=d.name,
                realtime_factor=0, estimated_seconds=0, available=False,
            ))
            continue
        if d.backend == Backend.CPU:
            rt = ref_realtime * ARCH_FACTOR[Backend.CPU]
        else:
            vram_scale = min(1.0, max(0.35, d.vram_gb / 24.0)) if d.vram_gb > 0 else 0.5
            rt = ref_realtime * ARCH_FACTOR.get(d.backend, 1.0) * vram_scale
        rt = max(rt, 0.1)
        estimates.append(SpeedEstimate(
            backend=d.backend, device_name=d.name,
            realtime_factor=round(rt, 1),
            estimated_seconds=round(audio_duration_s / rt, 1),
            available=True,
        ))
    return estimates
