"""模型管理页：按模型包下载，带真实进度、队列、取消与失败提示。"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy,
)
from qfluentwidgets import (
    CardWidget, StrongBodyLabel, BodyLabel, CaptionLabel,
    ComboBox, PushButton, PrimaryPushButton, ProgressBar,
    InfoBar, InfoBarPosition, IndeterminateProgressBar,
)

from app.download_manager import (
    DownloadManager, STATE_IDLE, STATE_QUEUED, STATE_RUNNING,
    STATE_DONE, STATE_FAILED, STATE_CANCELLED, mirror_hub,
)
from core.downloader import (
    DEFAULT_MIRROR_LABEL, MIRROR_SOURCES, MODEL_BUNDLES, ModelBundle,
    bundle_remote_size, bundle_status, human_size, repo_friendly_name,
)


_STATE_COLORS = {
    STATE_IDLE: "#8a8a8a",
    STATE_QUEUED: "#d68a00",
    STATE_RUNNING: "#0f6cbd",
    STATE_DONE: "#2e9b4f",
    STATE_FAILED: "#c42b1c",
    STATE_CANCELLED: "#8a8a8a",
}


_SIZE_CACHE: dict[tuple[str, str], int] = {}


class _SizeWorker(QThread):
    """后台查询远端仓库体积：网络请求绝不能跑在 UI 线程。"""

    one = pyqtSignal(str, int)      # bundle_key, bytes
    done = pyqtSignal()

    def __init__(self, hub: str, bundles, parent=None):
        super().__init__(parent)
        self._hub = hub
        self._bundles = list(bundles)

    def run(self):
        for bundle in self._bundles:
            cache_key = (bundle.key, self._hub)
            size = _SIZE_CACHE.get(cache_key, 0)
            if not size:
                try:
                    size = bundle_remote_size(bundle, self._hub)
                except Exception:
                    size = 0
                if size:
                    _SIZE_CACHE[cache_key] = size
            if size:
                self.one.emit(bundle.key, size)
        self.done.emit()


def _fmt_eta(seconds: float) -> str:
    if not seconds or seconds <= 0 or seconds > 86400:
        return "--"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class BundleCard(CardWidget):
    """单个模型包的卡片。"""

    def __init__(self, bundle: ModelBundle, manager: DownloadManager,
                 parent_tab=None, parent=None):
        super().__init__(parent)
        self.bundle = bundle
        self.manager = manager
        self.parent_tab = parent_tab
        self._remote_size: int = 0
        self._force_redownload = False

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(8)

        # ── 标题行 ──
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.title_label = StrongBodyLabel(bundle.name)
        title_row.addWidget(self.title_label)

        self.badge = CaptionLabel("未安装")
        self.badge.setStyleSheet("color: #8a8a8a;")
        title_row.addWidget(self.badge)
        title_row.addStretch()

        self.cancel_btn = PushButton("取消")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        title_row.addWidget(self.cancel_btn)

        self.action_btn = PrimaryPushButton("下载")
        self.action_btn.clicked.connect(self._on_action)
        title_row.addWidget(self.action_btn)
        root.addLayout(title_row)

        # ── 描述 ──
        self.desc_label = BodyLabel(bundle.description)
        self.desc_label.setWordWrap(True)
        root.addWidget(self.desc_label)

        self.meta_label = CaptionLabel("")
        self.meta_label.setStyleSheet("color: #8a8a8a;")
        self.meta_label.setWordWrap(True)
        root.addWidget(self.meta_label)

        # ── 进度条（独占一行，保证有足够宽度） ──
        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.progress_bar.setMinimumHeight(6)
        self.progress_bar.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Fixed)
        root.addWidget(self.progress_bar)

        self.indeterminate = IndeterminateProgressBar(self, start=False)
        self.indeterminate.setVisible(False)
        self.indeterminate.setMinimumHeight(6)
        root.addWidget(self.indeterminate)

        # ── 状态行 ──
        self.status_label = CaptionLabel("")
        self.status_label.setStyleSheet("color: #8a8a8a;")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self._connect_manager()
        self.refresh()

    # ─────────── 外部接口 ───────────

    def refresh(self):
        hub = self._hub()
        status = bundle_status(self.bundle, hub)
        self._update_meta(status)
        if self.manager.is_busy(self.bundle.key):
            self._apply_state(self.manager.state_of(self.bundle.key), "")
            return
        if status.installed:
            self._set_badge("已安装", _STATE_COLORS[STATE_DONE])
            self.action_btn.setText("校验文件")
            self.status_label.setText(
                f"占用磁盘 {human_size(status.size_on_disk)} · 可直接用于转写")
            self.status_label.setStyleSheet("color: #2e9b4f;")
        elif status.partial:
            self._set_badge(f"部分安装 {status.repos_done}/{status.repos_total}",
                            _STATE_COLORS[STATE_QUEUED])
            self.action_btn.setText("补全下载")
            self.status_label.setText("缺失组件：" + "、".join(
                repo_friendly_name(r) for r in status.missing))
            self.status_label.setStyleSheet("color: #d68a00;")
        else:
            self._set_badge("未安装", _STATE_COLORS[STATE_IDLE])
            self.action_btn.setText("下载")
            self.status_label.setText("尚未下载，转写前需要先下载")
            self.status_label.setStyleSheet("color: #8a8a8a;")
        self.action_btn.setEnabled(True)

    def set_remote_size(self, size: int):
        if size and size != self._remote_size:
            self._remote_size = size
            self._update_meta(bundle_status(self.bundle, self._hub()))

    # ─────────── 内部 ───────────

    def _hub(self) -> str:
        if self.parent_tab is not None:
            return self.parent_tab.current_hub()
        return "ms"

    def _update_meta(self, status):
        size_text = human_size(self._remote_size) if self._remote_size else \
            "约 " + human_size(self.bundle.approx_mb * 1024 ** 2)
        components = " + ".join(
            repo_friendly_name(r) for r in self.bundle.repos(self._hub()))
        parts = [f"语言 {self.bundle.language}" if self.bundle.language else "",
                 f"下载大小 {size_text}",
                 components]
        self.meta_label.setText("  ·  ".join(p for p in parts if p))

    def _set_badge(self, text: str, color: str):
        self.badge.setText(text)
        self.badge.setStyleSheet(f"color: {color};")

    def _connect_manager(self):
        self.manager.progress_changed.connect(self._on_progress)
        self.manager.state_changed.connect(self._on_state)

    def _on_action(self):
        status = bundle_status(self.bundle, self._hub())
        # 已装好的包先做一次体积校验，避免用户以为点了"下载"其实什么都没发生
        if status.installed and not self._force_redownload:
            self._verify_installed(status)
            return

        hub = self._hub()
        result = self.manager.enqueue(self.bundle.key, hub)
        if result in ("started", "queued"):
            self._force_redownload = False
        if result == "duplicate":
            InfoBar.info("正在下载", f"{self.bundle.name} 已在下载队列中", parent=self, duration=2500, position=InfoBarPosition.TOP)
        elif result == "queued":
            InfoBar.info("已加入队列", f"{self.bundle.name} 将在当前任务完成后开始", parent=self, duration=2500, position=InfoBarPosition.TOP)
        elif result == "unknown":
            InfoBar.error("模型包不存在", self.bundle.key, parent=self, duration=4000, position=InfoBarPosition.TOP)

    def _verify_installed(self, status):
        expected = self._remote_size or (self.bundle.approx_mb * 1024 ** 2)
        actual = status.size_on_disk
        parent = self.window()
        if expected <= 0 or actual >= expected * 0.9:
            InfoBar.success(
                "文件完整",
                f"{self.bundle.name} 已就绪（占用 {human_size(actual)}）",
                parent=parent, duration=3000, position=InfoBarPosition.TOP)
            return
        self._force_redownload = True
        self.action_btn.setText("重新下载")
        self.status_label.setText(
            f"文件可能不完整（{human_size(actual)} / 约 {human_size(expected)}），"
            f"可点击“重新下载”补齐")
        self.status_label.setStyleSheet("color: #d68a00;")
        InfoBar.warning(
            "文件可能不完整",
            "体积明显小于预期，建议重新下载；已存在的文件会被跳过。",
            parent=parent, duration=5000, position=InfoBarPosition.TOP)

    def _on_cancel(self):
        self.manager.cancel(self.bundle.key)

    def _on_progress(self, key: str, prog):
        if key != self.bundle.key:
            return
        self.progress_bar.setVisible(True)
        self.indeterminate.setVisible(False)
        if prog.percent >= 0:
            self.progress_bar.setValue(prog.percent)
            head = f"{prog.percent}%　{human_size(prog.done)} / {human_size(prog.total)}"
        else:
            self.progress_bar.setValue(0)
            head = f"已下载 {human_size(prog.done)}（总量未知）"
        extra = []
        if prog.speed > 0:
            extra.append(f"{human_size(prog.speed)}/s")
        if prog.eta > 0:
            extra.append(f"剩余 {_fmt_eta(prog.eta)}")
        if prog.repo and prog.repo_count:
            extra.append(f"仓库 {prog.repo_index}/{prog.repo_count}")
        text = head + ("　·　" + "　·　".join(extra) if extra else "")
        if prog.file:
            text += f"　·　{prog.file}"
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: #0f6cbd;")
        self._set_badge("下载中", _STATE_COLORS[STATE_RUNNING])
        self.action_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)

    def _on_state(self, key: str, state: str, message: str, hint: str):
        if key != self.bundle.key:
            return
        self._apply_state(state, message, hint)

    def _apply_state(self, state: str, message: str, hint: str = ""):
        color = _STATE_COLORS.get(state, "#8a8a8a")

        if state == STATE_RUNNING:
            self.action_btn.setEnabled(False)
            self.action_btn.setText("下载中…")
            self.cancel_btn.setVisible(True)
            self.progress_bar.setVisible(True)
            if not message:
                return
            self._set_badge("下载中", color)
            self.status_label.setText(message)
            self.status_label.setStyleSheet(f"color: {color};")
            return

        if state == STATE_QUEUED:
            self.action_btn.setEnabled(False)
            self.action_btn.setText("排队中")
            self.cancel_btn.setVisible(True)
            self.progress_bar.setVisible(False)
            self.indeterminate.setVisible(False)
            self._set_badge("排队中", color)
            self.status_label.setText("等待前一个下载任务完成…")
            self.status_label.setStyleSheet(f"color: {color};")
            return

        # 终态
        self.cancel_btn.setVisible(False)
        self.progress_bar.setVisible(False)
        self.indeterminate.setVisible(False)
        self.action_btn.setEnabled(True)

        if state == STATE_DONE:
            self.progress_bar.setVisible(False)
            self.refresh()
            InfoBar.success("下载完成", self.bundle.name, parent=self, duration=3000, position=InfoBarPosition.TOP)
            if self.parent_tab is not None:
                self.parent_tab.notify_download_finished(self.bundle.key, True, "")
            return

        if state == STATE_CANCELLED:
            self._set_badge("已取消", color)
            self.action_btn.setText("继续下载")
            self.status_label.setText("已取消，已下载的数据会保留，可断点续传")
            self.status_label.setStyleSheet(f"color: {color};")
            if self.parent_tab is not None:
                self.parent_tab.notify_download_finished(self.bundle.key, False, "已取消")
            return

        if state == STATE_FAILED:
            self._set_badge("下载失败", color)
            self.action_btn.setText("重试")
            text = message or "下载失败"
            if hint:
                text += f"\n{hint}"
            self.status_label.setText(text)
            self.status_label.setStyleSheet(f"color: {color};")
            InfoBar.error("下载失败", message or self.bundle.name, parent=self, duration=6000, position=InfoBarPosition.TOP)
            if self.parent_tab is not None:
                self.parent_tab.notify_download_finished(self.bundle.key, False, message)



class ModelsTab(QWidget):
    def __init__(self, parent=None, download_manager: DownloadManager | None = None):
        super().__init__(parent)
        self.log_forwarder = None
        self._size_worker: _SizeWorker | None = None
        self.manager = download_manager or DownloadManager(self)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)

        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("模型管理"))
        header.addStretch()
        layout.addLayout(header)

        tip = BodyLabel(
            "每个模型包包含主模型 + VAD + 说话人分离等依赖，一次性下齐才不会在转写时卡住。")
        tip.setStyleSheet("color: #8a8a8a;")
        layout.addWidget(tip)

        # ── 镜像 + 批量操作 ──
        mirror_card = CardWidget()
        mirror_layout = QHBoxLayout(mirror_card)
        mirror_layout.setContentsMargins(18, 12, 18, 12)
        mirror_layout.addWidget(QLabel("下载源："))
        self.mirror_combo = ComboBox()
        for name in MIRROR_SOURCES:
            self.mirror_combo.addItem(name)
        self.mirror_combo.setCurrentText(DEFAULT_MIRROR_LABEL)
        self.mirror_combo.currentTextChanged.connect(self._on_mirror_changed)
        mirror_layout.addWidget(self.mirror_combo)
        mirror_layout.addSpacing(16)
        self.batch_btn = PushButton("下载全部未安装项")
        self.batch_btn.clicked.connect(self._download_all_missing)
        mirror_layout.addWidget(self.batch_btn)
        refresh_btn = PushButton("刷新状态")
        refresh_btn.clicked.connect(self._refresh_all)
        mirror_layout.addWidget(refresh_btn)
        mirror_layout.addStretch()
        layout.addWidget(mirror_card)

        self.summary_label = CaptionLabel("")
        self.summary_label.setStyleSheet("color: #8a8a8a;")
        layout.addWidget(self.summary_label)

        self.cards: list[BundleCard] = []
        for bundle in MODEL_BUNDLES.values():
            card = BundleCard(bundle, self.manager, parent_tab=self, parent=self)
            self.cards.append(card)
            layout.addWidget(card)

        layout.addStretch()

        self.manager.log_message.connect(self._forward_log)
        self.manager.queue_changed.connect(self._update_summary)
        self.manager.state_changed.connect(lambda *_: self._update_summary())

        self._refresh_all()
        # 远端大小查询放到界面显示之后，避免拖慢首屏
        QTimer.singleShot(300, self._query_sizes)

    # ─────────── 对外 ───────────

    def current_hub(self) -> str:
        return mirror_hub(self.mirror_combo.currentText())

    def notify_download_finished(self, key: str, ok: bool, message: str):
        self._update_summary()

    # ─────────── 内部 ───────────

    def _forward_log(self, key: str, text: str):
        if self.log_forwarder and text:
            name = MODEL_BUNDLES[key].name if key in MODEL_BUNDLES else key
            try:
                self.log_forwarder(f"[模型] {name}: {text}")
            except Exception:
                pass

    def _query_sizes(self):
        if self._size_worker is not None and self._size_worker.isRunning():
            return
        self._size_worker = _SizeWorker(self.current_hub(),
                                        [c.bundle for c in self.cards], self)
        self._size_worker.one.connect(self._on_size_one)
        self._size_worker.start()

    def _on_size_one(self, key: str, size: int):
        for card in self.cards:
            if card.bundle.key == key:
                card.set_remote_size(size)
                break

    def _refresh_all(self):
        for card in self.cards:
            card.refresh()
        self._update_summary()

    def _on_mirror_changed(self, _text: str):
        for card in self.cards:
            card.refresh()
        self._update_summary()
        QTimer.singleShot(100, self._query_sizes)

    def _download_all_missing(self):
        hub = self.current_hub()
        queued = 0
        for card in self.cards:
            if bundle_status(card.bundle, hub).installed:
                continue
            if self.manager.is_busy(card.bundle.key):
                continue
            if self.manager.enqueue(card.bundle.key, hub) in ("started", "queued"):
                queued += 1
        if queued:
            InfoBar.success("已开始", f"{queued} 个模型包进入下载队列", parent=self, duration=3000, position=InfoBarPosition.TOP)
        else:
            InfoBar.info("无需下载", "所有模型包都已安装", parent=self, duration=2500, position=InfoBarPosition.TOP)

    def _update_summary(self):
        hub = self.current_hub()
        total = len(self.cards)
        installed = sum(1 for c in self.cards
                        if bundle_status(c.bundle, hub).installed)
        size = sum(bundle_status(c.bundle, hub).size_on_disk for c in self.cards)
        pending = len(self.manager.pending_keys())
        text = f"已安装 {installed}/{total} · 本地占用 {human_size(size)}"
        if pending:
            text += f" · 队列中 {pending} 个"
        if self.manager.any_running():
            text += " · 正在下载"
        self.summary_label.setText(text)
        self.batch_btn.setEnabled(True)
