"""操作系统层面的显卡探测（不依赖 PyTorch）。

PyTorch 是 CPU-only 构建时 ``torch.cuda.is_available()`` 永远返回 False，
但机器上可能确实有 NVIDIA / AMD / Intel 显卡。只靠 torch 判断会得出
"没有显卡" 的错误结论，因此这里直接从驱动 / 注册表 / sysfs 读取硬件信息，
再与 torch 的构建类型做交叉比对。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache

# 明显不是计算卡的适配器关键字
_VIRTUAL_KEYWORDS = (
    "virtual", "remote", "gameviewer", "parsec", "spacedesk",
    "basic display", "basic render", "idd", "meta driver",
    "vmware", "virtualbox", "hyper-v", "citrix", "sunshine",
)

VENDOR_LABELS = {
    "nvidia": "NVIDIA",
    "amd": "AMD",
    "intel": "Intel",
    "apple": "Apple",
    "other": "其他",
}


@dataclass
class RawGpu:
    """一条硬件层面的显卡记录。"""

    vendor: str
    name: str
    vram_gb: float = 0.0
    driver: str = ""
    source: str = ""

    @property
    def vendor_label(self) -> str:
        return VENDOR_LABELS.get(self.vendor, self.vendor)


def _is_virtual(name: str) -> bool:
    low = name.lower()
    if any(k in low for k in _VIRTUAL_KEYWORDS):
        return True
    # "Microsoft Basic Display Adapter" 之类
    return low.startswith("microsoft") and "display" in low


def _guess_vendor(name: str) -> str:
    low = name.lower()
    if any(k in low for k in ("nvidia", "geforce", "quadro", "tesla", "rtx", "gtx")):
        return "nvidia"
    if any(k in low for k in ("amd", "radeon", "ati ", "firepro", "instinct")):
        return "amd"
    if any(k in low for k in ("intel", "arc ", "iris", "uhd graphics", "hd graphics")):
        return "intel"
    if "apple" in low:
        return "apple"
    return "other"


def _run(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.stdout or ""
    except Exception:
        return ""


# ─────────────────────────── Windows ───────────────────────────


def _probe_windows() -> list[RawGpu]:
    gpus: list[RawGpu] = []

    # 1) 注册表：DriverDesc + 精确显存（qwMemorySize，不受 32 位截断影响）
    try:
        import winreg

        base = (r"SYSTEM\CurrentControlSet\Control\Class"
                r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            count = winreg.QueryInfoKey(root)[0]
            for i in range(count):
                sub = winreg.EnumKey(root, i)
                if not (len(sub) == 4 and sub.isdigit()):
                    continue
                try:
                    with winreg.OpenKey(root, sub) as key:
                        name = str(winreg.QueryValueEx(key, "DriverDesc")[0] or "").strip()
                        if not name or _is_virtual(name):
                            continue
                        vram = 0
                        try:
                            vram = int(winreg.QueryValueEx(
                                key, "HardwareInformation.qwMemorySize")[0])
                        except OSError:
                            try:
                                vram = int(winreg.QueryValueEx(key, "HardwareInformation.MemorySize")[0])
                            except OSError:
                                vram = 0
                        driver = ""
                        try:
                            driver = str(winreg.QueryValueEx(key, "DriverVersion")[0] or "")
                        except OSError:
                            pass
                        gpus.append(RawGpu(
                            vendor=_guess_vendor(name),
                            name=name,
                            vram_gb=round(vram / (1024 ** 3), 1) if vram else 0.0,
                            driver=driver,
                            source="registry",
                        ))
                except OSError:
                    continue
    except Exception:
        pass

    # 2) nvidia-smi：拿到更精确的显存与驱动版本
    nv = _probe_nvidia_smi()
    for g in nv:
        for existing in gpus:
            if existing.vendor == "nvidia" and _same_gpu(existing.name, g.name):
                existing.vram_gb = g.vram_gb or existing.vram_gb
                existing.driver = g.driver or existing.driver
                existing.source = "nvidia-smi"
                break
        else:
            gpus.append(g)

    return gpus


def _probe_nvidia_smi() -> list[RawGpu]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits"])
    if not out.strip():
        return []
    driver = ""
    header = _run([exe], timeout=8)
    m = re.search(r"Driver Version:\s*([0-9.]+)", header)
    if m:
        driver = m.group(1)

    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        name = parts[0]
        try:
            vram_mb = float(parts[1])
        except ValueError:
            vram_mb = 0.0
        gpus.append(RawGpu(
            vendor="nvidia",
            name=name,
            vram_gb=round(vram_mb / 1024, 1),
            driver=parts[2] if len(parts) > 2 and parts[2] else driver,
            source="nvidia-smi",
        ))
    return gpus


def _same_gpu(a: str, b: str) -> bool:
    def norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r"\(r\)|\(tm\)|\(c\)", "", s)
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return " ".join(s.split())

    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    return na in nb or nb in na


# ─────────────────────────── Linux ───────────────────────────


def _probe_linux() -> list[RawGpu]:
    gpus: list[RawGpu] = []
    out = _run(["lspci"], timeout=6)
    for line in out.splitlines():
        if not re.search(r"(VGA compatible controller|3D controller|Display controller)", line):
            continue
        name = line.split(":", 2)[-1].strip()
        name = re.sub(r"\[[0-9a-f]{4}:[0-9a-f]{4}\]", "", name, flags=re.I).strip()
        vendor = "nvidia" if "nvidia" in name.lower() else (
            "amd" if ("amd" in name.lower() or "ati" in name.lower()) else (
                "intel" if "intel" in name.lower() else "other"))
        vram = 0.0
        if vendor == "amd":
            for card in _glob_cards():
                try:
                    with open(os.path.join(card, "device", "mem_info_vram_total")) as f:
                        vram = round(int(f.read().strip()) / (1024 ** 3), 1)
                    break
                except Exception:
                    continue
        if vendor == "nvidia":
            nv = _probe_nvidia_smi()
            if nv:
                gpus.extend(nv)
                continue
        gpus.append(RawGpu(vendor=vendor, name=name, vram_gb=vram, source="lspci"))
    return gpus


def _glob_cards() -> list[str]:
    root = "/sys/class/drm"
    try:
        return [os.path.join(root, n) for n in sorted(os.listdir(root))
                if re.fullmatch(r"card\d+", n)]
    except Exception:
        return []


# ─────────────────────────── macOS ───────────────────────────


def _probe_macos() -> list[RawGpu]:
    out = _run(["system_profiler", "SPDisplaysDataType"], timeout=15)
    gpus: list[RawGpu] = []
    name = ""
    vram = 0.0
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Chipset Model:"):
            name = line.split(":", 1)[1].strip()
        elif "VRAM" in line and ":" in line:
            m = re.search(r"(\d+)\s*(GB|MB)", line)
            if m:
                v = float(m.group(1))
                vram = v if m.group(2) == "GB" else round(v / 1024, 1)
        elif not line and name:
            gpus.append(RawGpu(vendor=_guess_vendor(name), name=name,
                               vram_gb=vram, source="system_profiler"))
            name, vram = "", 0.0
    if name:
        gpus.append(RawGpu(vendor=_guess_vendor(name), name=name,
                           vram_gb=vram, source="system_profiler"))
    return gpus


# ─────────────────────────── 对外接口 ───────────────────────────


@lru_cache(maxsize=1)
def _probe_cached() -> tuple[RawGpu, ...]:
    if os.name == "nt":
        gpus = _probe_windows()
    elif os.uname().sysname == "Darwin":
        gpus = _probe_macos()
    else:
        gpus = _probe_linux()

    # 去重（同名同显存只保留一条，优先保留信息更全的记录）
    uniq: dict[tuple[str, str], RawGpu] = {}
    for g in gpus:
        key = (g.vendor, re.sub(r"\s+", " ", g.name.strip().lower()))
        old = uniq.get(key)
        if old is None or (g.vram_gb and not old.vram_gb) or (g.driver and not old.driver):
            uniq[key] = g
    return tuple(uniq.values())


def probe_gpus(refresh: bool = False) -> list[RawGpu]:
    """返回探测到的显卡列表（结果缓存）。"""
    if refresh:
        _probe_cached.cache_clear()
    try:
        return list(_probe_cached())
    except Exception:
        return []


def vendors_present() -> set[str]:
    return {g.vendor for g in probe_gpus()}
