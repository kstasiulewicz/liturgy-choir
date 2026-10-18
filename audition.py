"""Cleanup audition: render excerpts through each CLEAN preset and open a sync-switching page.

  python audition.py <audio> "Label@mm:ss+secs" ["Label@mm:ss+secs" ...]   -> docs/cleanup/
"""
import json
import subprocess
import sys
from pathlib import Path

import process as p

NAMES = {"a": "A · Original (level only)", "b": "B · Hum removed",
         "c": "C · Hum + light hiss reduction", "d": "D · Stronger cleanup + evened dynamics"}

audio = Path(sys.argv[1])
out = p.SITE / "cleanup"  # published, so the choir director can listen from a link
out.mkdir(parents=True, exist_ok=True)
excerpts = []
for n, spec in enumerate(sys.argv[2:]):
    label, _, rest = spec.partition("@")
    at, _, secs = rest.partition("+")
    m, s = at.split(":")
    start, dur = int(m) * 60 + float(s), float(secs or 30)
    files = {}
    for k, chain in p.CLEAN.items():
        f = f"{audio.stem}-{n}-{k}.mp3"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-t", str(dur),
                        "-i", str(audio), "-af", f"{chain},{p.LEVEL},loudnorm=I=-18:TP=-1.5:LRA=11,{p.LIMIT}",
                        "-ac", "1", "-ar", "48000", "-c:a", "libmp3lame", "-q:a", "4", str(out / f)], check=True)
        files[k] = f
    excerpts.append({"label": label, "at": at, "files": files})
    print("rendered", label)

page = (p.ROOT / "audition.html").read_text(encoding="utf-8")
page = page.replace("/*DATA*/", json.dumps({"names": NAMES, "excerpts": excerpts}))
(out / "index.html").write_text(page, encoding="utf-8")
print(out / "index.html")
