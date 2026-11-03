#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка видео-презентации: слайды + озвучка → mp4 (1080p)."""
import json
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
ROOT = Path("video")
SLIDES = ROOT / "slides"
AUDIO = ROOT / "audio"
SEG = ROOT / "segments"
SEG.mkdir(exist_ok=True)

LEAD_IN = 0.45     # тишина перед репликой
TAIL = 0.75        # пауза после реплики
FPS = 30


def run(args):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-2500:])
    return r


def duration(path: Path) -> float:
    r = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
    if not m:
        raise RuntimeError(f"не удалось определить длительность {path}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


data = json.loads(Path("video/narration.json").read_text(encoding="utf-8"))
n = len(data["slides"])
segments, total = [], 0.0

for s in data["slides"]:
    i = s["n"]
    img = SLIDES / f"slide_{i:02d}.png"
    aud = AUDIO / f"s{i:02d}.mp3"
    dur = round(duration(aud) + LEAD_IN + TAIL, 2)
    out = SEG / f"seg_{i:02d}.mp4"
    filters = (
        f"[0:v]scale=1920:1080,setsar=1,fps={FPS},format=yuv420p,"
        f"fade=t=in:st=0:d=0.45,fade=t=out:st={max(dur - 0.45, 0):.2f}:d=0.45[v];"
        f"[1:a]aresample=48000,adelay={int(LEAD_IN * 1000)}:all=1,"
        f"apad=whole_dur={dur}[a]"
    )
    run([FFMPEG, "-y", "-loop", "1", "-i", str(img), "-i", str(aud),
         "-filter_complex", filters, "-map", "[v]", "-map", "[a]",
         "-t", f"{dur}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
         "-ac", "2", "-movflags", "+faststart", str(out)])
    segments.append(out)
    total += dur
    print(f"{i:02d}  {dur:5.2f} c  накоплено {total/60:.2f} мин")

lst = SEG / "list.txt"
lst.write_text("".join(f"file '{p.resolve()}'\n" for p in segments), encoding="utf-8")
final = "Видео_презентация_ИИ-агент_ПТО.mp4"
run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
     "-c", "copy", "-movflags", "+faststart", final])
print(f"\nготово: {final} · {total/60:.2f} мин · {Path(final).stat().st_size/1e6:.1f} МБ")
