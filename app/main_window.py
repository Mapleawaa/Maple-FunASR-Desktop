from __future__ import annotations


from qfluentwidgets import (
    FluentWindow,
    NavigationItemPosition,
    FluentIcon,
    setTheme,
    Theme,
    InfoBar,
    InfoBarPosition,
)

from app.download_manager import DownloadManager
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
        self.download_manager = DownloadManager(self)

        self.setWindowTitle("Maple-FunASR-Desktop")
        self.resize(1200, 800)

        self._init_tabs()
        self._init_nav()
        self._init_menu()
        self._apply_theme()
        self._init_bindings()

    def _init_tabs(self):
        self.transcribe_tab = TranscribeTab(self)
        self.transcribe_tab._log_forwarder = self.append_log
        self.hardware_tab = HardwareTab(self)
        self.models_tab = ModelsTab(self, download_manager=self.download_manager)
        self.models_tab.log_forwarder = self.append_log
        self.logs_tab = LogsTab(self)

        # qfluentwidgets 要求每个子界面有唯一 objectName
        self.transcribe_tab.setObjectName("transcribeInterface")
        self.hardware_tab.setObjectName("hardwareInterface")
        self.models_tab.setObjectName("modelsInterface")
        self.logs_tab.setObjectName("logsInterface")

    def _init_nav(self):
        self.addSubInterface(self.transcribe_tab, FluentIcon.MICROPHONE, "转写")
        self.addSubInterface(self.hardware_tab, FluentIcon.SPEED_HIGH, "硬件")
        self.addSubInterface(self.models_tab, FluentIcon.DOWNLOAD, "模型")
        self.addSubInterface(self.logs_tab, FluentIcon.DOCUMENT, "日志",
                             position=NavigationItemPosition.BOTTOM)

    def _init_menu(self):
        self.navigationInterface.addItem(
            routeKey="settings_item",
            icon=FluentIcon.SETTING,
            text="设置",
            onClick=self._show_settings,
            position=NavigationItemPosition.BOTTOM,
            selectable=False,
        )
        self.navigationInterface.addItem(
            routeKey="about_item",
            icon=FluentIcon.INFO,
            text="关于",
            onClick=self._show_about,
            position=NavigationItemPosition.BOTTOM,
            selectable=False,
        )

    def _init_bindings(self):
        # 模型没下载时，从转写页直接跳到模型页
        self.transcribe_tab.model_missing.connect(self._open_models)
        # 下载完成后刷新转写页的设备/模型状态
        self.download_manager.state_changed.connect(self._on_download_state)
        # 硬件页检测完成后，若存在"有卡但缺运行库"的情况给一次提示
        self.hardware_tab.detection_finished.connect(self._on_detection_finished)

    def _open_models(self, bundle_key: str = ""):
        self.switchTo(self.models_tab)
        self.show_info("请先下载模型", "在模型页下载对应模型包后再开始转写")

    def _on_download_state(self, key: str, state: str, message: str, hint: str):
        if state in ("done", "failed", "cancelled"):
            self.transcribe_tab.refresh_devices()

    def _on_detection_finished(self, devices: list):
        from core.hardware_checker import DeviceState

        hw_only = [d for d in devices if d.state == DeviceState.HARDWARE_ONLY]
        if not hw_only:
            return
        self.show_info(
            "GPU 暂不可用",
            f"{hw_only[0].name} 未被当前 PyTorch 用于加速，可到“硬件”页查看处理方法")

    def _apply_theme(self):
        t = self.settings.theme
        if t == "dark":
            setTheme(Theme.DARK)
        elif t == "light":
            setTheme(Theme.LIGHT)
        else:
            setTheme(Theme.AUTO)

    def _show_about(self):
        AboutDialog(self).exec()

    def _show_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self._apply_theme()

    # ─────────── 对外 ───────────

    def append_log(self, text: str):
        self.logs_tab.append_log(text)

    def show_info(self, title: str, content: str = ""):
        InfoBar.info(title, content, parent=self, duration=4000,
                     position=InfoBarPosition.TOP)

    def show_error(self, title: str, content: str = ""):
        InfoBar.error(title, content, parent=self, duration=6000,
                      position=InfoBarPosition.TOP)

    def closeEvent(self, event):
        try:
            self.download_manager.shutdown()
            self.transcribe_tab.shutdown()
            self.hardware_tab.shutdown()
        except Exception:
            pass
        super().closeEvent(event)
