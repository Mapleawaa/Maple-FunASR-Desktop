from dataclasses import dataclass
from enum import Enum


class Backend(str, Enum):
    CUDA = "CUDA"
    ROCM = "ROCm"
    XPU = "Intel XPU"
    CPU = "CPU"


@dataclass
class DeviceInfo:
    backend: Backend
    name: str
    vram_gb: float
    available: bool
    description: str = ""


@dataclass
class SpeedEstimate:
    backend: Backend
    device_name: str
    realtime_factor: float
    estimated_seconds: float
    available: bool


# Benchmarks from FunASR official: SenseVoiceSmall
# Reference: A100 80G at 170x realtime
# Scaled by VRAM ratio * architecture factor
ARCH_FACTOR = {
    Backend.CUDA: 1.0,
    Backend.ROCM: 0.85,
    Backend.XPU: 0.55,
    Backend.CPU: 0.035,
}


def detect_all() -> list[DeviceInfo]:
    results: list[DeviceInfo] = []

    try:
        import torch

        # CUDA / ROCm
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                name = torch.cuda.get_device_name(i)
                props = torch.cuda.get_device_properties(i)
                vram = props.total_memory / (1024**3)
                is_hip = getattr(torch.version, "hip", None) is not None
                backend = Backend.ROCM if is_hip else Backend.CUDA
                results.append(DeviceInfo(
                    backend=backend,
                    name=name,
                    vram_gb=round(vram, 1),
                    available=True,
                    description=f"显存: {vram:.1f} GB | 核心: {props.multi_processor_count} SM",
                ))
        else:
            is_hip = getattr(torch.version, "hip", None) is not None
            if is_hip:
                results.append(DeviceInfo(
                    backend=Backend.ROCM, name="AMD GPU (HIP detected)",
                    vram_gb=0, available=False,
                    description="ROCm 可用但未检测到设备，请检查驱动",
                ))
            else:
                results.append(DeviceInfo(
                    backend=Backend.CUDA, name="NVIDIA GPU",
                    vram_gb=0, available=False,
                    description="未检测到 CUDA 设备",
                ))
                results.append(DeviceInfo(
                    backend=Backend.ROCM, name="AMD GPU",
                    vram_gb=0, available=False,
                    description="未检测到 ROCm 设备",
                ))

        # Intel XPU
        xpu_avail = False
        try:
            import intel_extension_for_pytorch as ipex  # noqa: F401
            if hasattr(torch, "xpu") and torch.xpu.is_available():
                for i in range(torch.xpu.device_count()):
                    name = f"Intel GPU {i}"
                    try:
                        name = torch.xpu.get_device_name(i)
                    except Exception:
                        pass
                    vram = 0
                    try:
                        vram = torch.xpu.get_device_properties(i).total_memory / (1024**3)
                    except Exception:
                        pass
                    results.append(DeviceInfo(
                        backend=Backend.XPU, name=name,
                        vram_gb=round(vram, 1), available=True,
                    ))
                    xpu_avail = True
        except ImportError:
            pass
        if not xpu_avail:
            results.append(DeviceInfo(
                backend=Backend.XPU, name="Intel GPU",
                vram_gb=0, available=False,
                description="未安装 intel-extension-for-pytorch",
            ))

    except ImportError:
        for b in Backend:
            if b == Backend.CPU:
                continue
            results.append(DeviceInfo(
                backend=b, name=f"{b.value} GPU",
                vram_gb=0, available=False,
                description="PyTorch 未安装",
            ))

    # CPU always available
    import platform
    results.append(DeviceInfo(
        backend=Backend.CPU, name=f"{platform.processor() or 'CPU'}",
        vram_gb=0, available=True,
        description="软件解码（CPU）",
    ))

    return results


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
            vram_scale = min(1.0, d.vram_gb / 24.0) if d.vram_gb > 0 else 0.5
            rt = ref_realtime * ARCH_FACTOR.get(d.backend, 1.0) * vram_scale
        rt = max(rt, 0.1)
        est = audio_duration_s / rt
        estimates.append(SpeedEstimate(
            backend=d.backend, device_name=d.name,
            realtime_factor=round(rt, 1),
            estimated_seconds=round(est, 1),
            available=True,
        ))
    return estimates


def pick_best_device(devices: list[DeviceInfo]) -> DeviceInfo | None:
    priority = [Backend.CUDA, Backend.ROCM, Backend.XPU, Backend.CPU]
    for b in priority:
        for d in devices:
            if d.backend == b and d.available:
                return d
    return None
