"""Lock the GitHub editor token behind a passcode: docs/editor.json holds only the encrypted token.

  .venv/Scripts/python set_passcode.py

Opens a small window (hidden fields), checks the token against GitHub, then writes docs/editor.json.
Commit and push docs/editor.json afterwards. Re-run any time to change the passcode or token.
The page decrypts with WebCrypto: PBKDF2-SHA256 (600k iterations) -> AES-256-GCM.
"""
import base64
import json
import os
import ssl
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import messagebox

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

OUT = Path(__file__).parent / "docs" / "editor.json"
REPO = "kstasiulewicz/liturgy-choir"
ITER = 600_000
b64 = lambda b: base64.b64encode(b).decode()


def check_token(token):
    """True if the token can read this repo's contents (Windows cert store: works under Avast)."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/contents/docs/clips.json",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=20) as r:
            return r.status == 200, ""
    except urllib.error.HTTPError as e:
        return False, f"GitHub said {e.code}. Check the token has access to liturgy-choir with Contents: Read and write."
    except Exception as e:
        return False, f"Couldn't reach GitHub: {e}"


def encrypt(token, passcode):
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(passcode.encode())
    ct = AESGCM(key).encrypt(iv, token.encode(), None)
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITER, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def main():
    root = tk.Tk()
    root.title("Liturgy site — set editor passcode")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    pad = {"padx": 12, "pady": 4, "sticky": "w"}
    tk.Label(root, text="GitHub editor token (github_pat_…)").grid(row=0, column=0, **pad)
    tok = tk.Entry(root, show="•", width=48); tok.grid(row=1, column=0, **pad)
    tk.Label(root, text="Passcode for the choir (8+ characters, e.g. holy-cherubim-1946)").grid(row=2, column=0, **pad)
    p1 = tk.Entry(root, show="•", width=48); p1.grid(row=3, column=0, **pad)
    tk.Label(root, text="Passcode again").grid(row=4, column=0, **pad)
    p2 = tk.Entry(root, show="•", width=48); p2.grid(row=5, column=0, **pad)
    status = tk.Label(root, text="", fg="#7b2d26", wraplength=380, justify="left"); status.grid(row=7, column=0, **pad)

    def save():
        t, a, b = tok.get().strip(), p1.get(), p2.get()
        if not t.startswith(("github_pat_", "ghp_")):
            return status.config(text="That doesn't look like a GitHub token.")
        if len(a) < 8:
            return status.config(text="Passcode needs at least 8 characters.")
        if a != b:
            return status.config(text="The two passcodes don't match.")
        status.config(text="Checking the token with GitHub…"); root.update()
        ok, why = check_token(t)
        if not ok:
            return status.config(text=why)
        OUT.write_text(json.dumps(encrypt(t, a), indent=1) + "\n", encoding="utf-8")
        messagebox.showinfo("Done", "Passcode set. Tell Claude it's done so the change gets published.")
        root.destroy()

    tk.Button(root, text="Save", width=12, command=save).grid(row=6, column=0, padx=12, pady=10, sticky="e")
    tok.focus_set()
    root.mainloop()


if __name__ == "__main__":
    main()
