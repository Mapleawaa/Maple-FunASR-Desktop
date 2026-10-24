"""Maple-FunASR-Desktop 入口。

启动顺序
--------
1. ``--download-worker``：模型下载子进程，最先拦截（不需要 GUI）；
2. ``--web``：强制启动 Web 控制台；
3. 其余情况走 GUI；在 WSL / Linux 上先探测 WSLg 与 Qt 平台插件，
   不可用（或 GUI 启动异常）时自动降级到 Web 控制台。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8420


def _run_download_worker() -> int:
    """下载子进程入口（打包后由同一个 exe 以参数方式启动）。"""
    from core.download_worker import main as worker_main
    return worker_main(sys.argv)


def _parse_args(argv: list[str]) -> dict:
    opts = {
        "web": False,
        "gui": False,
        "no_fallback": False,
        "host": DEFAULT_WEB_HOST,
        "port": DEFAULT_WEB_PORT,
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--web":
            opts["web"] = True
        elif arg == "--gui":
            opts["gui"] = True
        elif arg in ("--no-fallback", "--no-web-fallback"):
            opts["no_fallback"] = True
        elif arg == "--host" and i + 1 < len(argv):
            opts["host"] = argv[i + 1]
            i += 1
        elif arg == "--port" and i + 1 < len(argv):
            try:
                opts["port"] = int(argv[i + 1])
            except ValueError:
                pass
            i += 1
        elif arg.startswith("--port="):
            try:
                opts["port"] = int(arg.split("=", 1)[1])
            except ValueError:
                pass
        elif arg.startswith("--host="):
            opts["host"] = arg.split("=", 1)[1]
        i += 1
    return opts


def _run_web(opts: dict, note: str = "", open_browser: bool = False) -> int:
    from app.web_console.server import run_web_console
    return run_web_console(host=opts["host"], port=opts["port"],
                           note=note, open_browser=open_browser)


def _run_gui() -> int:
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
    return app.exec()


def main():
    argv = sys.argv[1:]

    # 必须在创建 QApplication 之前拦截，避免子进程再去初始化 GUI
    if "--download-worker" in argv:
        sys.exit(_run_download_worker())

    opts = _parse_args(argv)

    if opts["web"]:
        sys.exit(_run_web(opts, note="手动启动 Web 控制台", open_browser=True))

    from app.runtime_env import choose_gui_platform, detect_runtime

    runtime = detect_runtime()
    preflight_needed = runtime.is_wsl or (runtime.is_linux and not runtime.is_windows)

    if preflight_needed:
        platform, reason = choose_gui_platform(runtime, verbose=True)
        if platform is None:
            if opts["no_fallback"] or opts["gui"]:
                print(f"[gui] 图形界面不可用：{reason}", file=sys.stderr, flush=True)
                sys.exit(2)
            print(f"[gui] 图形界面不可用：{reason}", flush=True)
            print("[gui] 已切换到 Web 控制台", flush=True)
            sys.exit(_run_web(opts, note=f"WSLg 不可用：{reason}"))
        if platform:
            os.environ["QT_QPA_PLATFORM"] = platform
            print(f"[gui] 使用 Qt 平台插件：{platform}", flush=True)

    try:
        sys.exit(_run_gui())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        if opts["no_fallback"] or opts["gui"] or runtime.is_windows or runtime.is_macos:
            raise
        print(f"[gui] 启动失败：{exc}", flush=True)
        sys.exit(_run_web(opts, note=f"GUI 启动失败：{exc}"))


if __name__ == "__main__":
    main()
