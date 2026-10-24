"""语音转写页。"""

from __future__ import annotations

import json
import os

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFileDialog, QStackedWidget,
    QSplitter,
)
from qfluentwidgets import (
    CardWidget, StrongBodyLabel, CaptionLabel,
    PrimaryPushButton, ComboBox, SpinBox, TextBrowser,
    InfoBar, InfoBarPosition, PushButton, SegmentedWidget,
)

from app.worker import TranscribeWorker
from core.downloader import MODEL_BUNDLES, bundle_status
from core.hardware_checker import detect_all, resolve_device
from app.utils import keep_alive
from core.transcriber import MODEL_REGISTRY, bundle_key_for
from ui.widgets.monitor_panel import MonitorPanel


class _DeviceWorker(QThread):
    done = pyqtSignal(list)

    def run(self):
        try:
            devices = detect_all()
        except Exception:
            devices = []
        self.done.emit(devices)


class TranscribeTab(QWidget):
    model_missing = pyqtSignal(str)      # bundle key，用于跳转到模型页

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker: TranscribeWorker | None = None
        self.current_video: str | None = None
        self.current_result: list[dict] = []
        self._log_forwarder = None
        self._device_worker: _DeviceWorker | None = None
        self._devices_loaded = False
        self._device_options: dict[str, str] = {}
        self._had_error = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ─── 左：输入与参数 ───
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        upload_card = CardWidget()
        upload_layout = QVBoxLayout(upload_card)
        upload_layout.setContentsMargins(18, 14, 18, 14)
        upload_layout.setSpacing(8)
        upload_layout.addWidget(StrongBodyLabel("音频 / 视频"))
        self.file_path_label = CaptionLabel("尚未选择文件")
        self.file_path_label.setStyleSheet("color: #8a8a8a;")
        self.file_path_label.setWordWrap(True)
        upload_layout.addWidget(self.file_path_label)
        browse_btn = PushButton("选择文件…")
        browse_btn.clicked.connect(self._browse_file)
        upload_layout.addWidget(browse_btn)
        left_layout.addWidget(upload_card)

        params_card = CardWidget()
        params_layout = QVBoxLayout(params_card)
        params_layout.setContentsMargins(18, 14, 18, 14)
        params_layout.setSpacing(8)
        params_layout.addWidget(StrongBodyLabel("参数"))

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("模型："))
        self.model_combo = ComboBox()
        for name in MODEL_REGISTRY:
            self.model_combo.addItem(name)
        model_row.addWidget(self.model_combo, 1)
        params_layout.addLayout(model_row)

        self.model_hint = CaptionLabel("")
        self.model_hint.setWordWrap(True)
        params_layout.addWidget(self.model_hint)
        self.model_combo.currentIndexChanged.connect(lambda *_: self._update_model_hint())

        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("设备："))
        self.device_combo = ComboBox()
        self.device_combo.addItem("自动（推荐）")
        self._device_options["自动（推荐）"] = "auto"
        self.device_combo.addItem("CPU")
        self._device_options["CPU"] = "cpu"
        device_row.addWidget(self.device_combo, 1)
        params_layout.addLayout(device_row)

        spk_row = QHBoxLayout()
        spk_row.addWidget(QLabel("说话人上限："))
        self.max_spk = SpinBox()
        self.max_spk.setRange(0, 20)
        self.max_spk.setValue(0)
        self.max_spk.setToolTip("0 表示自动判断")
        spk_row.addWidget(self.max_spk, 1)
        params_layout.addLayout(spk_row)

        vad_row = QHBoxLayout()
        vad_row.addWidget(QLabel("VAD 分段（秒）："))
        self.vad_seg = SpinBox()
        self.vad_seg.setRange(5, 120)
        self.vad_seg.setValue(30)
        vad_row.addWidget(self.vad_seg, 1)
        params_layout.addLayout(vad_row)

        batch_row = QHBoxLayout()
        batch_row.addWidget(QLabel("批大小："))
        self.batch_size = SpinBox()
        self.batch_size.setRange(1, 16)
        self.batch_size.setValue(1)
        batch_row.addWidget(self.batch_size, 1)
        params_layout.addLayout(batch_row)

        left_layout.addWidget(params_card)

        self.transcribe_btn = PrimaryPushButton("开始转写")
        self.transcribe_btn.setMinimumHeight(44)
        self.transcribe_btn.clicked.connect(self._start_transcribe)
        left_layout.addWidget(self.transcribe_btn)

        self.cancel_btn = PushButton("取消")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._cancel_transcribe)
        left_layout.addWidget(self.cancel_btn)

        left_layout.addStretch()
        splitter.addWidget(left)

        # ─── 右：结果 ───
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.result_seg = SegmentedWidget()
        self.result_seg.addItem("text", "逐句结果", onClick=lambda: self.result_stack.setCurrentIndex(0))
        self.result_seg.addItem("json", "JSON", onClick=lambda: self.result_stack.setCurrentIndex(1))
        right_layout.addWidget(self.result_seg)

        self.result_stack = QStackedWidget()
        self.text_result = TextBrowser()
        self.text_result.setPlaceholderText("转写结果会显示在这里…")
        self.json_result = TextBrowser()
        self.json_result.setPlaceholderText("JSON 结构会显示在这里…")
        self.result_stack.addWidget(self.text_result)
        self.result_stack.addWidget(self.json_result)
        right_layout.addWidget(self.result_stack, 1)
        self.result_seg.setCurrentItem("text")

        export_row = QHBoxLayout()
        export_row.addWidget(QLabel("导出格式："))
        self.export_combo = ComboBox()
        self.export_combo.addItems(["TXT", "JSON", "SRT", "CSV", "MD"])
        export_row.addWidget(self.export_combo)
        export_btn = PushButton("导出")
        export_btn.clicked.connect(self._export_result)
        export_row.addWidget(export_btn)
        export_row.addStretch()
        right_layout.addLayout(export_row)

        splitter.addWidget(right)
        splitter.setSizes([360, 640])
        layout.addWidget(splitter, 1)

        # 任务开始后展开的监控区域（CPU / 内存 / GPU + 进度 + 日志）
        self.monitor = MonitorPanel(self)
        layout.addWidget(self.monitor)

        QTimer.singleShot(400, self.refresh_devices)
        QTimer.singleShot(600, self._update_model_hint)

    # ─────────── 设备列表 ───────────

    def showEvent(self, event):
        super().showEvent(event)
        if not self._devices_loaded:
            self.refresh_devices()

    def refresh_devices(self):
        self._update_model_hint()
        if self._device_worker is not None and self._device_worker.isRunning():
            return
        self._device_worker = _DeviceWorker(self)
        self._device_worker.done.connect(self._apply_devices)
        self._device_worker.start()

    def _apply_devices(self, devices: list):
        current = self.device_combo.currentText()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self._device_options = {}

        auto_target = resolve_device("auto", devices) if devices else "cpu"
        auto_label = f"自动（推荐）· {auto_target}"
        self.device_combo.addItem(auto_label)
        self._device_options[auto_label] = "auto"

        for d in devices:
            if d.available and d.backend.value != "CPU":
                label = f"{d.backend.value}　{d.name}"
                self.device_combo.addItem(label)
                self._device_options[label] = d.device_string

        self.device_combo.addItem("CPU")
        self._device_options["CPU"] = "cpu"

        if current in self._device_options:
            self.device_combo.setCurrentText(current)
        else:
            self.device_combo.setCurrentText(auto_label)
        self.device_combo.blockSignals(False)
        self._devices_loaded = True

    def _update_model_hint(self):
        bundle_key = bundle_key_for(self.model_combo.currentText())
        bundle = MODEL_BUNDLES.get(bundle_key)
        if bundle is None:
            self.model_hint.setText("")
            return
        ready = False
        for hub in ("ms", "hf"):
            if bundle_status(bundle, hub).installed:
                ready = True
                break
        if ready:
            self.model_hint.setText("✓ 模型已就绪")
            self.model_hint.setStyleSheet("color: #2e9b4f;")
        else:
            self.model_hint.setText("✗ 模型未下载完整，请先到“模型”页下载")
            self.model_hint.setStyleSheet("color: #d68a00;")

    def _selected_device(self) -> str:
        text = self.device_combo.currentText()
        return self._device_options.get(text, "auto")

    # ─────────── 文件 ───────────

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择音视频文件", "",
            "媒体文件 (*.mp4 *.avi *.mkv *.mov *.flv *.wmv *.wav *.mp3 *.m4a *.flac *.aac)"
        )
        if path:
            self.current_video = path
            name = os.path.basename(path)
            size_mb = os.path.getsize(path) / 1024 ** 2
            self.file_path_label.setText(f"{name}　（{size_mb:.1f} MB）")
            self.file_path_label.setStyleSheet("")

    # ─────────── 模型可用性 ───────────

    def _model_ready(self) -> tuple[bool, str]:
        bundle_key = bundle_key_for(self.model_combo.currentText())
        bundle = MODEL_BUNDLES.get(bundle_key)
        if bundle is None:
            return True, ""
        for hub in ("ms", "hf"):
            if bundle_status(bundle, hub).installed:
                return True, ""
        return False, bundle_key

    # ─────────── 转写 ───────────

    def _start_transcribe(self):
        if not self.current_video:
            InfoBar.warning("未选择文件", "请先选择一个音视频文件",
                            parent=self, duration=3000,
                            position=InfoBarPosition.TOP)
            return

        if self.worker is not None and self.worker.isRunning():
            InfoBar.warning("任务进行中", "已有转写任务在运行",
                            parent=self, duration=2500,
                            position=InfoBarPosition.TOP)
            return

        ready, bundle_key = self._model_ready()
        if not ready:
            name = MODEL_BUNDLES[bundle_key].name
            InfoBar.warning(
                "模型尚未下载",
                f"“{name}”还没下载完成，请先到“模型”页下载，否则会在推理时卡住。",
                parent=self, duration=6000, position=InfoBarPosition.TOP)
            self.model_missing.emit(bundle_key)
            return

        self.text_result.clear()
        self.json_result.clear()
        self.current_result = []
        self.transcribe_btn.setEnabled(False)
        self.transcribe_btn.setText("转写中…")
        self.cancel_btn.setVisible(True)
        self._had_error = False
        device = self._selected_device()
        if device == "auto":
            device = resolve_device("auto")

        self.monitor.start_task()
        self.monitor.log(f"文件：{os.path.basename(self.current_video)}")
        self.monitor.log(f"模型：{self.model_combo.currentText()}　设备：{device}")

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
        self.worker.progress_signal.connect(self._on_progress)
        self.worker.result_signal.connect(self._on_result)
        self.worker.error_signal.connect(self._on_error)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.start()

    def _cancel_transcribe(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.monitor.log("已请求取消，等待当前步骤结束")

    def _on_progress(self, pct: int, stage: str):
        self.monitor.set_progress(pct, stage)

    def _on_log(self, msg: str):
        self.monitor.log(msg)

    def _on_result(self, sentences: list[dict]):
        self.current_result = sentences
        self._display_results(sentences)
        self.monitor.set_progress(100, "完成")

    def _on_error(self, msg: str, hint: str):
        self._had_error = True
        parent = self.window()
        content = f"{msg}\n{hint}" if hint else msg
        InfoBar.error("转写失败", content, parent=parent, duration=8000,
                      position=InfoBarPosition.TOP)
        self.monitor.log(f"[错误] {msg}" + (f"　{hint}" if hint else ""))
        self._reset_buttons()

    def _on_finished(self):
        self._reset_buttons()
        self.monitor.stop_task(not self._had_error,
                               "" if not self._had_error else "转写失败")

    def _reset_buttons(self):
        self.transcribe_btn.setEnabled(True)
        self.transcribe_btn.setText("开始转写")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.setEnabled(True)

    # ─────────── 结果 ───────────

    def _display_results(self, sentences: list[dict]):
        if not sentences:
            self.text_result.setPlainText("（没有识别到内容）")
            self.json_result.setPlainText("[]")
            return

        lines = []
        for s in sentences:
            start = s.get("start_ms", 0) / 1000
            end = s.get("end_ms", 0) / 1000
            lines.append(
                f"[说话人 {s.get('spk', '?')}]  [{start:.1f}s - {end:.1f}s]  {s.get('text', '')}")
        self.text_result.setPlainText("\n\n".join(lines))
        self.json_result.setPlainText(
            json.dumps(sentences, ensure_ascii=False, indent=2))

    def _export_result(self):
        if not self.current_result:
            InfoBar.warning("无法导出", "当前没有转写结果",
                            parent=self, duration=2500,
                            position=InfoBarPosition.TOP)
            return

        fmt = self.export_combo.currentText()
        from core.exporter import export_result

        path, _ = QFileDialog.getSaveFileName(
            self, "导出结果", f"transcript.{fmt.lower()}",
            f"{fmt} 文件 (*.{fmt.lower()})")
        if not path:
            return
        try:
            export_result(self.current_result, fmt, path)
            InfoBar.success("导出成功", path, parent=self,
                            duration=3500, position=InfoBarPosition.TOP)
        except Exception as e:
            InfoBar.error("导出失败", str(e), parent=self,
                          duration=5000, position=InfoBarPosition.TOP)

    # ─────────── 收尾 ───────────

    def shutdown(self):
        self.monitor.shutdown()
        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            if not worker.wait(1500):
                keep_alive(worker)
