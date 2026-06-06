import time
import logging
from funasr import AutoModel

logger = logging.getLogger("FunASR")

MODEL_REGISTRY = {
    "SenseVoiceSmall": {
        "model": "iic/SenseVoiceSmall",
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
        "spk_model": "cam++",
    },
    "Paraformer (中文)": {
        "model": "paraformer-zh",
        "vad_model": "fsmn-vad",
        "punc_model": "ct-punc",
        "spk_model": "cam++",
    },
    "Fun-ASR-Nano (多语言)": {
        "model": "FunAudioLLM/Fun-ASR-Nano-2512",
        "trust_remote_code": True,
        "remote_code": "./model.py",
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
        "spk_model": "cam++",
    },
}

_model_instance = None
_model_key = None


def load_model(model_name: str, device: str, log_callback=None):
    global _model_instance, _model_key

    if _model_key == f"{model_name}|{device}" and _model_instance is not None:
        return _model_instance

    cfg = dict(MODEL_REGISTRY.get(model_name, MODEL_REGISTRY["SenseVoiceSmall"]))

    if log_callback:
        log_callback(f"Loading model: {model_name}  (device: {device})")
    else:
        logger.info(f"Loading model: {model_name} on {device}")

    start = time.time()
    _model_instance = AutoModel(device=device, **cfg)
    _model_key = f"{model_name}|{device}"

    elapsed = time.time() - start
    if log_callback:
        log_callback(f"Model loaded ({elapsed:.1f}s)")
    else:
        logger.info(f"Model loaded in {elapsed:.1f}s")

    return _model_instance


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
    model = load_model(model_name, device, log_callback)

    if log_callback:
        log_callback("Start: ASR + speaker diarization")
    else:
        logger.info("Starting transcription")

    kwargs = {
        "input": audio_path,
        "batch_size_s": batch_size_s,
        "batch_size": batch_size,
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
    elapsed = time.time() - start

    if log_callback:
        log_callback(f"Inference done ({elapsed:.1f}s)")
    else:
        logger.info(f"Inference done in {elapsed:.1f}s")

    sentences = res[0].get("sentence_info", [])
    if not sentences:
        text = res[0].get("text", "")
        if text:
            return [{"spk": "?", "start_ms": 0, "end_ms": 0, "text": text}]
        return []

    result = []
    for s in sentences:
        result.append({
            "spk": s.get("spk", "?"),
            "start_ms": s.get("start", 0),
            "end_ms": s.get("end", 0),
            "text": s.get("text", s.get("sentence", "")).strip(),
        })

    return result
