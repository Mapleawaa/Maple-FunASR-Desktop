from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel
from qfluentwidgets import (
    FluentWindow,
    NavigationItemPosition,
    FluentIcon,
    setTheme,
    Theme,
    InfoBar,
    InfoBarPosition,
)

from app.settings import Settings
from ui.tabs.transcribe_tab import TranscribeTab
from ui.tabs.hardware_tab import HardwareTab
from ui.tabs.models_tab import ModelsTab
from ui.tabs.logs_tab import LogsTab
from ui.dialogs.about_dialog import AboutDialog
from ui.dialogs.settings_dialog import SettingsDialog


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings()

        self.setWindowTitle("Maple-FunASR-Desktop")
        self.resize(1200, 800)

        self._init_tabs()
        self._init_nav()
        self._init_menu()
        self._init_status_bar()
        self._apply_theme()

    def _init_tabs(self):
        self.transcribe_tab = TranscribeTab(self)
        self.transcribe_tab._log_forwarder = self.append_log
        self.hardware_tab = HardwareTab(self)
        self.models_tab = ModelsTab(self)
        self.logs_tab = LogsTab(self)

    def _init_nav(self):
        self.addSubInterface(
            self.transcribe_tab, FluentIcon.MICROPHONE, "Transcribe"
        )
        self.addSubInterface(
            self.hardware_tab, FluentIcon.DEVICE_BUSY, "Hardware"
        )
        self.addSubInterface(
            self.models_tab, FluentIcon.DOWNLOAD, "Models"
        )
        self.addSubInterface(
            self.logs_tab, FluentIcon.DOCUMENT, "Logs",
            position=NavigationItemPosition.BOTTOM,
        )

    def _init_menu(self):
        self.navigationInterface.addItem(
            routeKey="settings_item",
            icon=FluentIcon.SETTING,
            text="Settings",
            onClick=self._show_settings,
            position=NavigationItemPosition.BOTTOM,
            selectable=False,
        )
        self.navigationInterface.addItem(
            routeKey="about_item",
            icon=FluentIcon.INFO,
            text="About",
            onClick=self._show_about,
            position=NavigationItemPosition.BOTTOM,
            selectable=False,
        )

    def _init_status_bar(self):
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #888; padding: 0 12px;")
        self.statusBar().addWidget(self.status_label)
        self.statusBar().setStyleSheet("background: transparent;")

    def _apply_theme(self):
        t = self.settings.theme
        if t == "dark":
            setTheme(Theme.DARK)
        elif t == "light":
            setTheme(Theme.LIGHT)
        else:
            setTheme(Theme.AUTO)

    def _show_about(self):
        dlg = AboutDialog(self)
        dlg.exec()

    def _show_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self._apply_theme()

    def append_log(self, text: str):
        self.logs_tab.append_log(text)

    def show_info(self, title: str, content: str = ""):
        InfoBar.info(title, content, self, 3000, InfoBarPosition.TOP)

    def show_error(self, title: str, content: str = ""):
        InfoBar.error(title, content, self, 5000, InfoBarPosition.TOP)
