"""无显示环境冒烟测试（CI / WSL2 用）。

覆盖三件事：
1. 所有"必须能在无 GUI 环境运行"的模块都能导入，且**不会顺带导入 PyQt6**；
2. WSL2 环境判定：有 WSLg / 无 WSLg 两种情况下的平台候选是否正确；
3. Flask Web 控制台的关键接口可用（用 test_client，不需要起服务）。

用法::

    python tests/test_smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILED: list[str] = []


def check(name: str, fn):
    try:
        fn()
        print(f"  ok    {name}")
    except Exception as e:  # noqa: BLE001
        FAILED.append(f"{name}: {type(e).__name__}: {e}")
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")


# ─────────────────────────── 1. 无 Qt 模块 ───────────────────────────


def test_headless_imports():
    assert "PyQt6" not in sys.modules, "PyQt6 不应被无界面模块导入"

    import app.runtime_env  # noqa: F401
    import app.web_console.server as server  # noqa: F401
    import app.web_console.service as service  # noqa: F401
    import core.hardware_monitor  # noqa: F401
    import core.pipeline  # noqa: F401
    import core.downloader  # noqa: F401
    import core.exporter  # noqa: F401

    assert "PyQt6" not in sys.modules, "导入 Web 控制台后不应引入 PyQt6"
    assert callable(core.pipeline.run_pipeline)
    assert hasattr(core.hardware_monitor, "HardwareMonitorSampler")
    assert server.create_app is not None
    assert service.WebJobService is not None


def test_web_console_in_fresh_interpreter():
    """在干净子进程里确认 Web 控制台不依赖 Qt。"""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import app.web_console.server as s\n"
        "assert 'PyQt6' not in sys.modules, 'PyQt6 imported'\n"
        "app = s.create_app()\n"
        "c = app.test_client()\n"
        "assert c.get('/api/health').status_code == 200\n"
        "print('WEB_OK')\n" % ROOT
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=180)
    if "WEB_OK" not in out.stdout:
        raise AssertionError(
            f"子进程校验失败（code={out.returncode}）：{out.stderr.strip()[-500:]}")


# ─────────────────────────── 2. 运行时判定 ───────────────────────────


def test_runtime_detection():
    from app.runtime_env import (
        RuntimeInfo, gui_platform_candidates, to_wsl_path,
    )

    no_wslg = RuntimeInfo(is_linux=True, is_wsl=True, is_wslg=False,
                          distro="Ubuntu", display="")
    assert gui_platform_candidates(no_wslg) == [], "无 WSLg 时不应有平台候选"

    wslg = RuntimeInfo(is_linux=True, is_wsl=True, is_wslg=True,
                       distro="Ubuntu", display=":0", wayland_display="wayland-0")
    candidates = gui_platform_candidates(wslg)
    assert candidates and candidates[0] == "xcb", f"WSLg 应优先 xcb，实际 {candidates}"
    assert "wayland" in candidates

    native = RuntimeInfo(is_linux=True, is_wsl=False)
    assert gui_platform_candidates(native) == [None]

    windows = RuntimeInfo(is_windows=True)
    assert gui_platform_candidates(windows) == [None]

    assert to_wsl_path(r"C:\Users\me\a.wav") == "/mnt/c/Users/me/a.wav"
    assert to_wsl_path("/home/me/a.wav") == "/home/me/a.wav"


def test_detect_runtime_under_env():
    """模拟 WSL2（无 WSLg）环境变量，确认降级判定。"""
    code = (
        "import sys, os; sys.path.insert(0, %r)\n"
        "os.environ['WSL_DISTRO_NAME'] = 'Ubuntu-Test'\n"
        "os.environ.pop('DISPLAY', None)\n"
        "os.environ.pop('WAYLAND_DISPLAY', None)\n"
        "from app.runtime_env import detect_runtime, gui_platform_candidates\n"
        "info = detect_runtime()\n"
        "print('WSL', info.is_wsl, 'WSLG', info.is_wslg, "
        "'CAND', len(gui_platform_candidates(info)))\n" % ROOT
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120)
    line = (out.stdout or "").strip().splitlines()[-1] if out.stdout.strip() else ""
    if "WSL True" not in line or "WSLG False" not in line or "CAND 0" not in line:
        raise AssertionError(f"WSL2 降级判定异常：{line!r} / {out.stderr.strip()[-300:]}")


# ─────────────────────────── 3. Web 接口 ───────────────────────────


def test_web_api():
    from app.web_console.server import create_app

    app = create_app()
    client = app.test_client()

    r = client.get("/api/health")
    assert r.status_code == 200 and r.get_json()["ok"] is True

    state = client.get("/api/state").get_json()
    for key in ("state", "progress", "downloads", "runtime"):
        assert key in state, f"/api/state 缺少字段 {key}"
    assert state["state"] == "idle"

    models = client.get("/api/models").get_json()
    assert len(models["bundles"]) >= 3
    assert models["bundles"][0]["key"] in ("sensevoice", "paraformer", "funasr-nano")

    page = client.get("/")
    assert page.status_code == 200
    html = page.data.decode("utf-8")
    assert "Maple-FunASR 控制台" in html
    assert "app.js" in html

    bad = client.post("/api/start", json={"upload_id": "missing"})
    assert bad.status_code == 400, f"无效上传应返回 400，实际 {bad.status_code}"

    logs = client.get("/api/logs?since=0").get_json()
    assert "logs" in logs and "next" in logs


def test_web_state_serialisable():
    from app.web_console.server import create_app

    app = create_app()
    client = app.test_client()
    # 确认所有接口返回的都是可 JSON 序列化的结构（没有 dataclass / 路径对象漏出）
    for path in ("/api/state", "/api/metrics", "/api/devices", "/api/models", "/api/logs"):
        payload = client.get(path).get_json()
        json.dumps(payload)
        assert isinstance(payload, dict), f"{path} 应返回对象"


# ─────────────────────────── 4. 模型清单一致性 ───────────────────────────


def test_model_registry_matches_bundles():
    from core.downloader import MODEL_BUNDLES
    from core.transcriber import MODEL_REGISTRY

    for name, cfg in MODEL_REGISTRY.items():
        key = cfg.get("bundle")
        assert key in MODEL_BUNDLES, f"{name} 引用了不存在的模型包 {key}"


def main() -> int:
    print("== 无显示环境冒烟测试 ==")
    check("无 Qt 模块导入", test_headless_imports)
    check("干净子进程中的 Web 控制台", test_web_console_in_fresh_interpreter)
    check("运行时判定（WSL/WSLg/平台候选）", test_runtime_detection)
    check("WSL2 无 WSLg 降级判定", test_detect_runtime_under_env)
    check("Web 接口", test_web_api)
    check("Web 接口可序列化", test_web_state_serialisable)
    check("模型清单一致性", test_model_registry_matches_bundles)

    if FAILED:
        print("\n失败项：")
        for item in FAILED:
            print("  -", item)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
