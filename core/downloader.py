"""模型下载（纯 Python，不依赖 Qt）。

设计要点
--------
* **按 "模型包" 下载**：FunASR 除了主模型还需要 VAD / 标点 / 说话人模型，
  只下主模型会在推理时触发隐藏下载（表现为界面卡死），所以这里一次把
  一个模型包需要的仓库全部拉齐。
* **字节级进度**：通过 ModelScope / HuggingFace 的进度回调聚合真实字节数，
  不再用 "10% -> 100%" 的假进度。
* **可中断**：``should_cancel`` 在仓库之间生效；真正的强杀由上层进程管理器
  负责（见 :mod:`core.download_worker`）。
* **失败可诊断**：网络 / 认证 / 仓库不存在 / 磁盘不足分别给出可操作提示，
  绝不把异常抛到 UI 线程。
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

# 这些文件对推理无用，下载时跳过，能省不少带宽
DEFAULT_IGNORE = [
    "example/*", "examples/*", "fig/*", "figs/*", "images/*",
    "*.md", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.gitattributes",
]

WEIGHT_SUFFIXES = (".pt", ".pth", ".bin", ".safetensors", ".onnx", ".ckpt", ".gguf")

# FunASR 常用子模型的仓库 ID（ModelScope / HuggingFace 两套命名）
VAD_MS = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
VAD_HF = "funasr/fsmn-vad"
PUNC_MS = "iic/punc_ct-transformer_cn-en-common-vocab471067-large"
PUNC_HF = "funasr/ct-punc"
SPK_MS = "iic/speech_campplus_sv_zh-cn_16k-common"
SPK_HF = "funasr/campplus"


@dataclass(frozen=True)
class ModelBundle:
    """一个可直接用于推理的完整模型组合。"""

    key: str
    name: str
    description: str
    ms_repos: tuple[str, ...]
    hf_repos: tuple[str, ...]
    approx_mb: int = 0
    language: str = ""

    def repos(self, hub: str) -> tuple[str, ...]:
        return self.hf_repos if hub == "hf" else self.ms_repos

    def primary_repo(self, hub: str) -> str:
        repos = self.repos(hub)
        return repos[0] if repos else ""


MODEL_BUNDLES: dict[str, ModelBundle] = {
    "sensevoice": ModelBundle(
        key="sensevoice",
        name="SenseVoiceSmall",
        description="多语言识别 + 情感/事件标签，速度最快，推荐首选",
        language="中/英/日/韩/粤",
        approx_mb=925,
        ms_repos=("iic/SenseVoiceSmall", VAD_MS, SPK_MS),
        hf_repos=("FunAudioLLM/SenseVoiceSmall", VAD_HF, SPK_HF),
    ),
    "paraformer": ModelBundle(
        key="paraformer",
        name="Paraformer 中文",
        description="中文长音频精准识别，带标点与说话人分离",
        language="中文",
        approx_mb=2115,
        ms_repos=(
            "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            VAD_MS, PUNC_MS, SPK_MS,
        ),
        hf_repos=("funasr/paraformer-zh", VAD_HF, PUNC_HF, SPK_HF),
    ),
    "funasr-nano": ModelBundle(
        key="funasr-nano",
        name="Fun-ASR-Nano",
        description="31 种语言，轻量端到端，适合多语种素材",
        language="31 种语言",
        approx_mb=2075,
        ms_repos=("FunAudioLLM/Fun-ASR-Nano-2512", VAD_MS, SPK_MS),
        hf_repos=("FunAudioLLM/Fun-ASR-Nano-2512", VAD_HF, SPK_HF),
    ),
}

# FunASR 内部使用的模型别名 -> 仓库 ID（用于把推理配置映射回模型包）
FUNASR_ALIAS_TO_MS = {
    "paraformer-zh": ("iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
                      "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"),
    "fsmn-vad": (VAD_MS,),
    "ct-punc": (PUNC_MS,),
    "cam++": (SPK_MS,),
}

# 子模型的中文名，界面上比仓库 ID 更易读
COMPONENT_LABELS = {
    VAD_MS: "VAD 端点检测", VAD_HF: "VAD 端点检测",
    PUNC_MS: "标点恢复", PUNC_HF: "标点恢复",
    SPK_MS: "说话人分离", SPK_HF: "说话人分离",
}


def repo_friendly_name(repo_id: str) -> str:
    if repo_id in COMPONENT_LABELS:
        return COMPONENT_LABELS[repo_id]
    if "paraformer" in repo_id.lower():
        return "Paraformer 主模型"
    if repo_id.startswith("iic/SenseVoice") or repo_id.startswith("FunAudioLLM/SenseVoice"):
        return "SenseVoice 主模型"
    if "Fun-ASR" in repo_id:
        return "Fun-ASR 主模型"
    return repo_id.split("/")[-1]


MIRROR_SOURCES = {
    "ModelScope（魔搭，国内推荐）": "ms",
    "HuggingFace（海外 / 需可访问）": "hf",
}

DEFAULT_MIRROR_LABEL = next(iter(MIRROR_SOURCES))


# ─────────────────────────── 进度 ───────────────────────────


@dataclass
class DownloadProgress:
    percent: int = -1          # -1 表示总量未知
    done: int = 0
    total: int = 0
    speed: float = 0.0         # 字节/秒
    eta: float = 0.0           # 秒
    file: str = ""
    repo: str = ""
    repo_index: int = 0
    repo_count: int = 0
    message: str = ""


class DownloadCancelled(Exception):
    pass


class DownloadFailed(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class _Aggregator:
    """把多个仓库 / 多个文件的回调汇总成一条进度。"""

    def __init__(self, total: int, callback: Optional[Callable[[DownloadProgress], None]],
                 repo_count: int, should_cancel: Optional[Callable[[], bool]] = None):
        self.total = total
        self.callback = callback
        self.repo_count = repo_count
        self.should_cancel = should_cancel
        self.lock = threading.Lock()
        self.done = 0
        self.cancelled = False
        self.repo = ""
        self.repo_index = 0
        self.current_file = ""
        self._samples: deque[tuple[float, int]] = deque(maxlen=24)
        self._last_emit = 0.0
        self._last_percent = -1

    # --- 生命周期 ---
    def begin_repo(self, repo: str, index: int):
        with self.lock:
            self.repo = repo
            self.repo_index = index
            self.current_file = ""
        self._emit(force=True)

    def start_file(self, filename: str, file_size: int):
        name = os.path.basename(filename) or filename
        with self.lock:
            self.current_file = name
        self._emit(force=True)

    def finish_file(self, filename: str):
        self._emit(force=True)

    def add(self, delta: int, filename: str = ""):
        if self.should_cancel and self.should_cancel():
            with self.lock:
                self.cancelled = True
        with self.lock:
            self.done += max(0, int(delta))
            now = time.monotonic()
            self._samples.append((now, self.done))
            if filename:
                self.current_file = os.path.basename(filename) or filename
        self._emit()

    # --- 输出 ---
    def _rate(self) -> tuple[float, float]:
        with self.lock:
            if len(self._samples) < 2:
                return 0.0, 0.0
            t0, b0 = self._samples[0]
            t1, b1 = self._samples[-1]
            dt = t1 - t0
            # 采样窗口太短时算出来的速度会非常离谱，直接不显示
            if dt < 1.0:
                return 0.0, 0.0
            speed = max(0.0, (b1 - b0) / dt)
            if speed < 1024:
                return 0.0, 0.0
            eta = (self.total - self.done) / speed if self.total else 0.0
            return speed, eta

    def snapshot(self, message: str = "") -> DownloadProgress:
        speed, eta = self._rate()
        percent = int(min(100, self.done * 100 / self.total)) if self.total > 0 else -1
        return DownloadProgress(
            percent=percent, done=self.done, total=self.total,
            speed=speed, eta=eta, file=self.current_file,
            repo=self.repo, repo_index=self.repo_index,
            repo_count=self.repo_count, message=message,
        )

    def _emit(self, force: bool = False):
        if not self.callback:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < 0.15:
            return
        prog = self.snapshot()
        if not force and prog.percent == self._last_percent and prog.percent >= 0:
            # 百分比没变时仍然允许 1s 一次的刷新，用于更新速度显示
            if now - self._last_emit < 1.0:
                return
        self._last_emit = now
        self._last_percent = prog.percent
        try:
            self.callback(prog)
        except Exception:
            pass


# ─────────────────────────── 缓存目录识别 ───────────────────────────


def _unique(paths: Iterable[Optional[str]]) -> list[str]:
    seen = []
    for p in paths:
        if not p:
            continue
        p = os.path.abspath(os.path.expanduser(str(p)))
        if p not in seen:
            seen.append(p)
    return seen


def modelscope_cache_roots() -> list[str]:
    home = Path.home()
    roots = [
        os.environ.get("MODELSCOPE_CACHE"),
        os.environ.get("MODELSCOPE_HOME"),
        str(home / ".cache" / "modelscope"),
        str(home / ".cache" / "modelscope" / "hub"),
        str(home / ".cache" / "modelscope" / "hub" / "models"),
    ]
    return _unique(roots)


def hf_cache_roots() -> list[str]:
    home = Path.home()
    hf_home = os.environ.get("HF_HOME")
    roots = [
        os.environ.get("HF_HUB_CACHE"),
        os.environ.get("HUGGINGFACE_HUB_CACHE"),
        os.path.join(hf_home, "hub") if hf_home else None,
        str(home / ".cache" / "huggingface" / "hub"),
        os.environ.get("TRANSFORMERS_CACHE"),
    ]
    return _unique(roots)


def _repo_dir_candidates(hub: str, repo_id: str) -> list[str]:
    """列出某个仓库在所有已知缓存布局下的候选目录。"""
    org, _, name = repo_id.partition("/")
    if not name:
        org, name = "_", repo_id
    cands: list[str] = []

    if hub == "ms":
        for root in modelscope_cache_roots():
            # modelscope_hub >= 0.4：<root>/models/org--name/snapshots/<rev>
            cands.append(os.path.join(root, "models", f"{org}--{name}", "snapshots"))
            # <root>/models/org/name/snapshots
            cands.append(os.path.join(root, "models", org, name, "snapshots"))
            # 旧版：<root>/hub/models/org/name
            cands.append(os.path.join(root, "hub", "models", org, name))
            # 最旧版：<root>/hub/org_name
            cands.append(os.path.join(root, "hub", f"{org}_{name}"))
            cands.append(os.path.join(root, "hub", org, name))
    else:
        for root in hf_cache_roots():
            cands.append(os.path.join(
                root, "models--" + repo_id.replace("/", "--"), "snapshots"))
    return _unique(cands)


def _has_weights(path: str) -> bool:
    """目录里是否真的有模型权重（避免把空目录 / 半成品当成已安装）。"""
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                low = f.lower()
                if low.endswith(WEIGHT_SUFFIXES):
                    try:
                        if os.path.getsize(os.path.join(root, f)) > 64 * 1024:
                            return True
                    except OSError:
                        continue
    except OSError:
        return False
    return False


def find_installed_path(hub: str, repo_id: str) -> Optional[str]:
    """返回仓库本地目录（若已完整下载）。"""
    for cand in _repo_dir_candidates(hub, repo_id):
        if not os.path.isdir(cand):
            continue
        # snapshots 目录下还有一层 revision
        if os.path.basename(cand) == "snapshots":
            try:
                revs = [os.path.join(cand, d) for d in os.listdir(cand)]
            except OSError:
                revs = []
            for rev in revs:
                if os.path.isdir(rev) and _has_weights(rev):
                    return rev
            continue
        if _has_weights(cand):
            return cand
    return None


def repo_installed(hub: str, repo_id: str) -> bool:
    return find_installed_path(hub, repo_id) is not None


def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    if not os.path.islink(fp):
                        total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total


@dataclass
class BundleStatus:
    key: str
    installed: bool
    repos_done: int
    repos_total: int
    missing: list[str] = field(default_factory=list)
    size_on_disk: int = 0

    @property
    def partial(self) -> bool:
        return 0 < self.repos_done < self.repos_total


def bundle_status(bundle: ModelBundle, hub: str = "ms") -> BundleStatus:
    repos = bundle.repos(hub)
    done, missing, size = 0, [], 0
    for repo in repos:
        path = find_installed_path(hub, repo)
        if path:
            done += 1
            size += _dir_size(path)
        else:
            missing.append(repo)
    return BundleStatus(
        key=bundle.key, installed=done == len(repos) and len(repos) > 0,
        repos_done=done, repos_total=len(repos),
        missing=missing, size_on_disk=size,
    )


def all_status(hub: str = "ms") -> dict[str, BundleStatus]:
    return {k: bundle_status(b, hub) for k, b in MODEL_BUNDLES.items()}


# ─────────────────────────── 大小预估 ───────────────────────────


def _matches_ignore(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def repo_file_sizes(repo_id: str, hub: str = "ms",
                    ignore: Optional[list[str]] = None) -> dict[str, int]:
    """获取仓库内需要下载的文件清单与大小（只读元数据，很快）。"""
    ignore = DEFAULT_IGNORE if ignore is None else ignore
    result: dict[str, int] = {}
    if hub == "ms":
        try:
            from modelscope_hub import HubApi
            files = HubApi().list_repo_files(repo_id, repo_type="model")
            for f in files:
                path = getattr(f, "path", "") or ""
                ftype = getattr(f, "type", "") or ""
                if ftype and ftype != "blob":
                    continue
                if not path or _matches_ignore(path, ignore):
                    continue
                result[path] = int(getattr(f, "size", 0) or 0)
            if result:
                return result
        except Exception:
            pass
        # 兼容旧接口
        try:
            from modelscope.hub.api import HubApi as LegacyHubApi
            files = LegacyHubApi().get_model_files(repo_id, recursive=True)
            for f in files:
                path = f.get("Path") or ""
                if not path or f.get("Type") == "tree" or _matches_ignore(path, ignore):
                    continue
                result[path] = int(f.get("Size") or 0)
        except Exception:
            pass
        return result

    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(repo_id, files_metadata=True)
        for sib in info.siblings or []:
            path = getattr(sib, "rfilename", "") or ""
            if not path or _matches_ignore(path, ignore):
                continue
            result[path] = int(getattr(sib, "size", 0) or 0)
    except Exception:
        pass
    return result


def bundle_remote_size(bundle: ModelBundle, hub: str = "ms") -> int:
    total = 0
    for repo in bundle.repos(hub):
        sizes = repo_file_sizes(repo, hub)
        total += sum(sizes.values())
    return total


# ─────────────────────────── 下载实现 ───────────────────────────


def _classify_error(exc: BaseException, hub: str) -> DownloadFailed:
    name = type(exc).__name__
    text = str(exc) or name
    low = text.lower()

    if isinstance(exc, DownloadCancelled):
        return DownloadFailed("已取消")

    if any(k in low for k in ("proxy", "connection", "timed out", "timeout",
                              "ssl", "certificate", "resolve", "network",
                              "max retries", "getaddrinfo", "unreachable")):
        source = "ModelScope" if hub == "ms" else "HuggingFace"
        return DownloadFailed(
            f"网络连接失败（{text[:160]}）",
            hint=f"无法连接 {source}。请检查网络 / 代理设置，或切换到另一个镜像源后重试。",
        )
    if any(k in low for k in ("401", "403", "auth", "token", "permission",
                              "unauthorized", "gated")):
        return DownloadFailed(
            f"认证失败（{text[:160]}）",
            hint="该仓库可能需要登录或授权，请先在命令行执行 modelscope login / huggingface-cli login。",
        )
    if any(k in low for k in ("404", "not found", "notexist", "does not exist")):
        return DownloadFailed(
            f"仓库不存在或已下线（{text[:160]}）",
            hint="可能是模型 ID 变更，请切换到另一个镜像源，或更新程序内的模型清单。",
        )
    if any(k in low for k in ("no space", "disk", "空间")):
        return DownloadFailed(
            f"磁盘空间不足（{text[:160]}）",
            hint="请清理模型缓存目录后重试。",
        )
    if isinstance(exc, MemoryError):
        return DownloadFailed("内存不足", hint="请关闭其他占用内存的程序后重试。")
    return DownloadFailed(f"下载失败：{text[:300]}")


def _ensure_disk_space(required: int, target_dir: str):
    """下载前检查磁盘余量，避免下到一半失败。"""
    if required <= 0:
        return
    try:
        probe = target_dir
        while probe and not os.path.isdir(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        free = shutil.disk_usage(probe).free
    except Exception:
        return
    if free < required * 1.1:
        raise DownloadFailed(
            f"磁盘空间不足：需要约 {required / 1024 ** 2:.0f} MB，"
            f"剩余 {free / 1024 ** 2:.0f} MB",
            hint="请清理磁盘或更换模型缓存目录后重试。",
        )


def _download_modelscope(repo_id: str, agg: _Aggregator, ignore: list[str]):
    from modelscope.hub.snapshot_download import snapshot_download
    from modelscope.hub.callback import ProgressCallback

    agg_ref = agg

    class _Callback(ProgressCallback):  # type: ignore[misc]
        def __init__(self, filename, file_size):
            super().__init__(filename, file_size)
            agg_ref.start_file(filename, file_size)

        def update(self, size):
            agg_ref.add(size, self.filename)

        def end(self):
            agg_ref.finish_file(self.filename)

    return snapshot_download(
        repo_id,
        ignore_patterns=ignore,
        progress_callbacks=[_Callback],
        max_workers=4,
    )


def _download_hf(repo_id: str, agg: _Aggregator, ignore: list[str]):
    from huggingface_hub import snapshot_download
    from tqdm.auto import tqdm

    agg_ref = agg

    class _Bar(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("disable", True)
            super().__init__(*args, **kwargs)
            agg_ref.start_file(str(kwargs.get("desc") or repo_id),
                               int(kwargs.get("total") or 0))

        def update(self, n=1):
            try:
                agg_ref.add(int(n), str(getattr(self, "desc", "") or ""))
            except Exception:
                pass
            return super().update(n)

    return snapshot_download(
        repo_id,
        ignore_patterns=ignore,
        tqdm_class=_Bar,
        max_workers=4,
    )


def download_bundle(
    bundle: ModelBundle,
    hub: str = "ms",
    on_progress: Optional[Callable[[DownloadProgress], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """下载一个模型包，返回结果字典（不会抛异常）。"""
    log = on_log or (lambda _m: None)
    repos = bundle.repos(hub)
    if not repos:
        return {"ok": False, "message": "模型包为空", "hint": ""}

    log(f"模型包：{bundle.name}（{len(repos)} 个仓库，源：{hub}）")

    # 1) 预估总量（失败则退化为未知进度）
    total = 0
    for repo in repos:
        sizes = repo_file_sizes(repo, hub)
        if not sizes:
            total = 0
            break
        total += sum(sizes.values())
    if total:
        log(f"需要下载约 {total / 1024 ** 2:.0f} MB")

    agg = _Aggregator(total, on_progress, len(repos), should_cancel)
    agg.begin_repo("", 0)

    # 2) 磁盘检查
    try:
        _ensure_disk_space(total, modelscope_cache_roots()[0] if hub == "ms" else hf_cache_roots()[0])
    except DownloadFailed as e:
        return {"ok": False, "message": str(e), "hint": e.hint}

    # 3) 逐仓库下载
    for idx, repo in enumerate(repos, start=1):
        if should_cancel and should_cancel():
            return {"ok": False, "message": "已取消", "cancelled": True, "hint": ""}
        if repo_installed(hub, repo):
            log(f"[{idx}/{len(repos)}] {repo} 已存在，跳过")
            # 已存在的仓库不再计入进度，直接补齐总量
            continue
        agg.begin_repo(repo, idx)
        log(f"[{idx}/{len(repos)}] 开始下载 {repo}")
        t0 = time.monotonic()
        try:
            if hub == "hf":
                _download_hf(repo, agg, DEFAULT_IGNORE)
            else:
                _download_modelscope(repo, agg, DEFAULT_IGNORE)
        except DownloadCancelled:
            return {"ok": False, "message": "已取消", "cancelled": True, "hint": ""}
        except BaseException as e:  # noqa: BLE001 - 任何异常都要转成可读信息
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            log(f"下载 {repo} 失败：{type(e).__name__}: {e}")
            log(traceback.format_exc(limit=3))
            failed = _classify_error(e, hub)
            return {"ok": False, "message": failed.args[0],
                    "hint": failed.hint, "repo": repo}
        finally:
            _ = t0

        # 校验落地
        if not repo_installed(hub, repo):
            return {"ok": False,
                    "message": f"{repo} 下载结束但未找到权重文件",
                    "hint": "可能是缓存目录权限问题，或下载被安全软件拦截。"}
        log(f"[{idx}/{len(repos)}] {repo} 完成")

    if should_cancel and should_cancel():
        return {"ok": False, "message": "已取消", "cancelled": True, "hint": ""}

    if on_progress:
        try:
            on_progress(DownloadProgress(percent=100, done=total or agg.done,
                                         total=total or agg.done, message="完成"))
        except Exception:
            pass
    return {"ok": True, "message": "下载完成", "hint": ""}


# ─────────────────────────── 展示辅助 ───────────────────────────


def human_size(num: float) -> str:
    if num <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def bundle_by_key(key: str) -> Optional[ModelBundle]:
    return MODEL_BUNDLES.get(key)
