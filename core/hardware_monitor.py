"""硬件实时监控（CPU / 内存 / GPU 占用与负载）。

优先使用 **HardwareMonitor**（LibreHardwareMonitorLib 的 Python 绑定）读取
CPU 负载 / 温度、内存占用、GPU 负载 / 显存 / 温度 / 功耗；在 Linux、WSL、
未安装 .NET 或非管理员等情况下自动降级到 psutil + nvidia-smi / sysfs，
保证任何平台上都不会因为监控失败而影响主流程。

用法::

    sampler = HardwareMonitorSampler()
    sample = sampler.sample()      # 约 1 秒一次
    sampler.close()
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Metric:
    """单条指标。percent 为 -1 表示读不到。"""

    key: str
    label: str
    percent: float = -1.0
    detail: str = ""
    name: str = ""

    @property
    def available(self) -> bool:
        return self.percent >= 0

    @property
    def text(self) -> str:
        return f"{self.percent:.0f}%" if self.available else "--"


@dataclass
class Sample:
    ts: float
    cpu: Metric
    ram: Metric
    gpu: Metric
    source: str = ""
    notes: list[str] = field(default_factory=list)


# ─────────────────────────── HardwareMonitor 后端 ───────────────────────────


class _LibreBackend:
    """LibreHardwareMonitorLib（Windows，需 .NET）。"""

    name = "HardwareMonitor"

    def __init__(self):
        import HardwareMonitor  # noqa: F401  触发 CLR / 程序集加载
        from HardwareMonitor.Hardware import Computer

        computer = Computer()
        computer.IsCpuEnabled = True
        computer.IsMemoryEnabled = True
        computer.IsGpuEnabled = True
        computer.Open()
        self._computer = computer

        self._cpu = None
        self._mem = None
        self._gpus = []
        for hw in computer.Hardware:
            kind = str(getattr(hw, "HardwareType", ""))
            name = str(getattr(hw, "Name", "") or "")
            if kind == "Cpu" and self._cpu is None:
                self._cpu = hw
            elif kind == "Memory":
                if "Total" in name or self._mem is None:
                    self._mem = hw
            elif kind.startswith("Gpu"):
                self._gpus.append(hw)

        if self._cpu is None and self._mem is None and not self._gpus:
            raise RuntimeError("HardwareMonitor 未返回任何硬件")

        self._sensors: dict[int, dict[tuple[str, str], object]] = {}
        for hw in [self._cpu, self._mem, *self._gpus]:
            if hw is None:
                continue
            table = {}
            for sensor in hw.Sensors:
                try:
                    table[(str(sensor.SensorType), str(sensor.Name))] = sensor
                except Exception:
                    continue
            self._sensors[id(hw)] = table

        self.notes: list[str] = []
        if self._cpu is not None:
            self._cpu.Update()
            temps = [s for (st, _n), s in self._sensors[id(self._cpu)].items()
                     if st == "Temperature"]
            if temps and all(getattr(s, "Value", None) is None for s in temps):
                self.notes.append("CPU 温度需要管理员权限")

    # --- 传感器读取 ---
    def _value(self, hw, stype: str, *names: str) -> Optional[float]:
        if hw is None:
            return None
        table = self._sensors.get(id(hw), {})
        for name in names:
            sensor = table.get((stype, name))
            if sensor is None:
                continue
            try:
                value = sensor.Value
            except Exception:
                continue
            if value is not None:
                return float(value)
        return None

    def _first_value(self, hw, stype: str, contains: str = "") -> Optional[float]:
        if hw is None:
            return None
        for (st, name), sensor in self._sensors.get(id(hw), {}).items():
            if st != stype:
                continue
            if contains and contains.lower() not in name.lower():
                continue
            try:
                value = sensor.Value
            except Exception:
                continue
            if value is not None:
                return float(value)
        return None

    def read(self) -> Sample:
        for hw in [self._cpu, self._mem, *self._gpus]:
            if hw is None:
                continue
            try:
                hw.Update()
            except Exception:
                pass

        # CPU
        cpu_load = self._value(self._cpu, "Load", "CPU Total")
        if cpu_load is None:
            cpu_load = self._value(self._cpu, "Load", "CPU Core Max")
        cpu_temp = self._value(self._cpu, "Temperature", "Core Average", "CPU Package")
        if cpu_temp is None:
            cpu_temp = self._first_value(self._cpu, "Temperature")
        cpu_name = str(getattr(self._cpu, "Name", "") or "")
        cpu_detail = []
        if cpu_temp is not None:
            cpu_detail.append(f"{cpu_temp:.0f}°C")
        cpu_clock = self._first_value(self._cpu, "Clock", "Bus")
        if cpu_clock:
            cpu_detail.append(f"总线 {cpu_clock:.0f} MHz")
        if not cpu_detail:
            try:
                import psutil

                cpu_detail.append(f"{psutil.cpu_count(logical=True)} 线程")
            except Exception:
                pass
        cpu = Metric("cpu", "CPU", cpu_load if cpu_load is not None else -1,
                     " · ".join(cpu_detail), cpu_name)

        # 内存（HardwareMonitor 返回 GB）
        mem_load = self._value(self._mem, "Load", "Memory")
        used = self._value(self._mem, "Data", "Memory Used")
        avail = self._value(self._mem, "Data", "Memory Available")
        detail = ""
        if used is not None and avail is not None:
            detail = f"{used:.1f} / {used + avail:.1f} GB"
            if mem_load is None and used + avail > 0:
                mem_load = used / (used + avail) * 100
        ram = Metric("ram", "内存", mem_load if mem_load is not None else -1,
                     detail, "系统内存")

        # GPU（多卡取第一块）
        gpu = Metric("gpu", "GPU", -1, "", "")
        for hw in self._gpus:
            load = self._value(hw, "Load", "GPU Core")
            if load is None:
                load = self._first_value(hw, "Load", "D3D 3D")
            if load is None and hw is not self._gpus[0]:
                continue
            temp = self._value(hw, "Temperature", "GPU Core")
            vram_used = self._value(hw, "SmallData", "GPU Memory Used")
            vram_total = self._value(hw, "SmallData", "GPU Memory Total")
            power = self._value(hw, "Power", "GPU Package")
            bits = []
            if temp is not None:
                bits.append(f"{temp:.0f}°C")
            if vram_used is not None and vram_total:
                bits.append(f"显存 {vram_used / 1024:.1f}/{vram_total / 1024:.1f} GB")
            if power is not None:
                bits.append(f"{power:.0f} W")
            gpu = Metric("gpu", "GPU", load if load is not None else -1,
                         " · ".join(bits), str(getattr(hw, "Name", "") or ""))
            break

        return Sample(ts=time.time(), cpu=cpu, ram=ram, gpu=gpu,
                      source=self.name, notes=list(self.notes))

    def close(self):
        try:
            self._computer.Close()
        except Exception:
            pass


# ─────────────────────────── psutil 后端 ───────────────────────────


class _PsutilBackend:
    """跨平台兜底：psutil + nvidia-smi / AMD sysfs。"""

    name = "psutil"

    def __init__(self):
        import psutil

        self._psutil = psutil
        psutil.cpu_percent(interval=None)      # 预热，否则第一次恒为 0
        self._last_gpu = 0.0
        self._cache_gpu: Optional[Metric] = None
        self.notes: list[str] = []

    # --- GPU ---
    def _nvidia_smi(self) -> Optional[Metric]:
        exe = shutil.which("nvidia-smi")
        if not exe:
            return None
        try:
            out = subprocess.run(
                [exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4,
                creationflags=_CREATE_NO_WINDOW,
            ).stdout.strip()
        except Exception:
            return None
        if not out:
            return None
        line = out.splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            return None
        name = parts[0]
        try:
            load = float(parts[1])
            used = float(parts[2]) / 1024
            total = float(parts[3]) / 1024
        except ValueError:
            return None
        bits = [f"显存 {used:.1f}/{total:.1f} GB"]
        if len(parts) > 4 and parts[4].replace(".", "", 1).isdigit():
            bits.insert(0, f"{float(parts[4]):.0f}°C")
        return Metric("gpu", "GPU", load, " · ".join(bits), name)

    def _amd_linux(self) -> Optional[Metric]:
        root = "/sys/class/drm"
        try:
            cards = [n for n in sorted(os.listdir(root)) if re.fullmatch(r"card\d+", n)]
        except OSError:
            return None
        for card in cards:
            base = os.path.join(root, card, "device")
            try:
                with open(os.path.join(base, "gpu_busy_percent")) as f:
                    load = float(f.read().strip())
            except Exception:
                continue
            detail = ""
            try:
                with open(os.path.join(base, "mem_info_vram_used")) as f:
                    used = int(f.read().strip()) / 1024 ** 3
                with open(os.path.join(base, "mem_info_vram_total")) as f:
                    total = int(f.read().strip()) / 1024 ** 3
                detail = f"显存 {used:.1f}/{total:.1f} GB"
            except Exception:
                pass
            try:
                with open(os.path.join(base, "hwmon", "hwmon0", "temp1_input")) as f:
                    temp = int(f.read().strip()) / 1000
                detail = f"{temp:.0f}°C" + (f" · {detail}" if detail else "")
            except Exception:
                pass
            return Metric("gpu", "GPU", load, detail, "AMD GPU")
        return None

    def _gpu_every_2s(self) -> Metric:
        now = time.monotonic()
        if self._cache_gpu is not None and now - self._last_gpu < 2.0:
            return self._cache_gpu
        metric = self._nvidia_smi() or self._amd_linux() or Metric(
            "gpu", "GPU", -1, "无可用 GPU 传感器", "")
        self._cache_gpu = metric
        self._last_gpu = now
        return metric

    def read(self) -> Sample:
        psutil = self._psutil
        cpu_percent = psutil.cpu_percent(interval=None)
        cpu_detail = f"{psutil.cpu_count(logical=True)} 线程"
        cpu = Metric("cpu", "CPU", cpu_percent, cpu_detail,
                     platform_name())

        vm = psutil.virtual_memory()
        ram = Metric("ram", "内存", vm.percent,
                     f"{vm.used / 1024 ** 3:.1f} / {vm.total / 1024 ** 3:.1f} GB",
                     "系统内存")

        gpu = self._gpu_every_2s()
        return Sample(ts=time.time(), cpu=cpu, ram=ram, gpu=gpu,
                      source=self.name, notes=list(self.notes))

    def close(self):
        pass


def platform_name() -> str:
    try:
        import platform

        return platform.processor() or platform.machine() or "CPU"
    except Exception:
        return "CPU"


# ─────────────────────────── 对外门面 ───────────────────────────


class HardwareMonitorSampler:
    """带自动降级的采样器；线程不安全，请在同一线程内使用。"""

    def __init__(self, prefer_libre: bool = True):
        self._backend = None
        self._prefer_libre = prefer_libre
        self._lock = threading.Lock()
        self._notes: list[str] = []
        self._init_error = ""

    @property
    def source(self) -> str:
        return getattr(self._backend, "name", "未初始化")

    def _ensure_backend(self):
        if self._backend is not None:
            return
        with self._lock:
            if self._backend is not None:
                return
            if self._prefer_libre:
                try:
                    self._backend = _LibreBackend()
                    return
                except Exception as e:
                    self._init_error = f"{type(e).__name__}: {e}"
            try:
                self._backend = _PsutilBackend()
            except Exception as e:
                self._backend = None
                self._init_error = f"{type(e).__name__}: {e}"

    def sample(self) -> Optional[Sample]:
        self._ensure_backend()
        if self._backend is None:
            return None
        try:
            return self._backend.read()
        except Exception as e:
            # 运行期出错就降级到 psutil，避免一条坏传感器搞崩整个监控
            if not isinstance(self._backend, _PsutilBackend):
                self._init_error = f"{type(self._backend).name}: {e}"
                try:
                    self._backend.close()
                except Exception:
                    pass
                try:
                    self._backend = _PsutilBackend()
                    return self._backend.read()
                except Exception:
                    self._backend = None
                    return None
            self._backend = None
            return None

    def close(self):
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception:
                pass
            self._backend = None
