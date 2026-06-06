import os
import sys
import subprocess
import json
from typing import Callable

MIRROR_SOURCES = {
    "modelscope (魔搭, 国内推荐)": "ms",
    "huggingface (海外)": "hf",
    "阿里云 (国内)": "ms",
}

MODELS_TO_DOWNLOAD = [
    {
        "name": "SenseVoiceSmall",
        "model_id": "iic/SenseVoiceSmall",
        "size": "~500MB",
        "description": "多语言 ASR + 情感识别",
    },
    {
        "name": "Paraformer (中文)",
        "model_id": "paraformer-zh",
        "size": "~800MB",
        "description": "中文精准识别",
    },
    {
        "name": "Fun-ASR-Nano (多语言)",
        "model_id": "FunAudioLLM/Fun-ASR-Nano-2512",
        "size": "~1.2GB",
        "description": "31 种语言，轻量端到端",
    },
]


def download_model(model_name: str, mirror: str = "ms",
                   progress_callback: Callable[[str, int], None] = None):
    """Download model using FunASR's built-in mechanism."""
    from funasr import AutoModel

    cfg = {"model": model_name, "hub": mirror}

    if progress_callback:
        progress_callback(f"开始下载: {model_name} (源: {mirror})", 10)

    try:
        AutoModel(**cfg)
        if progress_callback:
            progress_callback(f"下载完成: {model_name}", 100)
        return True
    except Exception as e:
        if progress_callback:
            progress_callback(f"下载失败: {str(e)}", -1)
        raise


def check_model_installed(model_id: str) -> bool:
    """Check if model is already cached locally."""
    import os
    home = os.path.expanduser("~")
    cache_paths = [
        os.path.join(home, ".cache", "modelscope", "hub", model_id.replace("/", "_")),
        os.path.join(home, ".cache", "huggingface", "hub",
                     "models--" + model_id.replace("/", "--")),
    ]
    for p in cache_paths:
        if os.path.isdir(p):
            return True
    return False


def list_installed_models() -> list[dict]:
    result = []
    for m in MODELS_TO_DOWNLOAD:
        result.append({
            **m,
            "installed": check_model_installed(m["model_id"]),
        })
    return result
