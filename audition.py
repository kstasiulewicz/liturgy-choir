"""Cleanup audition: render excerpts through each CLEAN preset and open a sync-switching page.

  python audition.py <audio> "Label@mm:ss+secs" ["Label@mm:ss+secs" ...]   -> docs/cleanup/
"""
import json
import subprocess
import sys
from pathlib import Path

import process as p

import audio as A

NAMES = {k: v["label"] for k, v in A.PRESETS.items()}

audio = Path(sys.argv[1])
out = p.SITE / "cleanup"  # published, so the choir director can listen from a link
out.mkdir(parents=True, exist_ok=True)
excerpts = []
qc = json.loads((A.workdir(audio) / "qc.json").read_text(encoding="utf-8"))
for old in out.glob(f"{audio.stem}-*.mp3"):
    old.unlink()
for n, spec in enumerate(sys.argv[2:]):
    label, _, rest = spec.partition("@")
    at, _, secs = rest.partition("+")
    m, s = at.split(":")
    start, dur = int(m) * 60 + float(s), float(secs or 30)
    files = {}
    for k in A.PRESETS:
        f = f"{audio.stem}-{n}-{k}.mp3"
        wav = out / "tmp.wav"
        # same chain as the export, then volume-matched so only the cleanup differs
        A.clean(audio, wav, k, start=start, dur=dur, qc=qc)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
                        "-af", f"loudnorm=I=-18:TP=-1.5:LRA=11,{A.LIMIT}",
                        "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "96k", str(out / f)], check=True)
        wav.unlink()
        files[k] = f
    excerpts.append({"label": label, "at": at, "files": files})
    print("rendered", label)

page = (p.ROOT / "audition.html").read_text(encoding="utf-8")
page = page.replace("/*DATA*/", json.dumps({"names": NAMES, "excerpts": excerpts}))
(out / "index.html").write_text(page, encoding="utf-8")
print(out / "index.html")
