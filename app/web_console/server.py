"""Flask 应用：WSLg 不可用时的备用 Web 控制台。

所有业务逻辑在 :class:`app.web_console.service.WebJobService`，
这里只做 HTTP 路由与静态资源。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid

from flask import Flask, jsonify, render_template, request, send_file

from app.runtime_env import detect_runtime
from core.exporter import EXPORT_MAP, export_result

from .service import MAX_UPLOAD_BYTES, WebJobService

_HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(_HERE, "templates")
STATIC_DIR = os.path.join(_HERE, "static")

UPLOAD_DIR = tempfile.mkdtemp(prefix="funasr-web-")

ALLOWED_SUFFIXES = (
    ".mp4", ".avi", ".mkv", ".mov", ".flv", ".wmv", ".webm",
    ".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".wma",
)


def create_app(service: WebJobService | None = None) -> Flask:
    service = service or WebJobService()
    app = Flask(
        __name__,
        template_folder=TEMPLATE_DIR,
        static_folder=STATIC_DIR,
        static_url_path="/static",
    )
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.config["JSON_AS_ASCII"] = False
    app.config["service"] = service

    runtime = detect_runtime()
    service.runtime_label = runtime.label

    if not service.runtime_label:
        service.runtime_label = "本地"

    # ─────────── 页面 ───────────

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            runtime_label=service.runtime_label,
            startup_note=service.startup_note,
            mirrored=service.startup_note.startswith("WSLg"),
            max_upload_mb=int(MAX_UPLOAD_BYTES / 1024 ** 2),
            export_formats=["TXT", "JSON", "SRT", "CSV", "MD"],
        )

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "ts": time.time()})

    # ─────────── 状态 / 监控 / 日志 ───────────

    @app.get("/api/state")
    def api_state():
        payload = service.snapshot()
        payload["platform"] = {
            "runtime": service.runtime_label,
            "note": service.startup_note,
        }
        return jsonify(payload)

    @app.get("/api/metrics")
    def api_metrics():
        return jsonify(service.metrics())

    @app.get("/api/devices")
    def api_devices():
        return jsonify({"devices": service.devices()})

    @app.get("/api/logs")
    def api_logs():
        try:
            since = int(request.args.get("since", 0))
        except ValueError:
            since = 0
        return jsonify(service.logs_since(since))

    # ─────────── 模型 ───────────

    @app.get("/api/models")
    def api_models():
        hub = request.args.get("hub", "ms")
        return jsonify(service.models(hub))

    @app.post("/api/models/download")
    def api_models_download():
        data = request.get_json(silent=True) or {}
        ok, message = service.start_download(data.get("key", ""),
                                              data.get("hub", "ms"))
        return jsonify({"ok": ok, "message": message}), (200 if ok else 409)

    # ─────────── 上传 / 任务 ───────────

    @app.post("/api/upload")
    def api_upload():
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"ok": False, "message": "没有收到文件"}), 400

        name = os.path.basename(file.filename)
        if not name.lower().endswith(ALLOWED_SUFFIXES):
            return jsonify({
                "ok": False,
                "message": "不支持的文件类型，请上传音视频文件",
            }), 400

        target = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{name}")
        file.save(target)
        size = os.path.getsize(target)
        if size == 0:
            try:
                os.unlink(target)
            except OSError:
                pass
            return jsonify({"ok": False, "message": "文件为空"}), 400

        info = service.register_upload(name, target, size)
        service.log(f"上传文件：{name}（{size / 1024 ** 2:.1f} MB）")
        return jsonify({"ok": True, **info})

    @app.post("/api/start")
    def api_start():
        data = request.get_json(silent=True) or {}
        upload_id = data.get("upload_id", "")
        model = data.get("model", "SenseVoiceSmall")
        device = data.get("device", "auto")
        try:
            max_speakers = int(data.get("max_speakers", 0) or 0)
            vad_seg = int(data.get("vad_seg", 30) or 30)
            batch_size = int(data.get("batch_size", 1) or 1)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "参数格式不正确"}), 400

        ok, message = service.start_job(
            upload_id, model, device,
            max_speakers=max_speakers, vad_seg=vad_seg, batch_size=batch_size)
        if ok:
            return jsonify({"ok": True, "message": message})
        status = 409 if "运行中" in message else 400
        return jsonify({"ok": False, "message": message}), status

    @app.post("/api/cancel")
    def api_cancel():
        ok = service.cancel_job()
        return jsonify({"ok": ok, "message": "已请求取消" if ok else "没有运行中的任务"})

    @app.get("/api/sentences")
    def api_sentences():
        return jsonify({"sentences": service.result()})

    @app.get("/api/result")
    def api_result():
        sentences = service.result()
        if not sentences:
            return jsonify({"ok": False, "message": "没有结果"}), 404
        fmt = (request.args.get("fmt") or "json").upper()
        if fmt not in EXPORT_MAP:
            return jsonify({"ok": False, "message": "不支持的格式"}), 400

        suffix = "." + fmt.lower()
        tmp = tempfile.NamedTemporaryFile(prefix="transcript_", suffix=suffix,
                                          delete=False)
        tmp.close()
        try:
            export_result(sentences, fmt, tmp.name)
        except Exception as e:  # noqa: BLE001
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            return jsonify({"ok": False, "message": str(e)}), 500

        download_name = f"transcript{suffix}"
        return send_file(tmp.name, as_attachment=True,
                         download_name=download_name)

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({
            "ok": False,
            "message": f"文件超过 {int(MAX_UPLOAD_BYTES / 1024 ** 2)} MB 限制",
        }), 413

    return app


def run_web_console(host: str = "127.0.0.1", port: int = 8420,
                    note: str = "", open_browser: bool = False) -> int:
    """启动 Web 控制台（阻塞）。返回退出码。"""
    try:
        from flask import Flask  # noqa: F401
    except ImportError:
        print("[web] 缺少 Flask，请执行：pip install flask", flush=True)
        return 1

    service = WebJobService()
    service.startup_note = note
    app = create_app(service)
    service.warm_up()

    url = f"http://{host if host not in ('0.0.0.0', '::') else '127.0.0.1'}:{port}"
    print("", flush=True)
    print("  Maple-FunASR Web 控制台已启动", flush=True)
    print(f"  地址：{url}", flush=True)
    if note:
        print(f"  原因：{note}", flush=True)
    print("  按 Ctrl+C 退出", flush=True)
    print("", flush=True)

    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass

    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        service.cancel_job()
    return 0


if __name__ == "__main__":
    # 便于在 WSL / 服务器上直接运行：python -m app.web_console.server --port 8420
    import argparse

    parser = argparse.ArgumentParser(description="Maple-FunASR Web 控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    sys.exit(run_web_console(host=args.host, port=args.port, note=args.note))
