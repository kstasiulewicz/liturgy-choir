# Liturgy Choir Practice

Slices a recorded Divine Liturgy into one clip per sung part and publishes a phone-friendly practice page for the choir.

## Adding a recording

1. Join the Zoom H1e's split WAVs for one date into `recordings/<YYYY-MM-DD>.flac` (it splits at 2 GB).
2. `.venv/Scripts/python process.py transcribe recordings/<date>.flac --model small.en` (~20 min per service)
3. `.venv/Scripts/python audio.py analyze recordings/<date>.flac` — audio checks (loudness, clipping, clicks, hum, noise, tonal balance) → `work/<date>/qc.json`.
4. `.venv/Scripts/python process.py align recordings/<date>.flac` — check `work/<date>/cuts.json` against the transcript; add cues to `liturgy.json` if a part is missed, or pin a start in `work/<date>/overrides.json` (`{"great-litany": 369.2}`).
5. `.venv/Scripts/python process.py export recordings/<date>.flac --date <date> --clean b` — renders `docs/audio/<date>.mp3` (stops at service end) and adds the parts as a Draft Sunday.
6. Commit and push. Boundaries, names, tones and the Draft flag are then fixed on the live site (Edit).

Changing the cleanup level later: re-run step 4 with another `--clean`; parts and labels edited on the site are kept.
`audition.py` rebuilds the A–E comparison at `docs/cleanup/`. Presets live in `audio.py` (`PRESETS`).
Export pulls site edits first; still, avoid re-exporting often — every audio re-export adds ~65–90 MB to git history.

## Files

- `liturgy.json` — service order and the opening words used to find each part. Add cues here if a part isn't found.
- `audio.py` — measured QC and cleanup (hum notches tuned to the measured, drifting fan hum; learned-profile noise reduction; crisp EQ).
- `process.py` — transcribe (faster-whisper) → align (fuzzy cue match + global ordering + pause snapping) → slice (ffmpeg, loudness-normalized mono MP3).
- `review.py` / `review.html` — older local boundary tool (superseded by Edit mode on the site).
- `audition.py` / `audition.html` — cleanup comparison page generator.
- `docs/` — the published page. `docs/clips.json` holds all names/tones/notes; the page's **Edit** button saves to it through the GitHub API using an editor key (fine-grained token with Contents read/write on this repo only).
