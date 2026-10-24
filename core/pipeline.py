"""音频处理管线（不依赖 Qt）。

GUI 的 :class:`app.worker.TranscribeWorker` 与 Web 控制台共用同一套流程：
取音频 → 校验 → 识别 → 返回句子列表。

回调约定
--------
``log(msg)``        文本日志
``progress(pct, stage)``  0-100 的粗粒度阶段进度
``should_cancel()`` 返回 True 时尽快退出（在阶段之间检查）
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from app.utils import MediaError, extract_audio, find_ffmpeg, get_media_duration
from core.transcriber import ModelLoadError, transcribe as run_transcribe

LOSSLESS_SUFFIXES = (".wav", ".flac")

# 阶段进度：抽音频 2-15，加载模型 18-32，推理 32-98，整理 98-100
P_EXTRACT = 2
P_EXTRACT_DONE = 15
P_LOAD = 18
P_INFER = 32
P_FINISH = 98


def run_pipeline(
    media_path: str,
    model_name: str,
    device: str,
    *,
    batch_size_s: int = 300,
    batch_size: int = 1,
    max_speakers: int = 0,
    vad_max_seg_sec: int = 30,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[int, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> list[dict]:
    """执行完整处理流程，异常为 :class:`MediaError` / :class:`ModelLoadError`。"""

    def _log(msg: str):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    def _progress(pct: int, stage: str = ""):
        if progress:
            try:
                progress(max(0, min(100, int(pct))), stage)
            except Exception:
                pass

    def _cancelled() -> bool:
        try:
            return bool(should_cancel and should_cancel())
        except Exception:
            return False

    if not os.path.isfile(media_path):
        raise MediaError("文件不存在", media_path)

    audio_path: Optional[str] = None
    temp_created = False
    try:
        ffmpeg = find_ffmpeg()
        duration = get_media_duration(media_path, ffmpeg)

        if ffmpeg:
            _log(f"[系统] FFmpeg: {ffmpeg}")
            if duration:
                _log(f"[系统] 媒体时长 {duration:.1f}s")
            _log("[系统] 正在提取音频…")
            _progress(P_EXTRACT, "提取音频")
            audio_path = extract_audio(
                media_path, ffmpeg, duration=duration,
                progress_cb=lambda p: _progress(
                    max(P_EXTRACT, int(p * P_EXTRACT_DONE / 100)), "提取音频"),
                should_cancel=should_cancel,
            )
            temp_created = True
            _log("[系统] 音频提取完成")
        elif media_path.lower().endswith(LOSSLESS_SUFFIXES):
            # 没有 ffmpeg 时，无损音频可直接交给 FunASR
            audio_path = media_path
            _log("[系统] 未找到 ffmpeg，直接使用原始音频文件")
            _progress(P_EXTRACT_DONE, "读取音频")
            if duration:
                _log(f"[系统] 音频时长 {duration:.1f}s")
        else:
            raise MediaError(
                "未找到 ffmpeg，无法处理视频/压缩音频",
                "请把 ffmpeg.exe 放到程序目录（或 ui/resources 下）；"
                "也可以先自行转成 wav 再导入。")

        if _cancelled():
            raise MediaError("已取消", "")

        _progress(P_LOAD, "加载模型")

        def _on_transcriber_log(msg: str):
            _log(msg)
            if msg.startswith("加载模型"):
                _progress(P_LOAD + 7, "加载模型")
            elif msg.startswith("开始识别"):
                _progress(P_INFER, "推理中")
            elif msg.startswith("识别完成"):
                _progress(P_FINISH, "整理结果")

        result = run_transcribe(
            audio_path=audio_path,
            model_name=model_name,
            device=device,
            batch_size_s=batch_size_s,
            batch_size=batch_size,
            max_speakers=max_speakers,
            vad_max_seg_sec=vad_max_seg_sec,
            log_callback=_on_transcriber_log,
        )
        _progress(100, "完成")
        _log(f"[系统] 完成，共 {len(result)} 条语句")
        return result

    except (MediaError, ModelLoadError):
        raise
    finally:
        if temp_created and audio_path and os.path.exists(audio_path):
            try:
                os.unlink(audio_path)
            except OSError:
                pass
