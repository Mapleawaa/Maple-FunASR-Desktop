"""转写后台任务。

所有异常都在线程内转换成可读的 (message, hint)，通过信号发回主线程，
保证任何失败都不会让程序崩溃或卡死。
"""

from __future__ import annotations

import os
import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.utils import MediaError, extract_audio, find_ffmpeg, get_media_duration
from core.transcriber import ModelLoadError, transcribe as run_transcribe


class TranscribeWorker(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, str)     # 0-100, 阶段说明
    result_signal = pyqtSignal(list)
    error_signal = pyqtSignal(str, str)        # message, hint
    finished_signal = pyqtSignal()

    def __init__(self, video_path, model_name, device,
                 batch_size_s, batch_size, max_speakers, vad_max_seg_sec,
                 parent=None):
        super().__init__(parent)
        self.video_path = video_path
        self.model_name = model_name
        self.device = device
        self.batch_size_s = batch_size_s
        self.batch_size = batch_size
        self.max_speakers = max_speakers
        self.vad_max_seg_sec = vad_max_seg_sec
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    # ─────────── 内部 ───────────

    def _log(self, msg: str):
        self.log_signal.emit(msg)

    def _progress(self, pct: int, stage: str = ""):
        self.progress_signal.emit(max(0, min(100, pct)), stage)

    def run(self):
        audio_path = None
        try:
            ffmpeg = find_ffmpeg()
            duration = get_media_duration(self.video_path, ffmpeg)

            if ffmpeg:
                self._log(f"[系统] FFmpeg: {ffmpeg}")
                if duration:
                    self._log(f"[系统] 媒体时长 {duration:.1f}s")
                self._log("[系统] 正在提取音频…")
                self._progress(2, "提取音频")
                audio_path = extract_audio(
                    self.video_path, ffmpeg, duration=duration,
                    progress_cb=lambda p: self._progress(max(2, int(p * 0.15)), "提取音频"),
                    should_cancel=lambda: self._cancelled,
                )
                self._log("[系统] 音频提取完成")
            elif self.video_path.lower().endswith((".wav", ".flac")):
                # 没有 ffmpeg 时，无损音频可以直接交给 FunASR
                audio_path = None  # 复用源文件，不要删除
                self._log("[系统] 未找到 ffmpeg，直接使用原始音频文件")
                self._progress(15, "读取音频")
                if duration:
                    self._log(f"[系统] 音频时长 {duration:.1f}s")
            else:
                raise MediaError(
                    "未找到 ffmpeg，无法处理视频/压缩音频",
                    "请把 ffmpeg.exe 放到程序目录（或 ui/resources 下）；"
                    "也可以先自行转成 wav 再导入。")

            if self._cancelled:
                raise MediaError("已取消", "")

            self._progress(18, "加载模型")

            def log(msg):
                self._log(msg)
                if msg.startswith("加载模型"):
                    self._progress(25, "加载模型")
                elif msg.startswith("开始识别"):
                    self._progress(32, "推理中")
                elif msg.startswith("识别完成"):
                    self._progress(98, "整理结果")

            result = run_transcribe(
                audio_path=audio_path or self.video_path,
                model_name=self.model_name,
                device=self.device,
                batch_size_s=self.batch_size_s,
                batch_size=self.batch_size,
                max_speakers=self.max_speakers,
                vad_max_seg_sec=self.vad_max_seg_sec,
                log_callback=log,
            )

            self._progress(100, "完成")
            self.result_signal.emit(result)
            self._log(f"[系统] 完成，共 {len(result)} 条语句")

        except ModelLoadError as e:
            self._log(f"[错误] {e}")
            self.error_signal.emit(str(e), e.hint)
        except MediaError as e:
            if str(e) == "已取消":
                self._log("[系统] 任务已取消")
            else:
                self._log(f"[错误] {e}")
                self.error_signal.emit(str(e), e.hint)
        except Exception as e:  # noqa: BLE001
            self._log(f"[错误] {type(e).__name__}: {e}")
            self._log(traceback.format_exc(limit=4))
            self.error_signal.emit(f"转写失败：{type(e).__name__}: {e}",
                                   "可查看日志页获取完整堆栈，或尝试切换设备 / 更换模型。")
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass
            self.finished_signal.emit()
