"""FunASR 推理封装。

* funasr / torch 全部延迟导入，避免程序启动时卡住几秒；
* 模型配置与 :mod:`core.downloader` 的模型包一一对应，便于下载前校验；
* 加载失败时给出可读原因（缺模型 / 显存不足 / 设备不支持）。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger("FunASR")

MODEL_REGISTRY: dict[str, dict] = {
    "SenseVoiceSmall": {
        "bundle": "sensevoice",
        "model": "iic/SenseVoiceSmall",
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
        "spk_model": "cam++",
    },
    "Paraformer (中文)": {
        "bundle": "paraformer",
        "model": "paraformer-zh",
        "vad_model": "fsmn-vad",
        "punc_model": "ct-punc",
        "spk_model": "cam++",
    },
    "Fun-ASR-Nano (多语言)": {
        "bundle": "funasr-nano",
        "model": "FunAudioLLM/Fun-ASR-Nano-2512",
        "trust_remote_code": True,
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
        "spk_model": "cam++",
    },
}

_model_instance = None
_model_key = None


class ModelLoadError(RuntimeError):
    """加载模型失败，message 已本地化，hint 是可执行建议。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def bundle_key_for(model_name: str) -> str:
    cfg = MODEL_REGISTRY.get(model_name, MODEL_REGISTRY["SenseVoiceSmall"])
    return cfg.get("bundle", "sensevoice")


def _friendly_error(exc: BaseException) -> ModelLoadError:
    text = str(exc) or type(exc).__name__
    low = text.lower()

    if "out of memory" in low or "cuda error" in low and "memory" in low:
        return ModelLoadError(
            "显存不足，模型无法加载",
            "请改用更小的模型（SenseVoiceSmall）、降低 batch size，或切换到 CPU 推理。")
    if "not compiled with cuda" in low or "torch not compiled" in low:
        return ModelLoadError(
            "当前 PyTorch 为 CPU 版本，无法使用 GPU",
            "请到“硬件”页安装对应显卡的 PyTorch 版本，或把设备切换为 CPU。")
    if "no kernel image" in low or "device-side assert" in low:
        return ModelLoadError(
            "显卡算力与当前 PyTorch 版本不匹配",
            "请安装与显卡架构匹配的 PyTorch（新显卡建议 CUDA 12.6+ 版本）。")
    if "connection" in low or "timed out" in low or "download" in low:
        return ModelLoadError(
            "模型文件缺失或下载失败",
            "请到“模型”页确认三个模型包均已下载完成后再试。")
    return ModelLoadError(f"模型加载失败：{text[:300]}",
                          "可查看日志页获取完整堆栈信息。")


def load_model(model_name: str, device: str, log_callback=None):
    global _model_instance, _model_key

    key = f"{model_name}|{device}"
    if _model_key == key and _model_instance is not None:
        return _model_instance

    cfg = dict(MODEL_REGISTRY.get(model_name, MODEL_REGISTRY["SenseVoiceSmall"]))
    cfg.pop("bundle", None)

    def log(msg: str):
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    log(f"加载模型：{model_name}（设备 {device}）")
    start = time.time()
    try:
        from funasr import AutoModel  # 延迟导入，避免拖慢启动
    except Exception as e:
        raise ModelLoadError(
            "FunASR 未正确安装", "请执行 pip install -U funasr 后重试。") from e

    try:
        _model_instance = AutoModel(device=device, **cfg)
    except Exception as e:
        raise _friendly_error(e) from e

    _model_key = key
    log(f"模型加载完成（{time.time() - start:.1f}s）")
    return _model_instance


def unload_model():
    """释放已加载模型（切换设备或更换模型时调用）。"""
    global _model_instance, _model_key
    _model_instance = None
    _model_key = None
    try:
        import gc
        gc.collect()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _postprocess_text(text: str, model_name: str) -> str:
    """去掉 SenseVoice 输出里的 <|zh|><|EMO_..|> 等富文本标记。"""
    if not text:
        return text
    if "SenseVoice" not in model_name:
        return text
    try:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        return rich_transcription_postprocess(text)
    except Exception:
        import re
        return re.sub(r"<\|[^|]*\|>", "", text).strip()


def transcribe(
    audio_path: str,
    model_name: str,
    device: str,
    batch_size_s: int = 300,
    batch_size: int = 1,
    max_speakers: int = 0,
    vad_max_seg_sec: int = 30,
    log_callback=None,
) -> list[dict]:
    def log(msg: str):
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    model = load_model(model_name, device, log_callback)
    log("开始识别（ASR + 说话人分离）")

    kwargs = {
        "input": audio_path,
        "batch_size_s": batch_size_s,
        "batch_size": batch_size,
        # 关闭 tqdm 进度条，界面自己显示进度
        "disable_pbar": True,
    }
    if max_speakers > 0:
        kwargs["max_speakers"] = max_speakers

    cfg = MODEL_REGISTRY.get(model_name, MODEL_REGISTRY["SenseVoiceSmall"])
    vad_kw = dict(cfg.get("vad_kwargs", {}))
    if vad_max_seg_sec > 0:
        vad_kw["max_single_segment_time"] = vad_max_seg_sec * 1000
    if vad_kw:
        kwargs["vad_kwargs"] = vad_kw

    start = time.time()
    res = model.generate(**kwargs)
    log(f"识别完成（{time.time() - start:.1f}s）")

    if not res:
        return []

    first = res[0]
    sentences = first.get("sentence_info", []) if isinstance(first, dict) else []
    if not sentences:
        text = first.get("text", "") if isinstance(first, dict) else ""
        text = _postprocess_text(text, model_name).strip()
        return [{"spk": "?", "start_ms": 0, "end_ms": 0, "text": text}] if text else []

    result = []
    for s in sentences:
        text = s.get("text", s.get("sentence", "")) or ""
        result.append({
            "spk": s.get("spk", "?"),
            "start_ms": s.get("start", 0),
            "end_ms": s.get("end", 0),
            "text": _postprocess_text(text, model_name).strip(),
        })
    return result
