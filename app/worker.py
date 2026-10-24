"""转写后台任务。

所有异常都在线程内转换成可读的 (message, hint)，通过信号发回主线程，
保证任何失败都不会让程序崩溃或卡死。处理流程本体在 :mod:`core.pipeline`，
与 Web 控制台共用。
"""

from __future__ import annotations

import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.utils import MediaError
from core.pipeline import run_pipeline
from core.transcriber import ModelLoadError


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

    def run(self):
        try:
            result = run_pipeline(
                self.video_path,
                self.model_name,
                self.device,
                batch_size_s=self.batch_size_s,
                batch_size=self.batch_size,
                max_speakers=self.max_speakers,
                vad_max_seg_sec=self.vad_max_seg_sec,
                log=self.log_signal.emit,
                progress=self.progress_signal.emit,
                should_cancel=lambda: self._cancelled,
            )
            self.result_signal.emit(result)
        except ModelLoadError as e:
            self.log_signal.emit(f"[错误] {e}")
            self.error_signal.emit(str(e), e.hint)
        except MediaError as e:
            if str(e) == "已取消":
                self.log_signal.emit("[系统] 任务已取消")
            else:
                self.log_signal.emit(f"[错误] {e}")
                self.error_signal.emit(str(e), e.hint)
        except Exception as e:  # noqa: BLE001
            self.log_signal.emit(f"[错误] {type(e).__name__}: {e}")
            self.log_signal.emit(traceback.format_exc(limit=4))
            self.error_signal.emit(f"转写失败：{type(e).__name__}: {e}",
                                   "可查看日志页获取完整堆栈，或尝试切换设备 / 更换模型。")
        finally:
            self.finished_signal.emit()
