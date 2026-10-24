"""Web 控制台的任务 / 模型 / 监控服务。

设计原则
--------
* **不依赖 PyQt**：可以在没有图形环境的 WSL 里独立运行；
* 单任务模型：同一时间只允许一个转写任务，重复提交直接拒绝；
* 所有状态都放在锁里，Flask 多线程访问安全；
* 处理流程复用 :func:`core.pipeline.run_pipeline`，与 GUI 行为一致。
"""

from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from app.utils import MediaError
from core.downloader import (
    DEFAULT_IGNORE, MODEL_BUNDLES, MIRROR_SOURCES, bundle_status, download_bundle,
    human_size,
)
from core.hardware_checker import detect_all, resolve_device
from core.hardware_monitor import HardwareMonitorSampler
from core.pipeline import run_pipeline
from core.transcriber import ModelLoadError

MAX_UPLOAD_BYTES = 2 * 1024 ** 3          # 单文件 2 GB
MAX_KEEP_UPLOADS = 6


@dataclass
class LogEntry:
    seq: int
    ts: float
    text: str


@dataclass
class JobState:
    state: str = "idle"          # idle / running / done / failed / cancelled
    progress: int = 0
    stage: str = ""
    message: str = ""
    hint: str = ""
    filename: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    result: list = field(default_factory=list)


class WebJobService:
    """线程安全的服务对象，被 Flask 路由直接调用。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._job = JobState()
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._logs: deque[LogEntry] = deque(maxlen=600)
        self._log_seq = 0

        self._uploads: dict[str, dict] = {}
        self._upload_order: list[str] = []

        self._sampler = HardwareMonitorSampler()
        self._sampler_lock = threading.Lock()
        self._last_metrics: dict = {}
        self._last_metrics_ts = 0.0
        self._sampler_ready = False

        self._downloads: dict[str, dict] = {}
        self._download_thread: Optional[threading.Thread] = None
        self._download_cancel = threading.Event()

        self.runtime_label = ""
        self.startup_note = ""

    # ─────────── 启动 ───────────

    def warm_up(self):
        """提前初始化传感器与设备探测，避免首个请求卡住。"""
        def _work():
            try:
                self.metrics()
            except Exception:
                pass
            try:
                self.devices()
            except Exception:
                pass

        threading.Thread(target=_work, name="web-warmup", daemon=True).start()

    # ─────────── 日志 ───────────

    def log(self, text: str):
        if not text:
            return
        with self._lock:
            self._log_seq += 1
            self._logs.append(LogEntry(self._log_seq, time.time(), str(text)))

    def logs_since(self, since: int = 0) -> dict:
        with self._lock:
            items = [{"seq": e.seq, "ts": e.ts, "text": e.text}
                     for e in self._logs if e.seq > since]
            return {"logs": items, "next": self._log_seq}

    # ─────────── 监控指标 ───────────

    def metrics(self, max_age: float = 1.5) -> dict:
        with self._sampler_lock:
            now = time.time()
            if self._last_metrics and now - self._last_metrics_ts < max_age:
                return self._last_metrics
            sample = self._sampler.sample()
            self._sampler_ready = sample is not None
            if sample is None:
                payload = {
                    "available": False,
                    "source": "不可用",
                    "notes": ["未能读取硬件传感器"],
                    "cpu": {"percent": -1, "detail": "", "name": ""},
                    "ram": {"percent": -1, "detail": "", "name": ""},
                    "gpu": {"percent": -1, "detail": "", "name": ""},
                }
            else:
                payload = {
                    "available": True,
                    "source": sample.source,
                    "notes": sample.notes,
                    "cpu": {"percent": sample.cpu.percent, "detail": sample.cpu.detail,
                            "name": sample.cpu.name},
                    "ram": {"percent": sample.ram.percent, "detail": sample.ram.detail,
                            "name": sample.ram.name},
                    "gpu": {"percent": sample.gpu.percent, "detail": sample.gpu.detail,
                            "name": sample.gpu.name},
                }
            self._last_metrics = payload
            self._last_metrics_ts = now
            return payload

    # ─────────── 设备 / 模型 ───────────

    def devices(self) -> list[dict]:
        result = []
        for d in detect_all():
            if not d.available:
                continue
            result.append({
                "value": d.device_string,
                "label": f"{d.backend.value}　{d.name}",
            })
        return result

    def models(self, hub: str = "ms") -> dict:
        items = []
        with self._lock:
            downloads = dict(self._downloads)
        for bundle in MODEL_BUNDLES.values():
            status = bundle_status(bundle, hub)
            dl = downloads.get(bundle.key, {})
            items.append({
                "key": bundle.key,
                "name": bundle.name,
                "description": bundle.description,
                "language": bundle.language,
                "approx_size": human_size(bundle.approx_mb * 1024 ** 2),
                "installed": status.installed,
                "repos_done": status.repos_done,
                "repos_total": status.repos_total,
                "disk_size": human_size(status.size_on_disk),
                "download": dl,
            })
        return {"bundles": items, "hub": hub,
                "hubs": list(MIRROR_SOURCES.keys())}

    def start_download(self, bundle_key: str, hub: str = "ms") -> tuple[bool, str]:
        if bundle_key not in MODEL_BUNDLES:
            return False, "未知模型包"
        with self._lock:
            current = self._downloads.get(bundle_key, {})
            if current.get("state") in ("queued", "running"):
                return False, "该模型包已在下载"
            if self._download_thread is not None and self._download_thread.is_alive():
                return False, "已有下载任务在进行，请稍候"
            self._downloads[bundle_key] = {"state": "running", "percent": 0,
                                          "message": "准备中"}
            self._download_cancel.clear()

        bundle = MODEL_BUNDLES[bundle_key]
        self.log(f"[模型] 开始下载 {bundle.name}（{hub}）")

        def _work():
            def on_progress(p):
                with self._lock:
                    entry = self._downloads.setdefault(bundle_key, {})
                    entry.update({
                        "state": "running",
                        "percent": p.percent,
                        "done": human_size(p.done),
                        "total": human_size(p.total),
                        "speed": human_size(p.speed) if p.speed else "",
                        "file": p.file,
                    })

            def on_log(msg: str):
                self.log(f"[模型] {msg}")

            result = download_bundle(
                bundle, hub, on_progress=on_progress, on_log=on_log,
                should_cancel=self._download_cancel.is_set)
            with self._lock:
                entry = self._downloads.setdefault(bundle_key, {})
                if result.get("ok"):
                    entry.update({"state": "done", "percent": 100, "message": "下载完成"})
                elif result.get("cancelled"):
                    entry.update({"state": "cancelled", "message": "已取消"})
                else:
                    entry.update({"state": "failed",
                                  "message": result.get("message", "下载失败"),
                                  "hint": result.get("hint", "")})
            self.log(f"[模型] {bundle.name}：{result.get('message', '')}")

        self._download_thread = threading.Thread(target=_work, name="web-download",
                                                 daemon=True)
        self._download_thread.start()
        return True, "已开始下载"

    # ─────────── 上传 ───────────

    def register_upload(self, filename: str, path: str, size: int) -> dict:
        upload_id = uuid.uuid4().hex
        with self._lock:
            self._uploads[upload_id] = {
                "name": os.path.basename(filename) or "upload.bin",
                "path": path, "size": size, "ts": time.time(),
            }
            self._upload_order.append(upload_id)
            while len(self._upload_order) > MAX_KEEP_UPLOADS:
                old = self._upload_order.pop(0)
                info = self._uploads.pop(old, None)
                if info:
                    try:
                        os.unlink(info["path"])
                    except OSError:
                        pass
        return {"id": upload_id, "name": os.path.basename(filename), "size": size}

    def _upload_path(self, upload_id: str) -> Optional[str]:
        with self._lock:
            info = self._uploads.get(upload_id)
        return info["path"] if info else None

    # ─────────── 任务 ───────────

    def snapshot(self) -> dict:
        with self._lock:
            job = self._job
            return {
                "state": job.state,
                "progress": job.progress,
                "stage": job.stage,
                "message": job.message,
                "hint": job.hint,
                "filename": job.filename,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "elapsed": (job.finished_at or time.time()) - job.started_at
                if job.started_at else 0,
                "sentences": len(job.result),
                "downloads": dict(self._downloads),
                "runtime": self.runtime_label,
                "startup_note": self.startup_note,
                "monitor_ready": self._sampler_ready,
            }

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start_job(self, upload_id: str, model_name: str, device: str = "auto",
                  max_speakers: int = 0, vad_seg: int = 30,
                  batch_size: int = 1) -> tuple[bool, str]:
        path = self._upload_path(upload_id)
        if not path or not os.path.isfile(path):
            return False, "文件不存在，请重新上传"

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, "已有任务在运行中"
            info = self._uploads.get(upload_id, {})
            self._job = JobState(
                state="running", progress=0, stage="准备中",
                filename=info.get("name", os.path.basename(path)),
                started_at=time.time(),
            )
            self._cancel.clear()

        resolved = resolve_device(device)
        self.log(f"任务开始：{os.path.basename(path)}　模型 {model_name}　设备 {resolved}")

        def _work():
            def _progress(pct: int, stage: str = ""):
                with self._lock:
                    self._job.progress = max(self._job.progress, int(pct))
                    if stage:
                        self._job.stage = stage

            def _should_cancel() -> bool:
                return self._cancel.is_set()

            try:
                result = run_pipeline(
                    path, model_name, resolved,
                    batch_size_s=300,
                    batch_size=batch_size,
                    max_speakers=max_speakers,
                    vad_max_seg_sec=vad_seg,
                    log=self.log,
                    progress=_progress,
                    should_cancel=_should_cancel,
                )
                with self._lock:
                    self._job.result = result
                    self._job.state = "done"
                    self._job.progress = 100
                    self._job.stage = "完成"
                    self._job.finished_at = time.time()
                self.log(f"任务完成，共 {len(result)} 条语句")
            except ModelLoadError as e:
                self._fail(str(e), e.hint)
            except MediaError as e:
                if str(e) == "已取消":
                    with self._lock:
                        self._job.state = "cancelled"
                        self._job.stage = "已取消"
                        self._job.finished_at = time.time()
                    self.log("任务已取消")
                else:
                    self._fail(str(e), e.hint)
            except Exception as e:  # noqa: BLE001
                self.log(traceback.format_exc(limit=4))
                self._fail(f"{type(e).__name__}: {e}", "查看日志获取完整堆栈")

        self._thread = threading.Thread(target=_work, name="web-job", daemon=True)
        self._thread.start()
        return True, "已开始"

    def _fail(self, message: str, hint: str = ""):
        with self._lock:
            self._job.state = "failed"
            self._job.message = message
            self._job.hint = hint
            self._job.finished_at = time.time()
        self.log(f"任务失败：{message}")

    def cancel_job(self) -> bool:
        if not self.is_running():
            return False
        self._cancel.set()
        self.log("已请求取消，等待当前步骤结束")
        return True

    def result(self) -> list[dict]:
        with self._lock:
            return list(self._job.result)
