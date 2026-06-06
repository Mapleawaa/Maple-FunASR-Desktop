import json
import os
import subprocess
import tempfile


def export_txt(sentences: list[dict], path: str):
    lines = []
    for s in sentences:
        start = s["start_ms"] / 1000
        end = s["end_ms"] / 1000
        lines.append(f"[Speaker {s['spk']}] [{start:.1f}s-{end:.1f}s] {s['text']}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(lines))


def export_json(sentences: list[dict], path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sentences, f, ensure_ascii=False, indent=2)


def export_srt(sentences: list[dict], path: str):
    def ms_to_srt(ms: int) -> str:
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(path, "w", encoding="utf-8") as f:
        for i, s in enumerate(sentences, 1):
            f.write(f"{i}\n")
            f.write(f"{ms_to_srt(s['start_ms'])} --> {ms_to_srt(s['end_ms'])}\n")
            f.write(f"Speaker {s['spk']}: {s['text']}\n\n")


def export_csv(sentences: list[dict], path: str):
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("Speaker,Start(ms),End(ms),Text\n")
        for s in sentences:
            f.write(f"{s['spk']},{s['start_ms']},{s['end_ms']},{s['text']}\n")


def export_md(sentences: list[dict], path: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write("| Speaker | Start | End | Text |\n")
        f.write("|---------|-------|-----|------|\n")
        for s in sentences:
            start = f"{s['start_ms']/1000:.1f}s"
            end = f"{s['end_ms']/1000:.1f}s"
            f.write(f"| {s['spk']} | {start} | {end} | {s['text']} |\n")


def export_docx(sentences: list[dict], path: str):
    try:
        subprocess.run(["pandoc", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        raise RuntimeError(
            "导出 DOCX 需要安装 pandoc。\n"
            "请从 https://pandoc.org/installing.html 下载安装"
        )

    md_path = tempfile.NamedTemporaryFile(suffix=".md", delete=False).name
    try:
        export_md(sentences, md_path)
        subprocess.run(
            ["pandoc", md_path, "-o", path, "--from", "markdown", "--to", "docx"],
            check=True,
        )
    finally:
        if os.path.exists(md_path):
            os.unlink(md_path)


EXPORT_MAP = {
    "TXT": export_txt,
    "JSON": export_json,
    "SRT": export_srt,
    "CSV": export_csv,
    "MD": export_md,
    "DOCX": export_docx,
}


def export_result(sentences: list[dict], fmt: str, path: str):
    fn = EXPORT_MAP.get(fmt.upper())
    if not fn:
        raise ValueError(f"不支持的导出格式: {fmt}")
    fn(sentences, path)
