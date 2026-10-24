# Maple-FunASR-Desktop

A FunASR desktop application based on QFluentWidgets.

Supports speech-to-text transcription with speaker diarization,
multi-format export, and hardware-accelerated inference
(CUDA / ROCm / Intel XPU / CPU).

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
- **ModelScope** — https://modelscope.cn
- **TUNA Mirrors** — https://mirrors.tuna.tsinghua.edu.cn

## Build

```bash
# With uv (recommended)
cd win
uv sync --group build
uv run python main.py
uv run pyinstaller build.spec

# With pip
cd win
pip install -r requirements.txt
python main.py
pip install pyinstaller
pyinstaller build.spec
```
