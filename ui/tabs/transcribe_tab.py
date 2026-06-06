import os
import json

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFileDialog, QTabWidget, QSplitter,
)
from qfluentwidgets import (
    CardWidget, StrongBodyLabel, BodyLabel,
    PrimaryPushButton, ComboBox, SpinBox,
    TextBrowser, InfoBar, InfoBarPosition,
    PushButton, ProgressRing,
)

from core.transcriber import MODEL_REGISTRY
from app.worker import TranscribeWorker


class TranscribeTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.current_video = None
        self.current_result = []
        self._log_forwarder = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        top_splitter = QSplitter(Qt.Orientation.Horizontal)

        # ─── Left: upload + params ───
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setSpacing(10)

        upload_card = CardWidget()
        upload_layout = QVBoxLayout(upload_card)
        upload_layout.addWidget(StrongBodyLabel("Video Input"))
        self.file_path_label = BodyLabel("No file selected")
        self.file_path_label.setStyleSheet("color: #888;")
        upload_layout.addWidget(self.file_path_label)
        browse_btn = PushButton("Browse...")
        browse_btn.clicked.connect(self._browse_file)
        upload_layout.addWidget(browse_btn)
        left_layout.addWidget(upload_card)

        params_card = CardWidget()
        params_layout = QVBoxLayout(params_card)
        params_layout.addWidget(StrongBodyLabel("Parameters"))

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model:"))
        self.model_combo = ComboBox()
        for name in MODEL_REGISTRY:
            self.model_combo.addItem(name)
        model_row.addWidget(self.model_combo)
        model_row.addStretch()
        params_layout.addLayout(model_row)

        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("Device:"))
        self.device_combo = ComboBox()
        self.device_combo.addItems(["auto", "cuda:0", "xpu", "cpu"])
        device_row.addWidget(self.device_combo)
        device_row.addStretch()
        params_layout.addLayout(device_row)

        spk_row = QHBoxLayout()
        spk_row.addWidget(QLabel("Max speakers:"))
        self.max_spk = SpinBox()
        self.max_spk.setRange(0, 20)
        self.max_spk.setValue(0)
        self.max_spk.setToolTip("0 = auto detect")
        spk_row.addWidget(self.max_spk)
        spk_row.addStretch()
        params_layout.addLayout(spk_row)

        vad_row = QHBoxLayout()
        vad_row.addWidget(QLabel("VAD segment (s):"))
        self.vad_seg = SpinBox()
        self.vad_seg.setRange(5, 120)
        self.vad_seg.setValue(30)
        vad_row.addWidget(self.vad_seg)
        vad_row.addStretch()
        params_layout.addLayout(vad_row)

        batch_row = QHBoxLayout()
        batch_row.addWidget(QLabel("Batch size:"))
        self.batch_size = SpinBox()
        self.batch_size.setRange(1, 16)
        self.batch_size.setValue(1)
        batch_row.addWidget(self.batch_size)
        batch_row.addStretch()
        params_layout.addLayout(batch_row)

        left_layout.addWidget(params_card)

        self.transcribe_btn = PrimaryPushButton("Start Transcription")
        self.transcribe_btn.setMinimumHeight(44)
        self.transcribe_btn.clicked.connect(self._start_transcribe)
        left_layout.addWidget(self.transcribe_btn)

        self.progress_ring = ProgressRing()
        self.progress_ring.setRange(0, 100)
        self.progress_ring.setValue(0)
        self.progress_ring.setFixedSize(60, 60)
        self.progress_ring.setVisible(False)
        progress_row = QHBoxLayout()
        progress_row.addStretch()
        progress_row.addWidget(self.progress_ring)
        progress_row.addStretch()
        left_layout.addLayout(progress_row)

        left_layout.addStretch()
        top_splitter.addWidget(left_widget)

        # ─── Right: results ───
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)

        right_tabs = QTabWidget()
        self.text_result = TextBrowser()
        self.text_result.setPlaceholderText("Results will appear here...")
        right_tabs.addTab(self.text_result, "Per-sentence")

        self.json_result = TextBrowser()
        self.json_result.setPlaceholderText("JSON structure will appear here...")
        right_tabs.addTab(self.json_result, "JSON")

        right_layout.addWidget(right_tabs)

        export_row = QHBoxLayout()
        self.export_combo = ComboBox()
        self.export_combo.addItems(["TXT", "JSON", "SRT", "CSV", "MD"])
        export_row.addWidget(QLabel("Export format:"))
        export_row.addWidget(self.export_combo)
        export_btn = PushButton("Export")
        export_btn.clicked.connect(self._export_result)
        export_row.addWidget(export_btn)
        export_row.addStretch()
        right_layout.addLayout(export_row)

        top_splitter.addWidget(right_widget)
        top_splitter.setSizes([350, 650])

        layout.addWidget(top_splitter)

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select video file", "",
            "Media files (*.mp4 *.avi *.mkv *.mov *.wav *.mp3 *.m4a *.flac)"
        )
        if path:
            self.current_video = path
            self.file_path_label.setText(os.path.basename(path))
            self.file_path_label.setStyleSheet("color: #000;")

    def _start_transcribe(self):
        if not self.current_video:
            InfoBar.warning("No file", "Please select a video file first",
                            self, 3000, InfoBarPosition.TOP)
            return

        if self.worker and self.worker.isRunning():
            InfoBar.warning("Busy", "Transcription already in progress",
                            self, 3000, InfoBarPosition.TOP)
            return

        self.text_result.clear()
        self.json_result.clear()
        self.current_result = []
        self.transcribe_btn.setEnabled(False)
        self.progress_ring.setVisible(True)
        self.progress_ring.setValue(0)

        device = self.device_combo.currentText()
        if device == "auto":
            from core.hardware_checker import detect_all, pick_best_device
            devices = detect_all()
            best = pick_best_device(devices)
            device = "cuda:0" if best and best.backend.value != "CPU" else "cpu"

        self.worker = TranscribeWorker(
            video_path=self.current_video,
            model_name=self.model_combo.currentText(),
            device=device,
            batch_size_s=300,
            batch_size=self.batch_size.value(),
            max_speakers=self.max_spk.value(),
            vad_max_seg_sec=self.vad_seg.value(),
        )
        self.worker.log_signal.connect(self._on_log)
        if self._log_forwarder:
            self.worker.log_signal.connect(self._log_forwarder)
        self.worker.result_signal.connect(self._on_result)
        self.worker.error_signal.connect(self._on_error)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.start()

    def _on_log(self, msg: str):
        self.text_result.append(msg)

    def _on_result(self, sentences: list[dict]):
        self.current_result = sentences
        self._display_results(sentences)
        self.progress_ring.setValue(100)

    def _on_error(self, msg: str):
        InfoBar.error("Transcription failed", msg, self, 6000,
                      InfoBarPosition.TOP)
        self.progress_ring.setVisible(False)
        self.transcribe_btn.setEnabled(True)

    def _on_finished(self):
        self.transcribe_btn.setEnabled(True)
        QTimer.singleShot(2000, lambda: self.progress_ring.setVisible(False))

    def _display_results(self, sentences: list[dict]):
        if not sentences:
            self.text_result.append("(No content identified)")
            self.json_result.setText("[]")
            return

        lines = []
        for s in sentences:
            start = s["start_ms"] / 1000
            end = s["end_ms"] / 1000
            lines.append(
                f"[Speaker {s['spk']}]  [{start:.1f}s - {end:.1f}s]  {s['text']}"
            )
        self.text_result.clear()
        self.text_result.append("\n\n".join(lines))
        self.json_result.setText(
            json.dumps(sentences, ensure_ascii=False, indent=2)
        )

    def _export_result(self):
        if not self.current_result:
            InfoBar.warning("Export", "No results to export",
                            self, 3000, InfoBarPosition.TOP)
            return

        fmt = self.export_combo.currentText()
        from core.exporter import export_result

        path, _ = QFileDialog.getSaveFileName(
            self, "Export file", f"transcript.{fmt.lower()}",
            f"{fmt} files (*.{fmt.lower()})"
        )
        if path:
            try:
                export_result(self.current_result, fmt, path)
                InfoBar.success("Exported", f"Saved to {path}",
                                self, 3000, InfoBarPosition.TOP)
            except Exception as e:
                InfoBar.error("Export failed", str(e), self, 5000,
                              InfoBarPosition.TOP)
