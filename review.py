"""Local boundary-review page: python review.py <audio>  -> http://localhost:7788"""
import http.server
import json
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent
AUDIO = Path(sys.argv[1]).resolve()
CUTS = ROOT / "work" / AUDIO.stem / "cuts.json"
LIT = ROOT / "liturgy.json"
PORT = 7788


class H(http.server.SimpleHTTPRequestHandler):
    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send((ROOT / "review.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/cuts":
            self._send(CUTS.read_bytes(), "application/json")
        elif self.path == "/liturgy":
            self._send(LIT.read_bytes(), "application/json")
        elif self.path.startswith("/audio"):
            self._send_audio()
        else:
            self.send_error(404)

    def _send_audio(self):
        # Range support so the browser can seek in a long recording
        size = AUDIO.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a or 0)
            end = int(b) if b else size - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        ctype = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".flac": "audio/flac"}
        self.send_header("Content-Type", ctype.get(AUDIO.suffix.lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with AUDIO.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (ConnectionResetError, BrokenPipeError):
                pass

    def do_POST(self):
        if self.path == "/cuts":
            body = self.rfile.read(int(self.headers["Content-Length"]))
            json.loads(body)  # validate
            CUTS.write_bytes(body)
            self._send(b'{"ok":true}', "application/json")


if __name__ == "__main__":
    print(f"Reviewing {AUDIO.name} -> http://localhost:{PORT}  (Ctrl+C to stop)")
    webbrowser.open(f"http://localhost:{PORT}")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
