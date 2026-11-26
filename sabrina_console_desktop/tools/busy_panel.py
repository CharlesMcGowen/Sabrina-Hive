"""Tiny web control panel for Sabrina's 'keep busy' loop -- the button you asked for.

Runs a dependency-free stdlib web server. Open it in your browser and click to flip what she
works on; the running busy loop (run_engagement.py busy) picks up the change on its next cycle.
No relaunch. Writes data/busy_control.json via tools/busy_control.

    python tools/busy_panel.py            # serves on http://0.0.0.0:8091
    python tools/busy_panel.py --port 9000

Then browse to  http://<lab-ip>:8091  (e.g. http://192.168.2.69:8091).
"""

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from tools.busy_control import read_control, write_control  # noqa: E402

LOG = REPO_ROOT / "logs" / "engagements" / "self_improve.log"
RECON_LOG_DIR = REPO_ROOT / "logs" / "engagements"


def _log_tail(n=12):
    try:
        import re
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines) or "(no activity yet)"
    except Exception:
        return "(no log yet)"


def _page():
    ctl = read_control()
    mode, paused = ctl["mode"], ctl["paused"]
    def chip(active, label, color):
        bg = color if active else "#2a2a2a"
        return f'<span style="background:{bg};padding:4px 10px;border-radius:6px;margin-right:6px">{label}</span>'
    status = (chip(paused, "PAUSED", "#b23") if paused else
              chip(mode == "improve", "SELF-IMPROVE", "#2a7") + chip(mode == "all", "RECON (all)", "#a63"))
    def btn(action, val, label, color):
        return (f'<form method="POST" action="/set" style="display:inline">'
                f'<input type="hidden" name="{action}" value="{val}">'
                f'<button style="background:{color};color:#fff;border:0;padding:12px 18px;'
                f'border-radius:8px;font-size:15px;margin:4px;cursor:pointer">{label}</button></form>')
    buttons = (
        btn("mode", "improve", "⚙️ Self-improve", "#2a7")
        + btn("mode", "all", "\U0001f3af Recon (authorized)", "#a63")
        + (btn("action", "resume", "▶ Resume", "#268") if paused
           else btn("action", "pause", "⏸ Pause", "#b23"))
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="6">
<title>Sabrina — busy control</title></head>
<body style="font-family:system-ui,sans-serif;background:#111;color:#eee;max-width:760px;margin:24px auto;padding:0 16px">
<h2>Sabrina — keep-busy control</h2>
<p style="font-size:15px">Now working on: {status}</p>
<div style="margin:14px 0">{buttons}</div>
<p style="color:#999;font-size:13px">Flip takes effect on her next loop cycle (no relaunch). "Recon (authorized)" only
touches programs you've already promoted into config/engagements/ — it is your authorization to run recon.</p>
<h3 style="margin-top:22px;font-size:15px">Recent activity</h3>
<pre style="background:#000;color:#8f8;padding:12px;border-radius:8px;overflow:auto;font-size:12px;white-space:pre-wrap">{_log_tail()}</pre>
<p style="color:#666;font-size:12px">auto-refreshes every 6s</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, code=200, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if urlparse(self.path).path in ("/", "/index.html"):
            self._send(_page())
        else:
            self._send("not found", 404, "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/set":
            self._send("not found", 404, "text/plain")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        try:
            if "mode" in form:
                write_control(mode=form["mode"][0])
            elif form.get("action", [""])[0] == "pause":
                write_control(paused=True)
            elif form.get("action", [""])[0] == "resume":
                write_control(paused=False)
        except Exception as e:
            self._send(f"error: {e}", 400, "text/plain")
            return
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *a):
        pass  # quiet


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8091)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"[busy_panel] serving on http://{a.host}:{a.port}  (control: {read_control()})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
