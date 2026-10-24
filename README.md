# Maple-FunASR-Desktop

[![Build & Test](https://github.com/Mapleawaa/Maple-FunASR-Desktop/actions/workflows/build.yml/badge.svg)](https://github.com/Mapleawaa/Maple-FunASR-Desktop/actions/workflows/build.yml)

A FunASR desktop application based on QFluentWidgets.

Supports speech-to-text transcription with speaker diarization,
multi-format export, and hardware-accelerated inference
(CUDA / ROCm / Intel XPU / CPU).

Runs on **Windows 10/11**, **WSL2**（有 WSLg 时原生窗口，没有时自动切到内置
Web 控制台）和 **Linux**。

## 支持平台

| 平台 | 状态 | GUI | 加速 | 说明 |
|------|------|-----|------|------|
| Windows 10 / 11 (x64) | ✅ 官方构建 | 原生窗口 | CUDA / Intel XPU / CPU | 发布页 `*-windows-x64.zip` |
| **WSL2 (Ubuntu 22.04+)** | ✅ 支持 | **WSLg**（XWayland / Wayland） | CUDA / Intel XPU（透传） / CPU | 无 WSLg 时自动切换 Web 控制台 |
| WSL1 | ⚠️ 仅 Web 控制台 | ❌ | CPU | 没有 WSLg，建议升级到 WSL2 |
| Linux (x86_64) | ✅ 支持 | X11 / Wayland | CUDA / ROCm / Intel XPU / CPU | 发布页 `*-linux-x86_64.tar.gz`，也可源码运行 |

- **WSL2 上推荐源码运行**（`uv sync && uv run python main.py`），能直接复用 WSL 里的
  CUDA / ROCm 环境；发布页的 Linux 包内置 CPU 版 PyTorch，开箱即用但不含 GPU 运行时。
- WSL2 的 GPU 加速依赖 Windows 侧驱动 + WSL 内对应版本的 PyTorch，本程序只在
  “硬件”页如实报告当前可用性，不会替用户静默安装。

## 功能与实现要点

### 模型下载（“模型”页）

- 按**模型包**下载：主模型 + VAD + 标点 + 说话人分离一起下齐，避免推理时
  触发隐藏下载导致界面像“卡死”。
- 下载在**独立子进程**中执行（`core/download_worker.py`），主界面通过
  `QProcess` 读取 JSON 进度，因此：下载慢不会卡 UI，取消可以真正结束进程，
  底层库崩溃也不会带走主程序。
- 字节级进度：百分比、已下载/总量、速度、剩余时间、当前文件、当前仓库。
- 断点续传：已下载的仓库自动跳过，中断后重新点击即可接着下。
- 去重与队列：同一模型包重复点击不会起第二个进程，多个包按队列串行下载。
- 失败可诊断：网络 / 认证 / 仓库不存在 / 磁盘不足分别给出处理建议；
  90 秒没有新数据判定为网络阻塞并自动中止。

### 任务监控（“转写”页）

- 任务开始后自动展开监控区：CPU / 内存 / GPU 占用条 + 数值与温度、显存、功耗。
- 数据源优先 **HardwareMonitor**（LibreHardwareMonitorLib），在 Linux / WSL /
  未安装 .NET / 非管理员时自动降级为 psutil + `nvidia-smi` / AMD sysfs，
  监控不可用也不会影响转写。
- 采样在独立线程每秒一次，同时展示任务进度（阶段 + 百分比 + 用时）与带时间
  戳的处理日志。
- CPU 温度等部分传感器需要以管理员身份运行；界面会标注"需要管理员权限"。

### WSL / WSLg 与 Web 控制台

- **环境识别**：通过 `WSL_DISTRO_NAME`、`/proc/version`、`/mnt/wslg` 判断 WSL
  与 WSLg 是否就绪。
- **WSLg 显示**：优先使用 `xcb`（WSLg 自带 XWayland），失败再依次尝试
  `wayland` 与软件渲染。探测在**子进程**中执行 —— Qt 平台插件缺失会让进程
  直接 abort，不能放在主进程里试。
- **兜底入口**：WSLg 不可用、或 GUI 启动失败时，自动启动 Flask Web 控制台
  （`http://127.0.0.1:8420`），浏览器里即可上传文件、转写、导出结果，
  同时可以看 CPU/内存/GPU 曲线式的实时负载与处理日志。
- **命令行**：

  ```bash
  python main.py                     # 自动：能显示 GUI 就 GUI，否则 Web
  python main.py --web               # 强制 Web 控制台
  python main.py --gui               # 强制 GUI，失败直接退出（不降级）
  python main.py --web --host 0.0.0.0 --port 8420   # 让局域网可访问
  python -m app.web_console.server --port 8420      # 只跑 Web 端（无 Qt）
  ```

- WSL 里若提示缺少 xcb 依赖：

  ```bash
  sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libxcb-xinerama0
  ```

- 安全提示：Web 控制台默认只监听 `127.0.0.1` 且没有鉴权，使用
  `--host 0.0.0.0` 暴露到局域网前请自行评估。

### 硬件检测与 PyTorch 版本（“硬件”页）

- 先用注册表 / `nvidia-smi` / sysfs 读**真实硬件**，再与 PyTorch 构建类型
  交叉比对，因此不会再出现“装了显卡却报未检测到 CUDA 设备”的误判。
- Intel XPU 判定改为 PyTorch 2.5+ 内置的 `torch.xpu`，不再强依赖
  `intel-extension-for-pytorch`。
- 明确区分 CUDA / ROCm（ROCm 走 `cuda:0`）并显示对应 device 字符串。
- 展示当前 PyTorch 构建（`+cpu` / `+cu126` / `+rocm6.x` / `+xpu`）、
  torchaudio 版本一致性；CPU-only 但存在独显时给出可直接执行的安装命令，
  源码运行时支持一键安装（打包版请复制命令到终端执行）。

### 其他

- `funasr` / `torch` 全部延迟导入，启动不再卡几秒。
- 缺少 ffmpeg 时，`wav` / `flac` 可直接送入识别；其他格式会给出明确提示。
- SenseVoice 输出会做富文本后处理，去掉 `<|zh|><|EMO_...|>` 等标记。

## License

Copyright (C) 2026 Maple-FunASR-Desktop

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

## Third-party Components

- **FFmpeg** — LGPLv3 — https://ffmpeg.org
- **FunASR** — MIT License — https://github.com/modelscope/FunASR
- **PyQt6** — GPL v3 — https://www.qt.io
- **QFluentWidgets** — GPL v3 — https://github.com/zhiyiYo/QFluentWidgets
- **LibreHardwareMonitor / HardwareMonitor** — MPL-2.0 — https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
- **Flask** — BSD-3-Clause — https://flask.palletsprojects.com
- **ModelScope** — https://modelscope.cn
- **TUNA Mirrors** — https://mirrors.tuna.tsinghua.edu.cn

## Build

仓库根目录即项目根目录（早期文档里的 `cd win` 已不再需要）。

### Windows

```bash
# 推荐 uv
uv sync --group build
uv run python main.py
uv run pyinstaller build.spec        # 产物：dist/Maple-FunASR-Desktop.exe

# 或用 pip
pip install -r requirements.txt
python main.py
pyinstaller build.spec
```

### Linux / WSL2

```bash
# 源码运行（WSL2 下建议这种方式，可直接复用 WSL 里的 CUDA/ROCm）
sudo apt install ffmpeg libxcb-cursor0 libxkbcommon-x11-0
uv sync
uv run python main.py

# 构建单文件可执行程序（CI 里用 CPU 版 PyTorch 控制体积）
uv sync --group build --no-install-project \
  --no-install-package torch --no-install-package torchaudio
uv pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio
uv run pyinstaller build.spec        # 产物：dist/Maple-FunASR-Desktop
```

`build.spec` 会自动区分平台：Windows 打包 `ffmpeg.exe` 与图标，Linux/WSL2
打包 `ffmpeg`、忽略 Windows 图标；找不到 ffmpeg 时跳过（应用会提示安装）。

### CI

`.github/workflows/build.yml` 依次执行：

| Job | 内容 |
|-----|------|
| `Lint & Import Check` | 全量语法编译 + 核心/GUI 模块导入（headless，不装 torch） |
| `WSL2 Compatibility (no WSLg)` | 用 `WSL_DISTRO_NAME` 模拟无 WSLg 的 WSL2，跑 `tests/test_smoke.py`（运行时判定、降级逻辑、Web 控制台接口） |
| `Build Windows` | PyInstaller 打包 Windows exe |
| `Build Linux / WSL2` | PyInstaller 打包 Linux 可执行文件（CPU 版 PyTorch + 内置 ffmpeg） |
| `Release` | tag 触发，产出 `*-windows-x64.zip` 与 `*-linux-x86_64.tar.gz` |
