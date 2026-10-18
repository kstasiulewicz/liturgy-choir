"""Audio QC and cleanup — the Audacity checks, measured instead of eyeballed.

  python audio.py analyze <audio>          # report + work/<stem>/qc.json
  python audio.py clean <audio> <out> --preset c [--start S --dur D]
                                           # cleaned mono WAV (used by export and audition)

Checks: loudness/true peak/LRA (EBU R128), clipping, clicks, DC offset, dropouts, noise floor + SNR,
mains hum (exact frequency + harmonics), tonal balance (mud / presence / air), stereo agreement.
Cleanup is driven by qc.json: hum notches sit on the measured hum frequency, click repair only runs
if clicks were found, and noise reduction learns its noise profile from the recording's own quietest
moments (spectral gating, like Audacity's Noise Reduction "Get Noise Profile").
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy import signal

ROOT = Path(__file__).parent
SR = 48000
CHUNK = 60  # seconds per analysis chunk


def workdir(audio):
    d = ROOT / "work" / Path(audio).stem
    d.mkdir(parents=True, exist_ok=True)
    return d


def stream(audio, sr=SR, channels=2, start=None, dur=None):
    """Yield (t0, float32 array [n, channels]) chunks decoded by ffmpeg."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", str(start)]
    if dur is not None:
        cmd += ["-t", str(dur)]
    cmd += ["-i", str(audio), "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    step = sr * CHUNK * channels * 4
    t0 = 0.0
    while True:
        buf = p.stdout.read(step)
        if not buf:
            break
        x = np.frombuffer(buf[: len(buf) // (4 * channels) * 4 * channels], np.float32).reshape(-1, channels)
        yield t0, x
        t0 += len(x) / sr
    p.wait()


def mmss(t):
    return f"{int(t // 60)}:{int(t % 60):02d}"


def db(x):
    return float(20 * np.log10(max(float(x), 1e-12)))


# ---------------------------------------------------------------- analyze
def analyze(audio, verbose=True):
    clips, clicks, drops = [], [], []
    peak = 0.0
    dc = np.zeros(2)
    n_total = 0
    sLL = sRR = sLR = 0.0
    rms_blocks, block_t = [], []
    quiet_pool, loud_pool = [], []
    half = SR // 2

    for t0, x in stream(audio):
        n = len(x)
        n_total += n
        dc += x.sum(0)
        peak = max(peak, float(np.abs(x).max()))
        L, R = x[:, 0].astype(np.float64), x[:, 1].astype(np.float64)
        sLL += (L * L).sum(); sRR += (R * R).sum(); sLR += (L * R).sum()
        m = x.mean(1)

        # clipping: 3+ consecutive samples at/above 0.999 full scale
        hot = np.abs(x).max(1) >= 0.999
        if hot.any():
            edges = np.flatnonzero(np.diff(np.r_[0, hot.astype(np.int8), 0]))
            for a, b in zip(edges[::2], edges[1::2]):
                if b - a >= 3:
                    clips.append(round(t0 + a / SR, 2))

        # dropouts: 10 ms+ of exact digital zero
        z = (x == 0).all(1)
        if z.any():
            edges = np.flatnonzero(np.diff(np.r_[0, z.astype(np.int8), 0]))
            for a, b in zip(edges[::2], edges[1::2]):
                if b - a >= SR // 100:
                    drops.append(round(t0 + a / SR, 2))

        # clicks: isolated spikes in the 2nd difference, far above the local level
        d2 = np.abs(np.diff(m, 2))
        for k in range(0, len(d2) - SR + 1, SR):
            blk = d2[k:k + SR]
            mad = np.median(blk) + 1e-9
            i = int(blk.argmax())
            if blk[i] > 40 * mad and blk[i] > 0.05:
                clicks.append(round(t0 + (k + i) / SR, 2))

        # half-second RMS blocks, keep samples of quiet/loud blocks for spectra
        nb = n // half
        if nb:
            b = m[: nb * half].reshape(nb, half)
            r = np.sqrt((b.astype(np.float64) ** 2).mean(1))
            rms_blocks.extend(r.tolist())
            block_t.extend((t0 + np.arange(nb) * 0.5).tolist())
            order = np.argsort(r)
            quiet_pool.extend(b[i] for i in order[:3])
            loud_pool.extend(b[i] for i in order[-6:])

    dur = n_total / SR
    rms = np.array(rms_blocks)
    rms_db = 20 * np.log10(rms + 1e-12)
    floor_db = float(np.percentile(rms_db, 5))
    signal_db = float(np.percentile(rms_db, 95))
    corr = sLR / np.sqrt(sLL * sRR + 1e-20)
    bal = 10 * np.log10((sLL + 1e-20) / (sRR + 1e-20))

    # quietest moments overall -> noise spectrum + exact hum frequency
    q_order = np.argsort(rms)[:120]
    quiet_t = sorted(block_t[i] for i in q_order)
    quiet = np.concatenate(sorted(quiet_pool, key=lambda b: float(np.sqrt((b ** 2).mean())))[:120])
    loud = np.concatenate(loud_pool)

    f, Pq = signal.welch(quiet, SR, nperseg=min(len(quiet), 1 << 17))  # ~0.37 Hz bins
    hum = None
    for base in (60.0, 50.0):
        sel = (f > base - 2) & (f < base + 2)
        if sel.any():
            i = np.flatnonzero(sel)[Pq[sel].argmax()]
            # parabolic interpolation for sub-bin accuracy
            if 0 < i < len(Pq) - 1:
                a, b, c = np.log(Pq[i - 1:i + 2] + 1e-30)
                off = 0.5 * (a - c) / (a - 2 * b + c)
            else:
                off = 0
            f0 = float(f[i] + off * (f[1] - f[0]))
            bg = np.median(Pq[(f > base - 15) & (f < base + 15)])
            prom = 10 * np.log10(Pq[i] / bg)
            if prom > 6 and (hum is None or prom > hum["prominence_db"]):
                harm = []
                for k in range(1, 9):
                    s2 = (f > k * f0 - 1.5) & (f < k * f0 + 1.5)
                    bg2 = np.median(Pq[(f > k * f0 - 20) & (f < k * f0 + 20)])
                    p2 = 10 * np.log10(Pq[s2].max() / bg2) if s2.any() else 0
                    if p2 > 6:
                        harm.append(k)
                hum = {"f0": round(f0, 2), "prominence_db": round(float(prom), 1), "harmonics": harm}

    # hum drifts with fan/motor speed: track its peak through the whole service, use the median + spread
    if hum:
        peaks = []
        for t in np.linspace(30, max(31, dur - 30), 24):
            for _, x in stream(audio, 8000, 1, start=float(t), dur=20):
                fx, Px = signal.welch(x[:, 0], 8000, nperseg=min(len(x), 1 << 15))
                s = (fx > hum["f0"] - 3) & (fx < hum["f0"] + 3)
                if s.any():
                    peaks.append(float(fx[s][Px[s].argmax()]))
        if peaks:
            lo, hi = np.percentile(peaks, [10, 90])
            hum.update(f0=round(float(np.median(peaks)), 2), drift_hz=[round(float(lo), 2), round(float(hi), 2)])

    # tonal balance of the singing (loudest moments), as share of energy per band
    fl, Pl = signal.welch(loud, SR, nperseg=8192)
    bands = {"rumble <60": (20, 60), "low 60-250": (60, 250), "mud 250-500": (250, 500),
             "mid 500-2k": (500, 2000), "presence 2-5k": (2000, 5000), "brilliance 5-10k": (5000, 10000),
             "air 10-16k": (10000, 16000), "top >16k": (16000, 24000)}
    tot = Pl[(fl >= 20)].sum()
    balance = {k: round(10 * np.log10(Pl[(fl >= a) & (fl < b)].sum() / tot + 1e-20), 1) for k, (a, b) in bands.items()}
    fq, Pqn = signal.welch(quiet, SR, nperseg=8192)
    totq = Pqn[(fq >= 20)].sum()
    noise_bal = {k: round(10 * np.log10(Pqn[(fq >= a) & (fq < b)].sum() / totq + 1e-20), 1) for k, (a, b) in bands.items()}

    # EBU R128 loudness + true peak
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio), "-af", "ebur128=peak=true:framelog=quiet",
                        "-f", "null", "-"], capture_output=True, text=True).stderr
    grab = lambda k: float(re.findall(k + r":\s+(-?[\d.]+)", r)[-1]) if re.findall(k + r":\s+(-?[\d.]+)", r) else None

    qc = {
        "file": str(audio), "duration_min": round(dur / 60, 1),
        "loudness_lufs": grab("I"), "loudness_range_lu": grab("LRA"), "true_peak_dbtp": grab("Peak"),
        "sample_peak_db": round(db(peak), 2),
        "clipping_events": len(clips), "clipping_at": [mmss(t) for t in clips[:20]],
        "clicks": len(clicks), "clicks_at": [mmss(t) for t in clicks[:20]],
        "dropouts": len(drops), "dropouts_at": [mmss(t) for t in drops[:20]],
        "dc_offset": [round(float(v), 5) for v in dc / max(n_total, 1)],
        "noise_floor_db": round(floor_db, 1), "singing_level_db": round(signal_db, 1),
        "snr_db": round(signal_db - floor_db, 1),
        "hum": hum,
        "tonal_balance_db": balance, "noise_balance_db": noise_bal,
        "stereo_correlation": round(float(corr), 3), "stereo_balance_db": round(float(bal), 1),
        "quiet_moments_s": [round(t, 1) for t in quiet_t[:60]],
    }
    (workdir(audio) / "qc.json").write_text(json.dumps(qc, indent=1, default=float), encoding="utf-8")
    if verbose:
        report(qc)
    return qc


def report(q):
    ok, warn = "ok  ", "WARN"
    lines = [f"Audio check: {Path(q['file']).name} ({q['duration_min']} min)"]
    add = lambda flag, text: lines.append(f"  {flag} {text}")
    add(ok if q["loudness_lufs"] and q["loudness_lufs"] > -30 else warn,
        f"Loudness {q['loudness_lufs']} LUFS, range {q['loudness_range_lu']} LU, true peak {q['true_peak_dbtp']} dBTP")
    add(ok if q["clipping_events"] <= 5 else warn,
        f"Clipping: {q['clipping_events']} events" + (f" at {', '.join(q['clipping_at'][:8])}" if q["clipping_events"] else ""))
    add(ok if q["clicks"] <= 10 else warn,
        f"Clicks/pops: {q['clicks']}" + (f" (e.g. {', '.join(q['clicks_at'][:6])})" if q["clicks"] else ""))
    add(ok if not q["dropouts"] else warn, f"Dropouts (digital silence): {q['dropouts']}")
    add(ok if max(abs(v) for v in q["dc_offset"]) < 0.005 else warn, f"DC offset: {q['dc_offset']}")
    add(ok if q["snr_db"] >= 25 else warn,
        f"Noise floor {q['noise_floor_db']} dB, singing {q['singing_level_db']} dB -> SNR {q['snr_db']} dB")
    h = q["hum"]
    add(warn if h else ok, f"Hum: {h['f0']} Hz (drifts {h.get('drift_hz')}), +{h['prominence_db']} dB above room noise"
        + (" — not mains (60.00 Hz): likely a fan/HVAC motor" if abs(h['f0'] - 60) > 0.3 and abs(h['f0'] - 50) > 0.3 else "")
        if h else "Mains hum: none detected")
    add(ok if q["stereo_correlation"] > 0.5 else warn,
        f"Stereo: correlation {q['stereo_correlation']}, L/R balance {q['stereo_balance_db']} dB "
        + ("(mono fold-down is safe)" if q["stereo_correlation"] > 0.5 else "(channels disagree - check mic)"))
    tb = q["tonal_balance_db"]
    lines.append("  Tonal balance of the singing (dB share of energy):")
    lines += [f"       {k:18s} {v:6.1f}" for k, v in tb.items()]
    print("\n".join(lines))


# ---------------------------------------------------------------- clean
LEVEL = "dynaudnorm=f=750:g=31:m=6:p=0.9"
LIMIT = "alimiter=limit=0.89:level=false"
# measured: these recordings are dark (2-5 kHz ~18 dB under the mids), so lift presence/air and trim mud
CRISP = "equalizer=f=320:t=o:w=1:g=-2,equalizer=f=3500:t=o:w=1.5:g=3,treble=f=9000:g=3:t=s"

PRESETS = {
    "a": {"label": "A · Original (level only)", "fix": False},
    "b": {"label": "B · Repair: tuned hum notches, rumble, clicks", "fix": True},
    "c": {"label": "C · B + light noise reduction (learned profile)", "fix": True, "nr": 0.5},
    "d": {"label": "D · C + crisp EQ (less mud, more presence/air)", "fix": True, "nr": 0.5, "eq": True},
    "e": {"label": "E · Strong noise reduction + crisp EQ", "fix": True, "nr": 0.8, "eq": True},
}


def repair_chain(qc):
    """ffmpeg filters for objective fixes, set from the measurements."""
    f = ["highpass=f=70:poles=2"]  # rumble + DC; stays under the lowest bass notes (~80 Hz)
    h = qc.get("hum")
    if h:
        f0 = h["f0"]
        lo, hi = h.get("drift_hz", [f0 - 0.3, f0 + 0.3])
        spread = max(0.6, hi - lo + 0.5)  # cover the measured drift at the fundamental
        for k in sorted(set([1, 2, 3, 4, 5] + h["harmonics"])):
            if k * f0 < 1000:
                f.append(f"bandreject=f={k * f0:.2f}:width_type=h:w={spread * k:.2f}")
    if qc.get("clicks", 0) > 10:
        f.insert(0, "adeclick=w=55:o=75")
    if qc.get("clipping_events", 0) > 20:
        # Audacity "Clip Fix": rebuild flattened peaks before anything else touches them
        f.insert(0, "adeclip=w=55:o=75")
    return f


def noise_profile(audio, qc, sr=SR):
    """Gather the recording's quietest half-seconds as the noise sample."""
    parts = []
    for t in qc["quiet_moments_s"][:40]:
        for _, x in stream(audio, sr, 1, start=t, dur=0.5):
            parts.append(x[:, 0])
    return np.concatenate(parts) if parts else None


def clean(audio, out, preset="b", start=None, dur=None, qc=None, level=True):
    """Write a cleaned 48 kHz mono WAV. Order: repair -> noise reduction -> EQ -> leveling -> limiter."""
    import noisereduce as nr
    qc = qc or json.loads((workdir(audio) / "qc.json").read_text(encoding="utf-8"))
    P = PRESETS[preset]
    pre = repair_chain(qc) if P["fix"] else ["anull"]
    post = ([CRISP] if P.get("eq") else []) + ([LEVEL] if level else []) + [LIMIT]

    # stage 1: repairs (ffmpeg) -> float mono
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", str(start)]
    if dur is not None:
        cmd += ["-t", str(dur)]
    cmd += ["-i", str(audio), "-af", ",".join(pre), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    enc = subprocess.Popen(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "f32le", "-ar", str(SR), "-ac", "1",
                            "-i", "-", "-af", ",".join(post), "-c:a", "pcm_s24le", str(out)], stdin=subprocess.PIPE)
    dec = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    # stage 2: spectral-gate noise reduction with a fixed learned profile, in overlapping chunks
    noise = None
    if P.get("nr"):
        noise = noise_profile(audio, qc)
        if noise is not None:
            from scipy.signal import sosfilt
            # run the profile through the same repair filters (approximately: high-pass) so it matches
            noise = sosfilt(signal.butter(2, 70, "hp", fs=SR, output="sos"), noise).astype(np.float32)
    step, ov = SR * 30, SR // 2
    fade = np.linspace(0, 1, ov, dtype=np.float32)
    carry = None
    while True:
        buf = dec.stdout.read(step * 4)
        if not buf:
            break
        x = np.frombuffer(buf, np.float32)
        if noise is not None:
            seg = x if carry is None else np.r_[carry[0], x]
            y = nr.reduce_noise(y=seg, sr=SR, y_noise=noise, stationary=True, prop_decrease=P["nr"], n_jobs=1).astype(np.float32)
            if carry is not None:
                y[:ov] = carry[1] * (1 - fade) + y[:ov] * fade
            if len(x) >= ov:
                carry = (x[-ov:], y[-ov:].copy())
                y = y[:-ov]
            else:
                carry = None
            enc.stdin.write(y.tobytes())
        else:
            enc.stdin.write(x.tobytes())
    if carry is not None:
        enc.stdin.write(carry[1].tobytes())
    enc.stdin.close()
    enc.wait()
    dec.wait()
    return out


if __name__ == "__main__":
    cmd, audio, *rest = sys.argv[1:]
    if cmd == "analyze":
        analyze(audio)
    elif cmd == "clean":
        out, *rest = rest
        o = dict(zip(rest[::2], rest[1::2]))
        clean(audio, out, o.get("--preset", "b"),
              float(o["--start"]) if "--start" in o else None, float(o["--dur"]) if "--dur" in o else None)
