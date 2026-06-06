import os
from PyQt6.QtCore import QThread, pyqtSignal

from app.utils import extract_audio, get_media_duration, find_ffmpeg
from core.transcriber import transcribe as run_transcribe


class TranscribeWorker(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int)
    result_signal = pyqtSignal(list)
    error_signal = pyqtSignal(str)
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

    def run(self):
        audio_path = None
        try:
            ffmpeg = find_ffmpeg()
            self.log_signal.emit(f"[System] FFmpeg: {ffmpeg}")

            self.log_signal.emit("[System] 提取音频中...")
            audio_path = extract_audio(self.video_path, ffmpeg)
            duration = get_media_duration(self.video_path, ffmpeg)
            self.log_signal.emit(f"[System] 音频时长: {duration:.0f}s")

            def log(msg):
                self.log_signal.emit(msg)

            result = run_transcribe(
                audio_path=audio_path,
                model_name=self.model_name,
                device=self.device,
                batch_size_s=self.batch_size_s,
                batch_size=self.batch_size,
                max_speakers=self.max_speakers,
                vad_max_seg_sec=self.vad_max_seg_sec,
                log_callback=log,
            )

            self.result_signal.emit(result)
            self.log_signal.emit(f"[System] 完成 - 共 {len(result)} 条语句")

        except Exception as e:
            self.error_signal.emit(str(e))
            self.log_signal.emit(f"[Error] {str(e)}")
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass
            self.finished_signal.emit()
