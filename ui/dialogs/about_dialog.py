from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Maple-FunASR-Desktop")
        self.setFixedSize(480, 400)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(6)

        icon_label = QLabel()
        pixmap = QPixmap("ui/resources/project-icon.jpg")
        if not pixmap.isNull():
            icon_label.setPixmap(
                pixmap.scaled(72, 72, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
            )
        else:
            icon_label.setText("")
            icon_label.setFixedSize(72, 72)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_label)

        layout.addWidget(QLabel("<h2>Maple-FunASR-Desktop</h2>"))
        layout.addWidget(QLabel("Version 1.0.0"))
        layout.addWidget(QLabel(
            'Author: DeepSeek V4 & <a href="https://github.com/mapleawaa">Mapleawaa</a>'
        ))
        layout.addWidget(QLabel(
            "A FunASR desktop application based on QFluentWidgets"
        ))

        layout.addSpacing(12)

        layout.addWidget(QLabel("<b>Acknowledgements</b>"))
        for name, url in [
            ("FFmpeg", "https://ffmpeg.org"),
            ("FunASR", "https://github.com/modelscope/FunASR"),
            ("ModelScope", "https://modelscope.cn"),
            ("Qt / PyQt6", "https://www.qt.io"),
            ("QFluentWidgets", "https://github.com/zhiyiYo/QFluentWidgets"),
            ("TUNA (Mirrors)", "https://mirrors.tuna.tsinghua.edu.cn"),
        ]:
            link = QLabel(f'<a href="{url}">{name}</a>')
            link.setOpenExternalLinks(True)
            link.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(link)

        layout.addSpacing(8)

        layout.addWidget(QLabel(
            "Copyright (C) 2026 Maple-FunASR-Desktop<br>"
            "Licensed under GNU GPL v3"
        ))
