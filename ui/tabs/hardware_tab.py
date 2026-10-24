"""硬件检测页：真实反映 CUDA / ROCm / XPU 可用性，并给出 PyTorch 加速版安装方案。

关键修复：
* 检测在后台线程执行，不再阻塞界面启动；
* 有显卡但 torch 为 CPU-only 时明确提示，并给出可直接执行的安装命令；
* 解码方案选项根据实际可用设备动态生成，设备字符串与后端严格对应。
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPalette
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QRadioButton,
    QButtonGroup, QSpinBox, QSizePolicy, QFrame,
)
from qfluentwidgets import (
    CardWidget, StrongBodyLabel, BodyLabel, CaptionLabel,
    PrimaryPushButton, PushButton, TextBrowser, LineEdit,
    InfoBar, InfoBarPosition, SingleDirectionScrollArea,
)

from app.utils import format_duration, keep_alive
from core.hardware_checker import (
    Backend, DeviceInfo, DeviceState, SpeedEstimate,
    detect_all, estimate_speeds, pick_best_device, resolve_device,
)
from core.torch_env import (
    InstallPlan, TorchEnv, get_torch_env, run_pip_install,
    suggest_install_plan, summarize,
)


BACKEND_COLORS = {
    Backend.CUDA: "#76B900",
    Backend.ROCM: "#FF4D00",
    Backend.XPU: "#0071C5",
    Backend.MPS: "#A855F7",
    Backend.CPU: "#888888",
}

_STATE_COLORS = {
    DeviceState.READY: "#2e9b4f",
    DeviceState.HARDWARE_ONLY: "#d68a00",
    DeviceState.MISSING: "#b0b0b0",
}


def _copy_to_clipboard(text: str) -> bool:
    try:
        QGuiApplication.clipboard().setText(text)
        return True
    except Exception:
        return False


# ─────────────────────────── 后台任务 ───────────────────────────


class DetectWorker(QThread):
    """torch 导入可能要几秒，放到后台线程避免界面卡住。"""

    done = pyqtSignal(list)

    def __init__(self, refresh: bool = True, parent=None):
        super().__init__(parent)
        self.refresh = refresh

    def run(self):
        try:
            devices = detect_all(refresh=self.refresh)
        except Exception:
            devices = []
        self.done.emit(devices)


class InstallWorker(QThread):
    line = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, plan: InstallPlan, parent=None):
        super().__init__(parent)
        self.plan = plan

    def run(self):
        try:
            ok, err = run_pip_install(self.plan, on_output=self.line.emit)
        except Exception as e:  # 兜底，绝不把异常抛到主线程
            ok, err = False, str(e)
        self.done.emit(ok, err)


# ─────────────────────────── 速度对比图 ───────────────────────────


class SpeedBarWidget(QWidget):
    def __init__(self, estimates: list[SpeedEstimate], audio_duration_s: float, parent=None):
        super().__init__(parent)
        self.estimates = [e for e in estimates if e.available]
        self.audio_duration_s = audio_duration_s
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(72)

    def preferred_height(self) -> int:
        if len(self.estimates) <= 1:
            return 76
        rows = max(1, len(self.estimates))
        return 16 + rows * 34 + 20

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(QFont("Microsoft YaHei UI", 9))

        text_color = self.palette().color(QPalette.ColorRole.WindowText)
        subtle = QColor(text_color)
        subtle.setAlpha(150)

        if not self.estimates:
            painter.setPen(subtle)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "暂无可用于加速的设备")
            painter.end()
            return

        if len(self.estimates) == 1:
            est = self.estimates[0]
            painter.setPen(text_color)
            painter.drawText(4, 4, self.width() - 8, 24,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             f"{est.backend.value}  ·  {est.realtime_factor:.0f}x 实时"
                             f"（{format_duration(self.audio_duration_s)} 音频约需 "
                             f"{format_duration(est.estimated_seconds)}）")
            painter.setPen(subtle)
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            hint = ("只有 CPU 可用：安装加速版 PyTorch 后通常可提升数十倍"
                    if est.backend == Backend.CPU
                    else f"按 {format_duration(self.audio_duration_s)} 音频估算，仅供参考")
            painter.drawText(4, 32, self.width() - 8, 34,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                             hint)
            painter.end()
            return

        w = self.width()
        bar_h = 26
        gap = 8
        label_w = 92
        bar_max_w = max(80, w - label_w - 120)
        max_rtf = max((e.realtime_factor for e in self.estimates), default=1) or 1

        y = 6
        for est in self.estimates:
            color = BACKEND_COLORS.get(est.backend, "#888")

            painter.setPen(text_color)
            painter.drawText(4, y, label_w - 8, bar_h,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             est.backend.value)

            track = QColor(text_color)
            track.setAlpha(28)
            painter.fillRect(label_w, y, bar_max_w, bar_h, track)

            bar_w = max(int(bar_max_w * (est.realtime_factor / max_rtf)), 4)
            painter.setBrush(QColor(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(label_w, y, bar_w, bar_h, 4, 4)

            text = (f"{est.realtime_factor:.0f}x 实时"
                    f"（约 {format_duration(est.estimated_seconds)}）")
            painter.setPen(text_color)
            painter.drawText(label_w + bar_w + 10, y, w - label_w - bar_w - 16, bar_h,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             text)
            y += bar_h + gap

        painter.setPen(subtle)
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        note = f"按 {format_duration(self.audio_duration_s)} 音频估算，仅供参考"
        painter.drawText(4, y + 2, w - 8, 16,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, note)
        painter.end()


# ─────────────────────────── 通用卡片 ───────────────────────────


def _section(title: str) -> tuple[CardWidget, QVBoxLayout]:
    card = CardWidget()
    layout = QVBoxLayout(card)
    layout.setContentsMargins(18, 14, 18, 14)
    layout.setSpacing(10)
    layout.addWidget(StrongBodyLabel(title))
    return card, layout


def _divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet("background: rgba(128,128,128,0.25); border: none;")
    return line


# ─────────────────────────── 设备行 ───────────────────────────


class DeviceCard(CardWidget):
    def __init__(self, info: DeviceInfo, parent=None):
        super().__init__(parent)
        self.info = info
        self.setStyleSheet(
            "CardWidget{background: rgba(128,128,128,0.06); border-radius: 6px;}")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)

        color = _STATE_COLORS.get(info.state, "#b0b0b0")
        dot = QLabel()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(f"background-color: {color}; border-radius: 5px;")
        layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        title = f"{info.backend.value}　·　{info.name}" if info.name else info.backend.value
        text_col.addWidget(StrongBodyLabel(title))

        if info.description:
            label = CaptionLabel(info.description)
            label.setStyleSheet("color: #8a8a8a;")
            text_col.addWidget(label)

        if info.state == DeviceState.HARDWARE_ONLY and info.hint:
            hint = CaptionLabel(info.hint)
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #d68a00;")
            text_col.addWidget(hint)

        layout.addLayout(text_col, 1)

        badge_text = {
            DeviceState.READY: "可用",
            DeviceState.HARDWARE_ONLY: "缺加速运行时",
            DeviceState.MISSING: "未检测到",
        }.get(info.state, "")
        badge = CaptionLabel(badge_text)
        badge.setStyleSheet(f"color: {color};")
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)


# ─────────────────────────── PyTorch 运行时卡片 ───────────────────────────


class RuntimeCard(CardWidget):
    """展示当前 PyTorch 构建，并在需要时提供升级入口。"""

    install_requested = pyqtSignal(object)   # InstallPlan

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan: InstallPlan | None = None
        self._installing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(StrongBodyLabel("推理运行时（PyTorch）"))
        self.build_badge = CaptionLabel("")
        head.addWidget(self.build_badge)
        head.addStretch()
        self.copy_btn = PushButton("复制安装命令")
        self.copy_btn.setVisible(False)
        self.copy_btn.clicked.connect(self._on_copy)
        head.addWidget(self.copy_btn)
        self.install_btn = PrimaryPushButton("安装加速版")
        self.install_btn.setVisible(False)
        self.install_btn.clicked.connect(self._on_install)
        head.addWidget(self.install_btn)
        root.addLayout(head)

        self.summary_label = BodyLabel("")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        self.advice_label = CaptionLabel("")
        self.advice_label.setWordWrap(True)
        self.advice_label.setVisible(False)
        root.addWidget(self.advice_label)

        self.command_edit = LineEdit()
        self.command_edit.setReadOnly(True)
        self.command_edit.setVisible(False)
        root.addWidget(self.command_edit)

        self.log_box = TextBrowser()
        self.log_box.setVisible(False)
        self.log_box.setFixedHeight(180)
        root.addWidget(self.log_box)

    def update_env(self, env: TorchEnv, devices: list[DeviceInfo]):
        self.build_badge.setText(env.build_label)
        if not env.installed:
            color = "#c42b1c"
        elif env.build in ("cuda", "rocm", "xpu", "mps"):
            color = "#2e9b4f"
        else:
            color = "#d68a00"
        self.build_badge.setStyleSheet(f"color: {color};")
        self.summary_label.setText("　·　".join(summarize(env)))

        gpu_vendors = []
        for d in devices:
            if d.state == DeviceState.HARDWARE_ONLY and d.vendor not in gpu_vendors:
                gpu_vendors.append(d.vendor)

        plan = suggest_install_plan(gpu_vendors[0], env) if gpu_vendors else None
        self._plan = plan

        if plan is None:
            self.copy_btn.setVisible(False)
            self.install_btn.setVisible(False)
            self.command_edit.setVisible(False)
            if env.build in ("cuda", "rocm", "xpu", "mps"):
                self.advice_label.setText("已安装带加速支持的 PyTorch，可直接使用 GPU 推理。")
                self.advice_label.setStyleSheet("color: #2e9b4f;")
            else:
                self.advice_label.setText(
                    "当前只能使用 CPU 推理。若机器上有独立显卡，请安装对应厂商的 PyTorch 版本。")
                self.advice_label.setStyleSheet("color: #8a8a8a;")
            self.advice_label.setVisible(True)
            return

        advice = plan.reason
        if plan.note:
            advice += f"\n{plan.note}"
        if not plan.supported:
            advice += "\n当前平台不支持一键安装，请按上面的方式处理。"
        self.advice_label.setText(advice)
        self.advice_label.setStyleSheet(
            "color: #d68a00;" if plan.supported else "color: #c42b1c;")
        self.advice_label.setVisible(True)

        self.command_edit.setText(plan.manual_command())
        self.command_edit.setVisible(True)
        self.copy_btn.setVisible(True)
        # 打包版 sys.executable 不是 python，无法直接调用 pip
        self.install_btn.setVisible(plan.supported and not getattr(sys, "frozen", False))

    def set_installing(self, installing: bool):
        self._installing = installing
        self.install_btn.setEnabled(not installing)
        self.install_btn.setText("安装中…" if installing else "安装加速版")
        if installing:
            self.log_box.setVisible(True)

    def append_log(self, text: str):
        self.log_box.setVisible(True)
        self.log_box.append(text)

    def _on_copy(self):
        if not self._plan:
            return
        parent = self.window()
        if _copy_to_clipboard(self._plan.manual_command()):
            InfoBar.success("已复制", "命令已复制到剪贴板，可在终端中执行",
                            parent=parent, duration=2500,
                            position=InfoBarPosition.TOP)
        else:
            InfoBar.warning("复制失败", "请手动选择命令文本复制",
                            parent=parent, duration=3000,
                            position=InfoBarPosition.TOP)

    def _on_install(self):
        if not self._plan or self._installing:
            return
        self.log_box.clear()
        self.set_installing(True)
        self.install_requested.emit(self._plan)


# ─────────────────────────── 主页面 ───────────────────────────


class HardwareTab(QWidget):
    detection_finished = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.devices: list[DeviceInfo] = []
        self.estimates: list[SpeedEstimate] = []
        self._detect_worker: DetectWorker | None = None
        self._install_worker: InstallWorker | None = None
        self._device_option: dict[int, str] = {}
        self._device_by_string: dict[str, DeviceInfo] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = SingleDirectionScrollArea(self, orient=Qt.Orientation.Vertical)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{background: transparent; border: none;}")
        outer.addWidget(scroll)

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        scroll.setWidget(container)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        # ── 顶部 ──
        top_row = QHBoxLayout()
        top_row.addWidget(StrongBodyLabel("硬件检测与推理方案"))
        top_row.addStretch()
        top_row.addWidget(QLabel("音频时长："))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 600)
        self.duration_spin.setValue(30)
        self.duration_spin.setSuffix(" 分钟")
        self.duration_spin.valueChanged.connect(self._update_estimates)
        top_row.addWidget(self.duration_spin)
        self.detect_btn = PrimaryPushButton("重新检测")
        self.detect_btn.clicked.connect(self.run_detection)
        top_row.addWidget(self.detect_btn)
        layout.addLayout(top_row)

        self.status_label = CaptionLabel("正在检测硬件…")
        self.status_label.setStyleSheet("color: #8a8a8a;")
        layout.addWidget(self.status_label)

        # ── 设备列表 ──
        self.device_card, self.device_layout = _section("检测到的设备")
        layout.addWidget(self.device_card)

        # ── PyTorch 运行时 ──
        self.runtime_card = RuntimeCard()
        self.runtime_card.install_requested.connect(self._start_install)
        layout.addWidget(self.runtime_card)

        # ── 速度对比 ──
        speed_card, speed_layout = _section("转写速度估算")
        self.speed_chart = SpeedBarWidget([], 1800)
        speed_layout.addWidget(self.speed_chart)
        layout.addWidget(speed_card)

        # ── 解码方案 ──
        scheme_card, scheme_layout = _section("默认解码方案")
        self.scheme_layout = QVBoxLayout()
        self.scheme_layout.setSpacing(4)
        scheme_layout.addLayout(self.scheme_layout)
        self.scheme_group = QButtonGroup(self)
        self.scheme_group.buttonToggled.connect(
            lambda *_: self._update_scheme_result(self.duration_spin.value() * 60))
        self.scheme_result = CaptionLabel("")
        self.scheme_result.setStyleSheet("color: #8a8a8a;")
        scheme_layout.addWidget(self.scheme_result)
        layout.addWidget(scheme_card)

        layout.addStretch()

        QTimer.singleShot(120, self.run_detection)

    # ─────────── 检测 ───────────

    def run_detection(self):
        if self._detect_worker is not None and self._detect_worker.isRunning():
            return
        self.detect_btn.setEnabled(False)
        self.status_label.setText("正在检测硬件与 PyTorch 构建…")
        self.status_label.setStyleSheet("color: #8a8a8a;")
        self._detect_worker = DetectWorker(refresh=True, parent=self)
        self._detect_worker.done.connect(self._on_detected)
        self._detect_worker.finished.connect(self._on_detect_finished)
        self._detect_worker.start()

    def _on_detect_finished(self):
        self.detect_btn.setEnabled(True)

    def _on_detected(self, devices: list[DeviceInfo]):
        self.devices = devices
        self._rebuild_device_rows()
        self._rebuild_scheme_options()
        self.runtime_card.update_env(get_torch_env(refresh=True), devices)
        self._update_estimates()

        ready = [d for d in devices if d.available and d.backend != Backend.CPU]
        hw_only = [d for d in devices if d.state == DeviceState.HARDWARE_ONLY]
        if ready:
            self.status_label.setText("可用于加速：" + "、".join(d.name for d in ready))
            self.status_label.setStyleSheet("color: #2e9b4f;")
        elif hw_only:
            self.status_label.setText(
                "检测到显卡，但当前 PyTorch 构建无法使用（处理方式见下方“推理运行时”）")
            self.status_label.setStyleSheet("color: #d68a00;")
        else:
            self.status_label.setText("未检测到可加速的显卡，将使用 CPU 推理")
            self.status_label.setStyleSheet("color: #8a8a8a;")

        self.detection_finished.emit(devices)

    def _rebuild_device_rows(self):
        while self.device_layout.count():
            item = self.device_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for d in self.devices:
            self.device_layout.addWidget(DeviceCard(d))

    def _rebuild_scheme_options(self):
        for btn in list(self.scheme_group.buttons()):
            self.scheme_group.removeButton(btn)
        while self.scheme_layout.count():
            item = self.scheme_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        self._device_option = {}
        self._device_by_string = {}
        options: list[tuple[str, str]] = [
            ("auto", "自动（推荐）：按 CUDA / ROCm / XPU / CPU 优先级选择")]
        for d in self.devices:
            if d.available and d.backend != Backend.CPU:
                options.append((d.device_string, f"{d.backend.value}　{d.name}"))
                self._device_by_string[d.device_string] = d
        options.append(("cpu", "CPU（软件解码，最慢但兼容性最好）"))

        for idx, (value, label) in enumerate(options):
            rb = QRadioButton(label)
            self.scheme_group.addButton(rb, idx)
            self._device_option[idx] = value
            self.scheme_layout.addWidget(rb)
            if idx == 0:
                rb.setChecked(True)

    # ─────────── 估算 ───────────

    def _update_estimates(self):
        duration_s = self.duration_spin.value() * 60
        if not self.devices:
            return
        self.estimates = estimate_speeds(self.devices, duration_s)
        self.speed_chart.estimates = [e for e in self.estimates if e.available]
        self.speed_chart.audio_duration_s = duration_s
        self.speed_chart.setFixedHeight(self.speed_chart.preferred_height())
        self.speed_chart.update()
        self._update_scheme_result(duration_s)

    def _selected_device_string(self) -> str:
        btn = self.scheme_group.checkedButton()
        if btn is None:
            return "auto"
        return self._device_option.get(self.scheme_group.id(btn), "auto")

    def _update_scheme_result(self, duration_s: float = 1800):
        current = self._selected_device_string()
        if current == "auto":
            best = pick_best_device(self.devices)
            if best is None:
                self.scheme_result.setText("没有可用设备")
                return
            resolved = best.device_string
            target_backend = best.backend
        else:
            resolved = current
            info = self._device_by_string.get(current)
            target_backend = info.backend if info else (
                Backend.CPU if current.startswith("cpu") else None)

        est = None
        for e in self.estimates:
            if e.available and e.backend == target_backend:
                est = e
                break
        time_str = format_duration(est.estimated_seconds) if est else "?"
        self.scheme_result.setText(
            f"实际使用：{resolved}　预估耗时 {time_str}"
            f"（{format_duration(duration_s)} 音频）")

    # ─────────── 安装加速版 ───────────

    def _start_install(self, plan: InstallPlan):
        if self._install_worker is not None and self._install_worker.isRunning():
            return
        self.runtime_card.append_log("开始安装，请保持网络畅通…")
        self._install_worker = InstallWorker(plan, parent=self)
        self._install_worker.line.connect(self.runtime_card.append_log)
        self._install_worker.done.connect(self._on_install_done)
        self._install_worker.start()

    def _on_install_done(self, ok: bool, error: str):
        self.runtime_card.set_installing(False)
        parent = self.window()
        if ok:
            self.runtime_card.append_log("安装完成，重启程序后生效。")
            InfoBar.success("安装完成", "请重启本程序以启用 GPU 加速",
                            parent=parent, duration=5000,
                            position=InfoBarPosition.TOP)
            self.run_detection()
        else:
            self.runtime_card.append_log(f"安装失败：{error}")
            InfoBar.error("安装失败", error, parent=parent, duration=6000,
                          position=InfoBarPosition.TOP)

    # ─────────── 对外 ───────────

    def get_selected_device(self) -> str:
        """返回 FunASR 可接受的 device 字符串；auto 会解析成具体设备。"""
        value = self._selected_device_string()
        if value == "auto":
            return resolve_device("auto", self.devices) if self.devices else "auto"
        return value

    def shutdown(self):
        """关闭窗口时：短暂等待，仍未结束的线程交给 keep_alive 兜底。"""
        for worker in (self._detect_worker, self._install_worker):
            if worker is not None and worker.isRunning():
                if not worker.wait(800):
                    keep_alive(worker)
