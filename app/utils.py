import os
import sys
import subprocess
import tempfile


def find_ffmpeg() -> str:
    paths = [
        os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"),
        os.path.join(os.path.dirname(__file__), "..", "ui", "resources", "ffmpeg.exe"),
        "ffmpeg.exe",
        "ffmpeg",
    ]
    for p in paths:
        abs_p = os.path.abspath(p)
        if os.path.isfile(abs_p):
            return abs_p
        try:
            subprocess.run([p, "-version"], capture_output=True, check=True)
            return p
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return "ffmpeg"


def safe_temp_path(suffix: str = ".wav") -> str:
    return tempfile.NamedTemporaryFile(suffix=suffix, delete=False).name


def format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def extract_audio(video_path: str, ffmpeg_path: str = "ffmpeg") -> str:
    audio_path = safe_temp_path(".wav")
    subprocess.run(
        [
            ffmpeg_path, "-i", video_path, "-vn",
            "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            audio_path, "-y",
        ],
        check=True, capture_output=True,
    )
    return audio_path


def get_media_duration(path: str, ffmpeg_path: str = "ffmpeg") -> float:
    r = subprocess.run(
        [ffmpeg_path, "-i", path],
        capture_output=True, text=True,
    )
    import re
    m = re.search(r"Duration: (\d+):(\d+):(\d+)\.(\d+)", r.stderr)
    if m:
        h, mi, s, ms = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        return h * 3600 + mi * 60 + s + ms / 100
    return 0.0
