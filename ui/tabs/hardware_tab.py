from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPainter, QColor, QPen, QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QRadioButton, QButtonGroup, QSpinBox,
    QGroupBox, QFrame,
)
from qfluentwidgets import (
    CardWidget, StrongBodyLabel, BodyLabel,
    PrimaryPushButton, PushButton,
)

from core.hardware_checker import (
    detect_all, estimate_speeds, pick_best_device,
    Backend, DeviceInfo, SpeedEstimate,
)
from app.utils import format_duration


BACKEND_COLORS = {
    Backend.CUDA: "#76B900",
    Backend.ROCM: "#FF4D00",
    Backend.XPU: "#0071C5",
    Backend.CPU: "#888888",
}


class SpeedBarWidget(QWidget):
    def __init__(self, estimates: list[SpeedEstimate], audio_duration_s: float, parent=None):
        super().__init__(parent)
        self.estimates = estimates
        self.audio_duration_s = audio_duration_s
        self.setMinimumHeight(160)
        self.setMaximumHeight(220)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.estimates:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        bar_h = 28
        gap = 8
        label_w = 100
        bar_max_w = w - label_w - 80

        painter.setFont(QFont("Microsoft YaHei UI", 9))

        max_rtf = max((e.realtime_factor for e in self.estimates if e.available), default=1)

        y = 10
        for est in self.estimates:
            color = BACKEND_COLORS.get(est.backend, "#888")

            painter.setPen(Qt.GlobalColor.black)
            painter.drawText(4, y, label_w - 8, bar_h,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             est.backend.value)

            bar_bg = QColor(240, 240, 240)
            painter.fillRect(label_w, y, bar_max_w, bar_h, bar_bg)

            if est.available and max_rtf > 0:
                bar_w = int(bar_max_w * (est.realtime_factor / max_rtf))
                bar_w = max(bar_w, 2)

                painter.setBrush(QColor(color))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(label_w, y, bar_w, bar_h, 4, 4)

                rt_str = f"{est.realtime_factor:.0f}x realtime"
                est_str = format_duration(est.estimated_seconds)
                text = f"{rt_str}  (~{est_str})"
                painter.setPen(Qt.GlobalColor.white if est.realtime_factor > 20 else Qt.GlobalColor.black)
                painter.drawText(label_w + 6, y, bar_w - 12, bar_h,
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 text)
            else:
                painter.setPen(QColor("#999"))
                painter.drawText(label_w + 6, y, bar_max_w - 12, bar_h,
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 "Unavailable")

            y += bar_h + gap

        painter.end()


class DeviceCard(CardWidget):
    def __init__(self, info: DeviceInfo, parent=None):
        super().__init__(parent)
        self.info = info

        layout = QHBoxLayout(self)
        layout.setSpacing(12)

        badge = QLabel()
        badge.setFixedSize(10, 10)
        badge.setStyleSheet(
            f"background-color: {'#4CAF50' if info.available else '#ccc'};"
            f"border-radius: 5px;"
        )
        layout.addWidget(badge)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        name_label = StrongBodyLabel(f"{info.backend.value}  {info.name}")
        text_col.addWidget(name_label)

        if info.available:
            parts = [f"VRAM: {info.vram_gb:.1f} GB"]
            if info.description:
                parts.append(info.description)
            text_col.addWidget(BodyLabel(" | ".join(parts)))
        else:
            text_col.addWidget(BodyLabel(info.description or "Device not detected"))

        layout.addLayout(text_col, 1)
        layout.addStretch()


class HardwareTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.devices: list[DeviceInfo] = []
        self.estimates: list[SpeedEstimate] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        title = StrongBodyLabel("Hardware Detection & Analysis")
        layout.addWidget(title)

        top_row = QHBoxLayout()

        detect_btn = PrimaryPushButton("Detect Hardware")
        detect_btn.clicked.connect(self._run_detection)
        top_row.addWidget(detect_btn)

        top_row.addSpacing(24)
        top_row.addWidget(QLabel("Audio duration:"))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 600)
        self.duration_spin.setValue(30)
        self.duration_spin.setSuffix(" min")
        self.duration_spin.valueChanged.connect(self._update_estimates)
        top_row.addWidget(self.duration_spin)
        top_row.addStretch()
        layout.addLayout(top_row)

        self.device_group = QGroupBox("Detected Devices")
        self.device_layout = QVBoxLayout(self.device_group)
        layout.addWidget(self.device_group)

        self.speed_group = QGroupBox("Estimated Transcription Speed")
        self.speed_layout = QVBoxLayout(self.speed_group)
        self.speed_chart = SpeedBarWidget([], 1800)
        self.speed_layout.addWidget(self.speed_chart)
        layout.addWidget(self.speed_group)

        scheme_group = QGroupBox("Decode Scheme")
        scheme_layout = QVBoxLayout(scheme_group)

        self.scheme_group = QButtonGroup(self)
        schemes = [
            ("auto", "Auto (Recommended)  CUDA > ROCm > XPU > CPU"),
            ("cuda", "CUDA / ROCm"),
            ("xpu", "Intel XPU"),
            ("cpu", "CPU (Software)"),
        ]
        for val, label in schemes:
            rb = QRadioButton(label)
            self.scheme_group.addButton(rb)
            scheme_layout.addWidget(rb)

        self.scheme_group.buttons()[0].setChecked(True)

        self.scheme_result = BodyLabel("")
        scheme_layout.addWidget(self.scheme_result)
        layout.addWidget(scheme_group)

        layout.addStretch()

        QTimer.singleShot(200, self._run_detection)

    def _run_detection(self):
        self.devices = detect_all()
        self._update_ui()

    def _update_estimates(self):
        duration_s = self.duration_spin.value() * 60
        if self.devices:
            self.estimates = estimate_speeds(self.devices, duration_s)
            self.speed_chart.estimates = self.estimates
            self.speed_chart.audio_duration_s = duration_s
            self.speed_chart.update()
            self._update_scheme_result(duration_s)

    def _update_ui(self):
        for i in reversed(range(self.device_layout.count())):
            w = self.device_layout.itemAt(i).widget()
            if w:
                w.setParent(None)

        for d in self.devices:
            card = DeviceCard(d)
            self.device_layout.addWidget(card)

        self._update_estimates()

    def _update_scheme_result(self, duration_s: float = 1800):
        best = pick_best_device(self.devices)
        if best:
            est = None
            for e in self.estimates:
                if e.backend == best.backend:
                    est = e
                    break
            time_str = format_duration(est.estimated_seconds) if est else "?"
            self.scheme_result.setText(
                f"Selected scheme: {best.backend.value}  "
                f"(estimated {time_str} for {format_duration(duration_s)} audio)"
            )
        else:
            self.scheme_result.setText("No available device detected")

    def get_selected_device(self) -> str:
        checked = self.scheme_group.checkedButton()
        if not checked:
            return "auto"
        if "cuda" in checked.text().lower():
            return "cuda:0"
        if "xpu" in checked.text().lower():
            return "xpu"
        if "cpu" in checked.text().lower():
            return "cpu"
        return "auto"
