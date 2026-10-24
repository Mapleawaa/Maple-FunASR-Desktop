import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _run_download_worker() -> int:
    """下载子进程入口（打包后由同一个 exe 以参数方式启动）。"""
    from core.download_worker import main as worker_main
    return worker_main(sys.argv)


def main():
    # 必须在创建 QApplication 之前拦截，避免子进程再去初始化 GUI
    if "--download-worker" in sys.argv:
        sys.exit(_run_download_worker())

    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication
    from app.main_window import MainWindow

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setOrganizationName("Maple-FunASR-Desktop")
    app.setApplicationName("Maple-FunASR-Desktop")

    font = QFont("Microsoft YaHei UI")
    font.setPointSize(9)
    app.setFont(font)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
