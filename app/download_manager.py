"""下载任务调度（Qt 层）。

职责
----
* 用 :class:`QProcess` 拉起 ``core.download_worker`` 子进程，逐行读取 JSON 进度；
* **全局串行队列**：同一时间只跑一个下载，避免多个大文件抢带宽；
* **去重**：同一个模型包重复点击不会起第二个进程，只会返回当前状态；
* **看门狗**：超过 ``STALL_TIMEOUT`` 没有任何字节进展就判定为网络阻塞并强制结束；
* **取消**：直接 terminate 子进程，能立即释放连接，不会污染主进程。
"""

from __future__ import annotations

import json
import os
import sys

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, pyqtSignal

from core.downloader import (
    MIRROR_SOURCES, MODEL_BUNDLES, DownloadProgress,
)

STALL_TIMEOUT_MS = 90_000      # 90s 没有新字节就认为网络卡死
KILL_GRACE_MS = 4_000          # terminate 后等待时间，超时则 kill

STATE_IDLE = "idle"
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"


def _project_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DownloadManager(QObject):
    """进程级单例式调度器（由主窗口持有一个实例）。"""

    progress_changed = pyqtSignal(str, object)          # bundle_key, DownloadProgress
    log_message = pyqtSignal(str, str)                  # bundle_key, text
    state_changed = pyqtSignal(str, str, str, str)      # key, state, message, hint
    queue_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._current_key: str = ""
        self._current_hub: str = "ms"
        self._current_result: dict = {}
        self._stdout_buf = ""
        self._pending: list[tuple[str, str]] = []
        self._states: dict[str, str] = {}
        self._last_payload: dict = {}
        self._terminating = ""   # "" / cancelled / stalled

        self._stall_timer = QTimer(self)
        self._stall_timer.setSingleShot(True)
        self._stall_timer.setInterval(STALL_TIMEOUT_MS)
        self._stall_timer.timeout.connect(self._on_stall)

        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.setInterval(KILL_GRACE_MS)
        self._kill_timer.timeout.connect(self._force_kill)

    # ─────────────── 对外查询 ───────────────

    def state_of(self, key: str) -> str:
        return self._states.get(key, STATE_IDLE)

    def is_busy(self, key: str) -> bool:
        return self.state_of(key) in (STATE_QUEUED, STATE_RUNNING)

    def any_running(self) -> bool:
        return self._proc is not None

    def active_key(self) -> str:
        return self._current_key if self._proc is not None else ""

    def pending_keys(self) -> list[str]:
        return [k for k, _hub in self._pending]

    # ─────────────── 入队 / 取消 ───────────────

    def enqueue(self, bundle_key: str, hub: str = "ms") -> str:
        """返回 started / queued / duplicate / unknown。"""
        if bundle_key not in MODEL_BUNDLES:
            return "unknown"
        if self.is_busy(bundle_key) or bundle_key in self.pending_keys():
            return "duplicate"

        if self._proc is None:
            self._start(bundle_key, hub)
            return "started"

        self._pending.append((bundle_key, hub))
        self._states[bundle_key] = STATE_QUEUED
        self.state_changed.emit(bundle_key, STATE_QUEUED, "排队中", "")
        self.queue_changed.emit()
        return "queued"

    def cancel(self, bundle_key: str):
        if bundle_key in self.pending_keys():
            self._pending = [(k, h) for k, h in self._pending if k != bundle_key]
            self._states[bundle_key] = STATE_CANCELLED
            self.state_changed.emit(bundle_key, STATE_CANCELLED, "已取消", "")
            self.queue_changed.emit()
            return
        if self._proc is None or bundle_key != self._current_key:
            return
        self.log_message.emit(bundle_key, "正在停止下载…")
        self.state_changed.emit(bundle_key, STATE_CANCELLED, "正在取消…", "")
        self._terminating = "cancelled"
        self._stall_timer.stop()
        self._force_kill()

    def shutdown(self):
        """窗口关闭时清理，避免残留子进程。"""
        self._pending.clear()
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.waitForFinished(2000)
            except Exception:
                pass
        self._proc = None

    # ─────────────── 内部：进程生命周期 ───────────────

    def _build_command(self, bundle_key: str, hub: str) -> tuple[list[str], str]:
        if getattr(sys, "frozen", False):
            return ([sys.executable, "--download-worker",
                     "--bundle", bundle_key, "--hub", hub], _project_root())
        return ([sys.executable, "-m", "core.download_worker",
                 "--bundle", bundle_key, "--hub", hub], _project_root())

    def _start(self, bundle_key: str, hub: str):
        cmd, cwd = self._build_command(bundle_key, hub)
        self._current_key = bundle_key
        self._current_hub = hub
        self._current_result = {}
        self._stdout_buf = ""
        self._last_payload = {}
        self._terminating = ""

        proc = QProcess(self)
        proc.setProgram(cmd[0])
        proc.setArguments(cmd[1:])
        proc.setWorkingDirectory(cwd)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_process_error)

        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("TQDM_DISABLE", "1")
        env.insert("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        proc.setProcessEnvironment(env)

        self._proc = proc
        self._states[bundle_key] = STATE_RUNNING
        self.state_changed.emit(bundle_key, STATE_RUNNING, "准备中…", "")
        self.log_message.emit(
            bundle_key,
            f"使用镜像：{hub}（{ 'ModelScope' if hub == 'ms' else 'HuggingFace' }）")
        self._stall_timer.start()

        proc.start()
        if not proc.waitForStarted(10_000):
            detail = proc.errorString()
            self._states[bundle_key] = STATE_FAILED
            self.state_changed.emit(
                bundle_key, STATE_FAILED,
                f"无法启动下载进程：{detail}",
                "请确认程序目录完整；若使用打包版本，请尝试重新解压后再运行。")
            self._cleanup_process()
            self._start_next()

    def _on_stdout(self):
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")
        self._stdout_buf += data
        while "\n" in self._stdout_buf:
            line, self._stdout_buf = self._stdout_buf.split("\n", 1)
            line = line.strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line: str):
        try:
            payload = json.loads(line)
        except Exception:
            # 非 JSON 输出（第三方库打印），当作日志
            self.log_message.emit(self._current_key, line[:400])
            return

        kind = payload.get("t")
        if kind == "log":
            self.log_message.emit(self._current_key, str(payload.get("m", "")))
        elif kind == "progress":
            prog = DownloadProgress(
                percent=int(payload.get("percent", -1)),
                done=int(payload.get("done", 0)),
                total=int(payload.get("total", 0)),
                speed=float(payload.get("speed", 0) or 0),
                eta=float(payload.get("eta", 0) or 0),
                file=str(payload.get("file", "") or ""),
                repo=str(payload.get("repo", "") or ""),
                repo_index=int(payload.get("repo_index", 0) or 0),
                repo_count=int(payload.get("repo_count", 0) or 0),
                message=str(payload.get("message", "") or ""),
            )
            self._last_payload = payload
            self._stall_timer.start()          # 有进展就续命
            self.progress_changed.emit(self._current_key, prog)
        elif kind == "done":
            self._current_result = payload

    def _on_process_error(self, error):
        if self._proc is None:
            return
        if error == QProcess.ProcessError.FailedToStart:
            return  # 已由 waitForStarted 处理
        # Crashed 会紧跟 finished，留给 _on_finished 统一处理

    def _on_finished(self, exit_code: int, _status):
        key = self._current_key
        result = self._current_result
        terminating = self._terminating
        stderr_text = ""
        if self._proc is not None:
            try:
                stderr_text = bytes(self._proc.readAllStandardError()).decode(
                    "utf-8", "replace").strip()
            except Exception:
                pass

        self._stall_timer.stop()
        self._kill_timer.stop()
        self._cleanup_process()

        if terminating == "cancelled":
            self._states[key] = STATE_CANCELLED
            self.state_changed.emit(key, STATE_CANCELLED, "已取消", "")
        elif terminating == "stalled":
            # 超时状态在 _on_stall 中已上报，这里只做清理
            pass
        elif result.get("ok") and exit_code == 0:
            self._states[key] = STATE_DONE
            self.state_changed.emit(key, STATE_DONE, "下载完成", "")
        elif result.get("cancelled"):
            self._states[key] = STATE_CANCELLED
            self.state_changed.emit(key, STATE_CANCELLED, "已取消", "")
        else:
            message = result.get("message") or stderr_text or \
                f"下载进程退出（代码 {exit_code}）"
            hint = result.get("hint", "")
            self._states[key] = STATE_FAILED
            self.state_changed.emit(key, STATE_FAILED, message, hint)

        self._start_next()

    def _cleanup_process(self):
        proc = self._proc
        self._proc = None
        self._current_key = ""
        if proc is not None:
            try:
                proc.readyReadStandardOutput.disconnect()
            except Exception:
                pass
            try:
                proc.finished.disconnect()
            except Exception:
                pass
            try:
                proc.deleteLater()
            except Exception:
                pass

    def _start_next(self):
        self.queue_changed.emit()
        if self._proc is not None or not self._pending:
            return
        key, hub = self._pending.pop(0)
        QTimer.singleShot(50, lambda k=key, h=hub: self._start(k, h))

    def _on_stall(self):
        key = self._current_key
        if self._proc is None:
            return
        self.log_message.emit(key, "长时间没有收到数据，判定为网络阻塞，正在中止…")
        self._terminating = "stalled"
        self._states[key] = STATE_FAILED
        self.state_changed.emit(
            key, STATE_FAILED, "下载超时（90 秒无进展）",
            "网络连接可能被中断或被代理拦截。已下载的部分会保留，重新点击下载可断点续传。")
        self._force_kill()

    def _force_kill(self):
        """直接结束子进程：Windows 下 terminate() 对无窗口控制台进程无效。"""
        if self._proc is None:
            return
        try:
            self._proc.kill()
        except Exception:
            pass
        self._kill_timer.start()


def mirror_hub(label: str) -> str:
    return MIRROR_SOURCES.get(label, "ms")
