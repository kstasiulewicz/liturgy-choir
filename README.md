# Liturgy Choir Practice

Slices a recorded Divine Liturgy into one clip per sung part and publishes a phone-friendly practice page for the choir.

## Adding a recording

1. Copy the recording (wav/mp3/m4a/flac) into `recordings/`.
2. Transcribe (~10–20 min for a 90-min liturgy on this laptop, CPU only):
   `.venv/Scripts/python process.py transcribe recordings/<file>`
3. Locate the parts: `.venv/Scripts/python process.py align recordings/<file>`
4. Check every boundary by ear: `.venv/Scripts/python review.py recordings/<file>` → http://localhost:7788
   (red-edged rows were weaker matches; nudge, rename, or drop clips, then **Save cuts**)
5. Cut the clips: `.venv/Scripts/python process.py slice recordings/<file> --date 2026-10-18 --tone 5`
6. Commit and push; GitHub Pages republishes in about a minute.

Re-slicing a recording keeps any names, tones and notes edited on the site.

## Files

- `liturgy.json` — service order and the opening words used to find each part. Add cues here if a part isn't found.
- `process.py` — transcribe (faster-whisper) → align (fuzzy cue match + global ordering + pause snapping) → slice (ffmpeg, loudness-normalized mono MP3).
- `review.py` / `review.html` — local boundary review tool.
- `site/` — the published page. `site/clips.json` holds all names/tones/notes; the page's **Edit** button saves to it through the GitHub API using an editor key (fine-grained token with Contents read/write on this repo only).
