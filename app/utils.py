"""通用工具：ffmpeg 定位、音频抽取（带进度）、时长解析。"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from typing import Callable, Optional

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 窗口关闭时仍在运行的线程不能被销毁，统一托管在这里
_BACKGROUND_THREADS: list = []


def keep_alive(thread) -> None:
    """把仍在运行的 QThread 与父控件解绑并保留引用，避免 "Destroyed while running"。"""
    if thread is None:
        return
    try:
        thread.setParent(None)
    except Exception:
        pass
    _BACKGROUND_THREADS.append(thread)


class MediaError(RuntimeError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def _candidates() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    names = ["ffmpeg.exe", "ffmpeg"] if os.name == "nt" else ["ffmpeg"]
    dirs = [
        os.path.dirname(sys.executable),
        os.path.join(root, "ui", "resources"),
        os.path.join(root, "resources"),
        root,
    ]
    paths = [os.path.join(d, n) for d in dirs for n in names]
    paths += names  # PATH 兜底
    return paths


def find_ffmpeg() -> str:
    """返回可用的 ffmpeg 路径；找不到返回空字符串。"""
    for p in _candidates():
        if os.path.isfile(p):
            return os.path.abspath(p)
        try:
            r = subprocess.run([p, "-version"], capture_output=True,
                               timeout=8, creationflags=_CREATE_NO_WINDOW)
            if r.returncode == 0:
                return p
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return ""


def require_ffmpeg() -> str:
    path = find_ffmpeg()
    if not path:
        raise MediaError(
            "未找到 ffmpeg，无法解析音视频文件",
            "请把 ffmpeg.exe 放到程序目录（或 ui/resources 下），或安装后加入 PATH。")
    return path


def safe_temp_path(suffix: str = ".wav") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


def format_duration(seconds: float) -> str:
    m, s = divmod(int(max(0, seconds)), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _wav_duration(path: str) -> float:
    try:
        import wave

        with wave.open(path, "rb") as f:
            frames = f.getnframes()
            rate = f.getframerate() or 1
            return frames / float(rate)
    except Exception:
        return 0.0


def get_media_duration(path: str, ffmpeg_path: str = "") -> float:
    if path.lower().endswith(".wav"):
        duration = _wav_duration(path)
        if duration > 0:
            return duration
    ffmpeg_path = ffmpeg_path or find_ffmpeg()
    if not ffmpeg_path:
        return 0.0
    try:
        r = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-i", path],
            capture_output=True, text=True, timeout=30,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        return 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", r.stderr or "")
    if not m:
        return 0.0
    h, mi, s, frac = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + frac / (10 ** len(str(frac)))


def extract_audio(video_path: str, ffmpeg_path: str = "",
                  duration: float = 0.0,
                  progress_cb: Optional[Callable[[int], None]] = None,
                  should_cancel: Optional[Callable[[], bool]] = None) -> str:
    """把任意音视频转成 16k 单声道 wav，并通过 ffmpeg 的进度输出回报百分比。"""
    ffmpeg_path = ffmpeg_path or require_ffmpeg()
    audio_path = safe_temp_path(".wav")
    cmd = [
        ffmpeg_path, "-hide_banner", "-nostdin", "-y",
        "-i", video_path, "-vn",
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        "-progress", "pipe:1", "-loglevel", "error",
        audio_path,
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError as e:
        raise MediaError("无法启动 ffmpeg", "请确认 ffmpeg.exe 可用。") from e

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if should_cancel and should_cancel():
                proc.kill()
                raise MediaError("已取消", "")
            line = line.strip()
            if not line.startswith("out_time_ms="):
                continue
            try:
                ms = int(line.split("=", 1)[1])
            except ValueError:
                continue
            if duration > 0 and progress_cb:
                pct = min(99, int(ms / 1000 / duration * 100))
                progress_cb(max(0, pct))
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        code = proc.wait()
    except MediaError:
        raise
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        raise MediaError(f"音频抽取失败：{e}") from e

    if code != 0:
        msg = stderr.strip().splitlines()[-1] if stderr.strip() else f"ffmpeg 退出码 {code}"
        raise MediaError(f"音频抽取失败：{msg[:200]}",
                         "请确认文件未损坏，且格式受 ffmpeg 支持。")
    if progress_cb:
        progress_cb(100)
    return audio_path
