"""模型下载子进程入口。

把下载放在独立进程里跑，好处是：

* 下载再慢也不会拖住 UI（Qt 主线程只负责读管道）；
* 取消可以真正 ``terminate()``（进程内线程无法安全强杀）；
* 万一底层库崩溃（SSL / 解压 / 内存），挂掉的是子进程，主界面不受影响。

对外协议：stdout 上逐行输出 JSON，字段统一为 ``{"t": ...}``：

    {"t": "log",      "m": "..."}
    {"t": "progress", "percent": 42, "done": 1, "total": 2, ...}
    {"t": "done",     "ok": true, "message": "...", "hint": ""}

调用方式（仓库根目录下）::

    python -m core.download_worker --bundle sensevoice --hub ms
    FunASR-Desktop.exe --download-worker --bundle sensevoice --hub ms
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CANCELLED = 2


def _emit(payload: dict) -> None:
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--download-worker" in argv:
        argv.remove("--download-worker")

    parser = argparse.ArgumentParser(prog="download-worker", add_help=False)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--hub", default="ms", choices=["ms", "hf"])
    try:
        args, _unknown = parser.parse_known_args(argv)
    except SystemExit:
        _emit({"t": "done", "ok": False, "message": "参数错误", "hint": ""})
        return EXIT_FAILED

    # 子进程里禁止任何 tqdm / 日志噪声污染 stdout
    os.environ.setdefault("TQDM_DISABLE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("MODELSCOPE_LOG_LEVEL", "40")

    from core.downloader import MODEL_BUNDLES, download_bundle

    bundle = MODEL_BUNDLES.get(args.bundle)
    if bundle is None:
        _emit({"t": "done", "ok": False,
               "message": f"未知的模型包: {args.bundle}", "hint": ""})
        return EXIT_FAILED

    def on_log(msg: str):
        _emit({"t": "log", "m": str(msg)})

    def on_progress(prog):
        payload = {"t": "progress"}
        payload.update(asdict(prog))
        _emit(payload)

    try:
        result = download_bundle(bundle, args.hub,
                                 on_progress=on_progress, on_log=on_log)
    except BaseException as e:  # noqa: BLE001
        import traceback
        _emit({"t": "log", "m": f"内部异常: {type(e).__name__}: {e}"})
        _emit({"t": "log", "m": traceback.format_exc(limit=4)})
        _emit({"t": "done", "ok": False,
               "message": f"下载进程异常：{type(e).__name__}", "hint": str(e)[:200]})
        return EXIT_FAILED

    ok = bool(result.get("ok"))
    _emit({"t": "done", "ok": ok,
           "message": result.get("message", ""),
           "hint": result.get("hint", ""),
           "cancelled": bool(result.get("cancelled"))})
    if ok:
        return EXIT_OK
    return EXIT_CANCELLED if result.get("cancelled") else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
