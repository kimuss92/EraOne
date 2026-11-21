"""Turn a reference animation link (Pinterest, Instagram, YouTube...) into something a Motion
Design build can learn from: the video file, its technical facts and a contact sheet.

    python scripts/motion_reference.py <url> [--name slug]

Output goes to ~/.era/motion_refs/<slug>/ (a NEW folder per reference):
    ref.mp4       the downloaded video (untrusted: only ffprobe/ffmpeg ever read it)
    sheet.jpg     frames across the WHOLE video (about 16), to look at
    reference.md  link, title, duration, size, fps, audio, and where the sheet is

It does not describe what the video shows: a model with vision (the Claude Code session that runs
the motion-design skill) opens sheet.jpg and writes that. Inspiration only: never reuse another
brand's logo, name, text or footage.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME_DIR = Path.home() / ".era" / "motion_refs"


def _tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for base in (Path("C:/ffmpeg/bin"), Path(sys.executable).parent / "Scripts",
                 Path.home() / "AppData/Local/Python/pythoncore-3.14-64/Scripts"):
        for ext in (".exe", ""):
            p = base / f"{name}{ext}"
            if p.is_file():
                return str(p)
    return None


def _slug(url: str, name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or url).lower()).strip("-")[-48:] or "ref"


def fetch(url: str, name: str | None = None, home: Path = HOME_DIR) -> dict:
    """Download and analyse. Returns {"ok", "dir", "error"?, ...}. Never raises."""
    dest = home / _slug(url, name)
    try:
        ytdlp, ffmpeg, ffprobe = _tool("yt-dlp"), _tool("ffmpeg"), _tool("ffprobe")
        missing = [n for n, t in (("yt-dlp", ytdlp), ("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not t]
        if missing:
            return {"ok": False, "error": "missing tools: " + ", ".join(missing)}
        if dest.exists() and any(dest.iterdir()):
            dest = dest.with_name(dest.name + "-2")
        dest.mkdir(parents=True, exist_ok=True)
        run = subprocess.run([ytdlp, "--no-playlist", "-o", str(dest / "ref.%(ext)s"), url],
                             capture_output=True, text=True, timeout=300)
        video = next(iter(sorted(dest.glob("ref.*"))), None)
        if run.returncode != 0 or not video:
            return {"ok": False, "dir": str(dest), "error": (run.stderr or run.stdout)[-300:]}
        probe = json.loads(subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,width,height,r_frame_rate,duration",
             "-of", "json", str(video)], capture_output=True, text=True, timeout=60).stdout or "{}")
        v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
        has_audio = any(s.get("codec_type") == "audio" for s in probe.get("streams", []))
        dur = float(v.get("duration") or 0)
        fps = 16 / dur if dur else 1
        sheet = dest / "sheet.jpg"
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(video), "-vf",
                        f"fps={fps:.4f},scale=300:-1,tile=8x2", "-frames:v", "1", str(sheet)],
                       capture_output=True, timeout=120)
        facts = {"url": url, "file": str(video), "size": f"{v.get('width')}x{v.get('height')}",
                 "seconds": round(dur, 1), "fps": v.get("r_frame_rate"), "audio": has_audio,
                 "sheet": str(sheet) if sheet.is_file() else ""}
        (dest / "reference.md").write_text(
            "# Motion reference\n\n" + "\n".join(f"- {k}: {val}" for k, val in facts.items())
            + "\n\nThe sheet shows about 16 frames across the whole video, left to right, top to "
            "bottom. Look at it and write: the structure (shots, transitions), camera moves, pacing, "
            "text and logo placement. Inspiration only: never reuse another brand's logo, name, "
            "text or footage.\n", encoding="utf-8")
        return {"ok": True, "dir": str(dest), **facts}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dir": str(dest), "error": f"{type(exc).__name__}: {exc}"[:300]}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    name = sys.argv[sys.argv.index("--name") + 1] if "--name" in sys.argv else None
    out = fetch(sys.argv[1], name)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    raise SystemExit(0 if out.get("ok") else 1)
