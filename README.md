# Maple-FunASR-Desktop

A FunASR desktop application based on QFluentWidgets.

Supports speech-to-text transcription with speaker diarization,
multi-format export, and hardware-accelerated inference
(CUDA / ROCm / Intel XPU / CPU).

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
