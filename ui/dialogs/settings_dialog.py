from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton
from PyQt6.QtCore import Qt


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        self.resize(400, 200)

        layout = QVBoxLayout(self)

        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Theme:"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["System (auto)", "Light", "Dark"])
        current = self.settings.theme
        idx = {"auto": 0, "light": 1, "dark": 2}.get(current, 0)
        self.theme_combo.setCurrentIndex(idx)
        theme_row.addWidget(self.theme_combo)
        layout.addLayout(theme_row)

        layout.addStretch()

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _save(self):
        mapping = {0: "auto", 1: "light", 2: "dark"}
        self.settings.theme = mapping[self.theme_combo.currentIndex()]
        self.accept()
