"""任务监控面板。

处理任务开始后展开，实时展示：

* CPU / 内存 / GPU 占用（数据来自 :mod:`core.hardware_monitor`，
  优先 HardwareMonitor，缺失时自动降级 psutil）；
* 任务进度条与当前阶段、已用时；
* 处理过程日志（时间戳 + 步骤）。
"""

from __future__ import annotations

import datetime
import threading

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy
from qfluentwidgets import (
    CardWidget, StrongBodyLabel, CaptionLabel, ProgressBar, TextBrowser,
)

from core.hardware_monitor import HardwareMonitorSampler, Metric, Sample

_ACCENT = ("#0f6cbd", "#4aa3e8")
_WARN = ("#c47f00", "#e0a02a")
_DANGER = ("#c42b1c", "#f06a5f")
_OK = ("#2e9b4f", "#4bbd74")


def _load_colors(percent: float) -> tuple[str, str]:
    if percent >= 90:
        return _DANGER
    if percent >= 70:
        return _WARN
    return _ACCENT


class _MetricRow(QWidget):
    """一条指标：名称 + 进度条 + 数值说明。"""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.name_label = CaptionLabel(label)
        self.name_label.setFixedWidth(44)
        layout.addWidget(self.name_label)

        self.bar = ProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFixedHeight(10)
        self.bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.bar, 1)

        self.value_label = CaptionLabel("--")
        self.value_label.setFixedWidth(132)
        self.value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.value_label)

        self.detail_label = CaptionLabel("")
        self.detail_label.setFixedWidth(210)
        self.detail_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.detail_label.setStyleSheet("color: rgba(128,128,128,0.95);")
        layout.addWidget(self.detail_label)

    def update_metric(self, metric: Metric):
        if metric.available:
            pct = max(0.0, min(100.0, metric.percent))
            self.bar.setValue(int(pct))
            self.bar.setCustomBarColor(*[QColor(c) for c in _load_colors(pct)])
            self.value_label.setText(f"{pct:.0f}%")
            self.value_label.setStyleSheet("")
        else:
            self.bar.setValue(0)
            self.value_label.setText("--")
        detail = metric.detail
        self.detail_label.setText(detail)
        tooltip = metric.name or detail
        self.setToolTip(tooltip)


class _MonitorWorker(QThread):
    """独立线程采样，避免任何一次传感器读取拖慢界面。"""

    sampled = pyqtSignal(object)

    def __init__(self, interval: float = 1.0, parent=None):
        super().__init__(parent)
        self.interval = interval
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        sampler = HardwareMonitorSampler()
        try:
            while not self._stop.is_set():
                sample = sampler.sample()
                if sample is not None:
                    self.sampled.emit(sample)
                self._stop.wait(self.interval)
        finally:
            sampler.close()


class MonitorPanel(CardWidget):
    """任务监控卡片。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: _MonitorWorker | None = None
        self._started_at: float | None = None
        self._last_percent = 0
        self._stage = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 12)
        root.setSpacing(8)

        # ── 标题 ──
        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(StrongBodyLabel("任务监控"))
        self.state_label = CaptionLabel("")
        header.addWidget(self.state_label)
        header.addStretch()
        self.source_label = CaptionLabel("")
        self.source_label.setStyleSheet("color: rgba(128,128,128,0.9);")
        header.addWidget(self.source_label)
        root.addLayout(header)

        # ── 指标 ──
        self.cpu_row = _MetricRow("CPU")
        self.ram_row = _MetricRow("内存")
        self.gpu_row = _MetricRow("GPU")
        root.addWidget(self.cpu_row)
        root.addWidget(self.ram_row)
        root.addWidget(self.gpu_row)

        # ── 任务进度 ──
        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        self.task_bar = ProgressBar()
        self.task_bar.setRange(0, 100)
        self.task_bar.setValue(0)
        self.task_bar.setFixedHeight(10)
        progress_row.addWidget(self.task_bar, 1)
        self.progress_label = CaptionLabel("0% · 等待开始")
        self.progress_label.setFixedWidth(230)
        self.progress_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        progress_row.addWidget(self.progress_label)
        root.addLayout(progress_row)

        # ── 实时日志 ──
        self.log_view = TextBrowser()
        self.log_view.setFixedHeight(104)
        self.log_view.setStyleSheet(
            "TextBrowser{background: rgba(128,128,128,0.06); border: none;"
            " border-radius: 6px;}")
        root.addWidget(self.log_view)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._refresh_progress_label)

        self.setVisible(False)

    # ─────────── 生命周期 ───────────

    def start_task(self):
        self.log_view.clear()
        self.task_bar.setValue(0)
        self._last_percent = 0
        self._stage = "准备中"
        self._started_at = datetime.datetime.now().timestamp()
        self.state_label.setText("运行中")
        self.state_label.setStyleSheet("color: #0f6cbd;")
        self.setVisible(True)
        self._refresh_progress_label()
        self._elapsed_timer.start()
        self._start_worker()
        self.log("任务开始")

    def stop_task(self, ok: bool, message: str = ""):
        self._elapsed_timer.stop()
        self._stop_worker()
        if ok:
            self.task_bar.setValue(100)
            self._last_percent = 100
            self.state_label.setText("已完成")
            self.state_label.setStyleSheet("color: #2e9b4f;")
            self.log("任务完成")
        else:
            self.state_label.setText("已失败" if message else "已结束")
            self.state_label.setStyleSheet("color: #c42b1c;")
            if message:
                self.log(f"任务失败：{message}")
        self._refresh_progress_label(final=True)

    def shutdown(self):
        self._elapsed_timer.stop()
        self._stop_worker()

    # ─────────── 进度 / 日志 ───────────

    def set_progress(self, percent: int, stage: str = ""):
        percent = max(0, min(100, int(percent)))
        # 进度只增不减，避免阶段切换时跳动
        self._last_percent = max(self._last_percent, percent)
        self.task_bar.setValue(self._last_percent)
        if stage:
            self._stage = stage
        self._refresh_progress_label()

    def log(self, text: str):
        if not text:
            return
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_view.append(f"[{stamp}] {text}")
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _refresh_progress_label(self, final: bool = False):
        elapsed = 0
        if self._started_at:
            elapsed = int(datetime.datetime.now().timestamp() - self._started_at)
        mm, ss = divmod(max(0, elapsed), 60)
        stage = self._stage or "处理中"
        if final:
            self.progress_label.setText(f"{self._last_percent}% · 用时 {mm:02d}:{ss:02d}")
        else:
            self.progress_label.setText(
                f"{self._last_percent}% · {stage} · 用时 {mm:02d}:{ss:02d}")

    # ─────────── 采样 ───────────

    def _start_worker(self):
        if self._worker is not None and self._worker.isRunning():
            return
        self._worker = _MonitorWorker(parent=self)
        self._worker.sampled.connect(self._on_sample)
        self._worker.start()

    def _stop_worker(self):
        worker = self._worker
        self._worker = None
        if worker is not None and worker.isRunning():
            worker.stop()
            worker.wait(1500)

    def _on_sample(self, sample: Sample):
        self.cpu_row.update_metric(sample.cpu)
        self.ram_row.update_metric(sample.ram)
        self.gpu_row.update_metric(sample.gpu)
        text = f"数据源：{sample.source}"
        if sample.notes:
            text += "（" + "；".join(sample.notes) + "）"
        self.source_label.setText(text)
