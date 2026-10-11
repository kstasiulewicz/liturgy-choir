"""Slice a Divine Liturgy recording into per-part clips for the choir site.

Usage:
  python process.py transcribe <audio>      # whisper -> work/<name>/transcript.json (slow, cached)
  python process.py align <audio>           # transcript + silences -> work/<name>/cuts.json
  python process.py slice <audio> [--date YYYY-MM-DD] [--tone N]
                                            # cuts.json -> site/audio/*.mp3 + site/clips.json
  python process.py all <audio> [...]       # all three

Between align and slice, run `python review.py <audio>` to fix boundaries by ear.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).parent
SITE = ROOT / "site"
SNAP_WINDOW = 8.0      # seconds to search for a pause near a cue
MIN_SCORE = 78         # fuzzy cue match threshold (0-100)


def workdir(audio):
    d = ROOT / "work" / Path(audio).stem
    d.mkdir(parents=True, exist_ok=True)
    return d


def norm(s):
    return re.sub(r"[^a-z ]+", "", s.lower().replace("-", " ")).split()


# ---------- transcribe ----------
def transcribe(audio, model_name="medium.en"):
    from faster_whisper import WhisperModel
    out = workdir(audio) / "transcript.json"
    if out.exists():
        print(f"cached: {out}")
        return
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    # decode with ffmpeg ourselves (16 kHz mono float) - avoids PyAV version issues
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(audio),
                          "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    samples = np.frombuffer(pcm, dtype=np.float32)
    # No VAD: chant is sustained singing and VAD tends to drop it.
    segments, info = model.transcribe(
        samples, language="en", word_timestamps=True, vad_filter=False,
        condition_on_previous_text=False,
        initial_prompt="Divine Liturgy. Lord have mercy. Theotokos. Alleluia. Holy God, holy and mighty.",
    )
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)})
        print(f"\r{seg.end/60:6.1f} / {info.duration/60:.1f} min", end="", flush=True)
    print()
    out.write_text(json.dumps({"duration": info.duration, "words": words}))
    print(f"wrote {out} ({len(words)} words)")


# ---------- silences ----------
def silences(audio, noise_db=-35, min_dur=0.8):
    cache = workdir(audio) / "silences.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio), "-af",
         f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"],
        capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    sil = [[s, e] for s, e in zip(starts, ends)]
    cache.write_text(json.dumps(sil))
    return sil


def boundary(t, sil, lo=0.0):
    """Pick the pause that separates two parts near cue time t -> (prev_end, next_start).
    Whisper often stamps the first word after a silence early, so look a little ahead too."""
    cands = [(e - s, s, e) for s, e in sil if max(t - SNAP_WINDOW, lo) < s and e <= t + 4]
    if not cands:
        return t, max(0.0, t - 0.5)
    _, s, e = max(cands)
    return s + 0.6, e - 0.3


# ---------- align ----------
def align(audio):
    wd = workdir(audio)
    tr = json.loads((wd / "transcript.json").read_text(encoding="utf-8"))
    words = tr["words"]
    toks = [(norm(w["w"]) or [""])[0] for w in words]
    sil = silences(audio)
    lit = json.loads((ROOT / "liturgy.json").read_text(encoding="utf-8"))

    parts = [dict(p, group=g["group"]) for g in lit["sections"] for p in g["parts"]]

    # 1) candidate placements for every part: local fuzzy-match peaks of any cue
    cands = []
    for p in parts:
        hits = []
        for cue in p["cues"]:
            c = " ".join(norm(cue))
            n = len(c.split())
            row = [fuzz.ratio(c, " ".join(toks[i:i + n])) for i in range(len(toks) - n + 1)]
            for i, sc in enumerate(row):
                if sc >= MIN_SCORE and sc >= max(row[max(0, i - 3):i + 4]):
                    # long distinctive cues outweigh short generic ones ("alleluia", "lord have mercy")
                    hits.append((i, n, sc, n * sc / 100))
        hits.sort(key=lambda h: -h[3])
        cands.append(sorted(hits[:40]))

    # 2) pick one placement (or none) per part so the order matches the service, maximizing total weight
    best = []  # best[j][k] = (total, back-pointer)
    for j, hs in enumerate(cands):
        row = []
        for (i, n, sc, w) in hs:
            prev = (0, None)
            for jj in range(j):
                for kk, (i2, n2, _, _) in enumerate(cands[jj]):
                    if i2 + n2 <= i and best[jj][kk][0] > prev[0]:
                        prev = (best[jj][kk][0], (jj, kk))
            row.append((prev[0] + w, prev[1]))
        best.append(row)
    end_at = max(((t, (j, k)) for j, row in enumerate(best) for k, (t, _) in enumerate(row)), default=(0, None))[1]
    chosen = {}
    while end_at:
        j, k = end_at
        chosen[j] = cands[j][k]
        end_at = best[j][k][1]

    # 3) a part starts at its earliest cue hit after the previous part (e.g. "God grant" before "many years")
    order = sorted(chosen)
    for a, j in enumerate(order):
        i, n, sc, w = chosen[j]
        lo = chosen[order[a - 1]][0] + chosen[order[a - 1]][1] if a else 0
        earlier = [h for h in cands[j] if lo <= h[0] < i]
        if earlier:
            chosen[j] = (min(earlier)[0], n, sc, w)

    found = []
    for j, p in enumerate(parts):
        if j in chosen:
            i, n, sc, _ = chosen[j]
            found.append({"part": p, "i": i, "score": sc})
            print(f"  ok  {p['name']}: {words[i]['s']/60:5.1f} min  (match {sc:.0f})")
        else:
            print(f"  --  {p['name']}: not found")

    cuts = []
    for k, f in enumerate(found):
        t = words[f["i"]]["s"]
        prev_t = words[found[k - 1]["i"]]["e"] if k else 0.0
        start = boundary(t, sil, prev_t)[1]
        if k + 1 < len(found):
            end = boundary(words[found[k + 1]["i"]]["s"], sil, words[f["i"]]["e"])[0]
        else:
            end = tr["duration"]
        if k == 0 and t < 10:
            start = 0.0
        if cuts:
            start = max(start, cuts[-1]["end"])
        text = " ".join(w["w"] for w in words if start <= w["s"] < end)
        p = f["part"]
        cuts.append({"id": p["id"], "name": p["name"], "group": p["group"],
                     "variable": p.get("variable", False),
                     "start": round(start, 2), "end": round(end, 2),
                     "score": f["score"], "text": text[:400]})
    (wd / "cuts.json").write_text(json.dumps(cuts, indent=1))
    missing = [p["name"] for p in parts if p["id"] not in {c["id"] for c in cuts}]
    print(f"\n{len(cuts)} parts located, {len(missing)} not found -> {wd/'cuts.json'}")


# ---------- slice ----------
def slice_(audio, date=None, tone=None):
    wd = workdir(audio)
    cuts = json.loads((wd / "cuts.json").read_text(encoding="utf-8"))
    rec = date or Path(audio).stem
    adir = SITE / "audio" / rec
    adir.mkdir(parents=True, exist_ok=True)
    clips_path = SITE / "clips.json"
    data = json.loads(clips_path.read_text(encoding="utf-8")) if clips_path.exists() else {"recordings": [], "clips": []}
    # keep any names/tones/notes the choir already edited for this recording
    old = {c["id"]: c for c in data["clips"] if c["recording"] == rec}
    data["clips"] = [c for c in data["clips"] if c["recording"] != rec]
    if not any(r["id"] == rec for r in data["recordings"]):
        data["recordings"].append({"id": rec, "label": rec, "tone": tone})

    for n, c in enumerate(cuts, 1):
        fname = f"{n:02d}-{c['id']}.mp3"
        dur = c["end"] - c["start"]
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", str(c["start"]), "-t", str(dur), "-i", str(audio),
             "-af", f"afade=t=in:d=0.3,afade=t=out:st={max(0, dur-0.8)}:d=0.8,loudnorm=I=-18:TP=-1.5",
             "-ac", "1", "-b:a", "96k", str(adir / fname)], check=True)
        prev = old.get(f"{rec}/{c['id']}", {})
        data["clips"].append({
            "id": f"{rec}/{c['id']}", "part": c["id"], "recording": rec,
            "name": prev.get("name", c["name"]), "group": c["group"],
            "tone": prev.get("tone", tone if c["variable"] else None),
            "variable": c["variable"], "notes": prev.get("notes", ""),
            "file": f"audio/{rec}/{fname}", "duration": round(dur, 1),
        })
        print(f"  {fname}  {dur:5.0f}s")
    clips_path.write_text(json.dumps(data, indent=1))
    print(f"wrote {len(cuts)} clips -> {adir}")


if __name__ == "__main__":
    cmd, audio, *rest = sys.argv[1:]
    opts = dict(zip(rest[::2], rest[1::2]))
    if cmd in ("transcribe", "all"):
        transcribe(audio, opts.get("--model", "medium.en"))
    if cmd in ("align", "all"):
        align(audio)
    if cmd in ("slice", "all"):
        slice_(audio, opts.get("--date"), opts.get("--tone"))
