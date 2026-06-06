from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
)
from qfluentwidgets import (
    CardWidget, StrongBodyLabel, BodyLabel,
    ComboBox, PushButton, ProgressBar,
    InfoBar, InfoBarPosition,
)

from core.downloader import (
    MODELS_TO_DOWNLOAD, MIRROR_SOURCES,
    download_model, check_model_installed,
)


class DownloadWorker(QThread):
    progress_signal = pyqtSignal(str, int)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, model_id: str, mirror: str):
        super().__init__()
        self.model_id = model_id
        self.mirror = mirror

    def run(self):
        try:
            download_model(
                self.model_id, self.mirror,
                progress_callback=self.progress_signal.emit,
            )
            self.finished_signal.emit(True, self.model_id)
        except Exception as e:
            self.finished_signal.emit(False, str(e))


class ModelCard(CardWidget):
    def __init__(self, model_info: dict, parent_tab=None, parent=None):
        super().__init__(parent)
        self.model_info = model_info
        self.model_id = model_info["model_id"]
        self.parent_tab = parent_tab

        layout = QVBoxLayout(self)

        title_row = QHBoxLayout()
        title_row.addWidget(StrongBodyLabel(model_info["name"]))
        self.status_label = BodyLabel("")
        title_row.addWidget(self.status_label)
        title_row.addStretch()

        self.download_btn = PushButton("Download")
        self.download_btn.clicked.connect(self._start_download)
        title_row.addWidget(self.download_btn)

        self.progress_bar = ProgressBar()
        self.progress_bar.setVisible(False)
        title_row.addWidget(self.progress_bar)

        layout.addLayout(title_row)
        layout.addWidget(BodyLabel(f"Model ID: {model_info['model_id']}"))
        layout.addWidget(BodyLabel(
            f"Size: {model_info['size']}  |  {model_info['description']}"
        ))

        self._refresh_status()

    def _refresh_status(self):
        installed = check_model_installed(self.model_id)
        if installed:
            self.status_label.setText("Installed")
            self.status_label.setStyleSheet("color: #4CAF50;")
            self.download_btn.setText("Re-download")
        else:
            self.status_label.setText("Not installed")
            self.status_label.setStyleSheet("color: #888;")
            self.download_btn.setText("Download")
        self.download_btn.setEnabled(True)

    def _start_download(self):
        mirror = "modelscope (Recommended for China)"
        if self.parent_tab:
            mirror = self.parent_tab.mirror_combo.currentText()
        hub = MIRROR_SOURCES.get(mirror, "ms")

        self.download_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.status_label.setText("Downloading...")
        self.status_label.setStyleSheet("color: #FF9800;")

        self.worker = DownloadWorker(self.model_id, hub)
        self.worker.progress_signal.connect(self._on_progress)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.start()

    def _on_progress(self, msg: str, pct: int):
        if pct >= 0:
            self.progress_bar.setValue(pct)
        self.status_label.setText(msg)

    def _on_finished(self, success: bool, msg: str):
        self.progress_bar.setVisible(False)
        if success:
            self._refresh_status()
            InfoBar.success("Download complete", self.model_id,
                            self, 3000, InfoBarPosition.TOP)
        else:
            self.status_label.setText("Download failed")
            self.status_label.setStyleSheet("color: #f44336;")
            self.download_btn.setEnabled(True)
            InfoBar.error("Download failed", msg,
                          self, 5000, InfoBarPosition.TOP)


class ModelsTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(StrongBodyLabel("Model Management"))

        mirror_card = CardWidget()
        mirror_layout = QHBoxLayout(mirror_card)
        mirror_layout.addWidget(QLabel("Mirror source:"))
        self.mirror_combo = ComboBox()
        for name in MIRROR_SOURCES:
            self.mirror_combo.addItem(name)
        mirror_layout.addWidget(self.mirror_combo)
        mirror_layout.addStretch()
        layout.addWidget(mirror_card)

        self.model_cards = []
        for m in MODELS_TO_DOWNLOAD:
            card = ModelCard(m, parent_tab=self, parent=self)
            self.model_cards.append(card)
            layout.addWidget(card)

        refresh_btn = PushButton("Refresh status")
        refresh_btn.clicked.connect(self._refresh_all)
        layout.addWidget(refresh_btn)

        layout.addStretch()

    def _refresh_all(self):
        for card in self.model_cards:
            card._refresh_status()
