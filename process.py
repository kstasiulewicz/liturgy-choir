"""Slice a Divine Liturgy recording into per-part clips for the choir site.

Usage:
  python process.py transcribe <audio>      # whisper -> work/<name>/transcript.json (slow, cached)
  python process.py align <audio>           # transcript + silences -> work/<name>/cuts.json
  python process.py export <audio> --date YYYY-MM-DD [--tone N] [--clean a|b|c|d]
                                            # cleaned full service -> docs/audio/<date>.mp3, parts -> docs/clips.json
  python process.py all <audio> --date ...  # all three

Boundaries are then fine-tuned on the live site (Edit mode).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).parent
SITE = ROOT / "docs"
SNAP_WINDOW = 8.0      # seconds to search for a pause near a cue
MIN_SCORE = 78         # fuzzy cue match threshold (0-100)
TAIL_GAP = 45.0        # a word-free gap this long after the last part = service is over

# Cleanup presets (ffmpeg filter chains), auditioned with audition.py.
# Room noise in these recordings is mains hum (60 Hz + harmonics) and rumble below ~50 Hz.
_HUM = "highpass=f=70:poles=2," + ",".join(f"bandreject=f={h}:width_type=q:w=30" for h in (60, 120, 180, 240, 300))
CLEAN = {
    "a": "anull",
    "b": _HUM,
    "c": _HUM + ",afftdn=nr=10:nf=-50:tn=1",
    "d": _HUM + ",afftdn=nr=20:nf=-50:tn=1,acompressor=threshold=-26dB:ratio=2:attack=20:release=400:makeup=2",
}
LIMIT = "alimiter=limit=0.89:level=false"


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
    import os
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
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
            # last part ends at the first long silence after it, not at the end of the file
            # (recorders keep running into after-service conversation, which stays off the site)
            tail = [w for w in words if w["s"] >= t]
            end = tail[-1]["e"] if tail else tr["duration"]
            for w1, w2 in zip(tail, tail[1:]):
                if w2["s"] - w1["e"] > TAIL_GAP:
                    end = w1["e"]
                    break
            end = min(tr["duration"], end + 2.0)
        if k == 0 and t < 10:
            start = 0.0
        if cuts:
            start = max(start, cuts[-1]["end"])
        text = " ".join(w["w"] for w in words if start <= w["s"] < end)
        p = f["part"]
        cuts.append({"id": p["id"], "name": p["name"], "group": p["group"],
                     "variable": p.get("variable", False), "spoken": p.get("spoken", False),
                     "start": round(start, 2), "end": round(end, 2),
                     "score": f["score"], "text": text[:400]})
    (wd / "cuts.json").write_text(json.dumps(cuts, indent=1))
    missing = [p["name"] for p in parts if p["id"] not in {c["id"] for c in cuts}]
    print(f"\n{len(cuts)} parts located, {len(missing)} not found -> {wd/'cuts.json'}")


# ---------- export ----------
# Whole-service leveling: brings quiet passages up without pumping (per-part loudnorm isn't possible
# now that parts are time ranges inside one file).
LEVEL = "dynaudnorm=f=750:g=31:m=6:p=0.9"


def export(audio, date, tone=None, clean="b"):
    """Render the cleaned full service to docs/audio/<date>.mp3 and add its parts to docs/clips.json.
    Re-running only re-renders the audio: part names/times edited on the site are kept."""
    rec = date
    (SITE / "audio").mkdir(parents=True, exist_ok=True)
    out = SITE / "audio" / f"{rec}.mp3"
    # stop just after the last labelled part: after-service conversation stays off the public site
    cuts = json.loads((workdir(audio) / "cuts.json").read_text(encoding="utf-8"))
    until = max(c["end"] for c in cuts) + 3
    # CBR so browsers can seek precisely to a part's start time
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-t", str(until), "-i", str(audio),
                    "-af", f"{CLEAN[clean]},{LEVEL},{LIMIT}",
                    "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "96k", str(out)], check=True)
    print(f"rendered {out} ({out.stat().st_size / 1e6:.0f} MB, cleanup {clean})")

    clips_path = SITE / "clips.json"
    data = json.loads(clips_path.read_text(encoding="utf-8")) if clips_path.exists() else {"recordings": [], "clips": []}
    r = next((x for x in data["recordings"] if x["id"] == rec), None)
    if r:
        r["clean"] = clean
        print("recording already on the site: kept its parts and labels")
    else:
        data["recordings"].append({"id": rec, "date": rec, "label": "", "tone": tone, "notes": "",
                                   "draft": True, "clean": clean, "file": f"audio/{rec}.mp3"})
        for c in cuts:
            data["clips"].append({
                "id": f"{rec}/{c['id']}", "part": c["id"], "recording": rec,
                "name": c["name"], "group": c["group"],
                "start": c["start"], "end": c["end"],
                "tone": tone if c["variable"] else None, "variable": c["variable"],
                "spoken": c.get("spoken", False), "notes": "",
            })
        print(f"added {len(cuts)} parts")
    data["recordings"].sort(key=lambda x: x["id"])
    clips_path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    cmd, audio, *rest = sys.argv[1:]
    opts = dict(zip(rest[::2], rest[1::2]))
    if cmd in ("transcribe", "all"):
        transcribe(audio, opts.get("--model", "medium.en"))
    if cmd in ("align", "all"):
        align(audio)
    if cmd in ("export", "all"):
        export(audio, opts["--date"], opts.get("--tone"), opts.get("--clean", "b"))
