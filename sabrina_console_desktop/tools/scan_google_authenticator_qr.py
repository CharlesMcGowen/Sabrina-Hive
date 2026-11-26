#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Localhost (`127.0.0.1`-only) Windows Browser Webcam & Image QR Scanner for Sabrina
==================================================================================
Ports and upgrades the operator's `EgoQT` (`brock_2fa_scanner.py` + `qr_scanner.html`)
onto Windows so you can hold your phone's Google Authenticator "Transfer accounts ->
Export accounts" QR code (`otpauth-migration://offline?data=...`) or any `otpauth://`
QR code up to your PC webcam (or drop a screenshot), select which accounts to import
(e.g. Bugcrowd, HackerOne), and save them straight into Windows Credential Manager
(`secret_store.py`) with zero secrets ever touching chat or logs.

Usage:
    python c:\Users\bcmcg\Desktop\bot\Sabrina\tools\scan_google_authenticator_qr.py
"""
from __future__ import annotations

import http.server
import json
from pathlib import Path
import secrets
import sys
import threading
import webbrowser

SABRINA_ROOT = Path(__file__).resolve().parents[1]
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from tools.custom.totp_authenticator_plugin import (
    generate_totp,
    import_qr_accounts_to_vault,
    parse_any_qr_payload,
    secret_key_name_for_service,
)

SESSION_CSRF = secrets.token_urlsafe(24)
PORT = 8765

# Held strictly in local process RAM during interactive selection; wiped on shutdown.
_LAST_SCANNED_QR: dict[str, str] = {"raw": ""}

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sabrina Local 2FA QR Importer (127.0.0.1)</title>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; max-width: 760px; margin: 28px auto; padding: 0 20px; }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 20px; margin-bottom: 18px; }
  h1 { font-size: 1.35rem; margin-top: 0; color: #38bdf8; }
  button { background: #0284c7; color: #fff; border: none; border-radius: 8px; padding: 10px 16px; font-weight: 600; cursor: pointer; margin-right: 8px; }
  button:hover { background: #0369a1; }
  button.secondary { background: #475569; }
  #reader { width: 100%; max-width: 480px; margin: 14px auto; border-radius: 8px; overflow: hidden; }
  .status { padding: 10px 14px; border-radius: 8px; background: #0f172a; border: 1px solid #334155; margin-top: 12px; }
  .acct { display: flex; align-items: center; justify-content: space-between; padding: 10px; border-bottom: 1px solid #334155; }
  input[type="text"] { background: #0f172a; color: #f8fafc; border: 1px solid #475569; border-radius: 6px; padding: 6px 10px; width: 170px; }
  .code-badge { font-family: monospace; font-size: 1.15rem; background: #064e3b; color: #6ee7b7; padding: 4px 10px; border-radius: 6px; }
</style>
</head>
<body>
  <div class="card">
    <h1>Sabrina Local Google Authenticator QR Importer</h1>
    <p style="color:#94a3b8;font-size:0.92rem;">
      Runs strictly on <code>127.0.0.1</code>. Supports both <b>Google Authenticator &rarr; Transfer accounts &rarr; Export accounts</b>
      (<code>otpauth-migration://</code>) and standard <code>otpauth://totp/</code> QR codes. Secrets save directly to Windows Credential Manager.
    </p>
    <div>
      <button id="btnStart" onclick="startCam()">Start Webcam Scanner</button>
      <button class="secondary" onclick="stopCam()">Stop Camera</button>
      <label style="margin-left:12px;font-size:0.9rem;cursor:pointer;color:#38bdf8;">
        Or scan QR image file: <input type="file" id="qrFile" accept="image/*" onchange="scanFile(this)" style="color:#94a3b8;">
      </label>
    </div>
    <div id="reader"></div>
    <div id="status" class="status">Ready. Click <b>Start Webcam Scanner</b> and hold up your Google Authenticator Export QR code.</div>
  </div>

  <div id="accountsCard" class="card" style="display:none;">
    <h2 style="font-size:1.1rem;margin-top:0;">Detected 2FA Accounts (Select Which to Save to Sabrina Vault)</h2>
    <div id="accountsList"></div>
    <div style="margin-top:16px;">
      <button onclick="importSelected()">Save Selected to Windows Credential Manager</button>
    </div>
  </div>

<script>
const CSRF = "__CSRF__";
let html5Qr = null;

function setStatus(msg) {
  document.getElementById("status").innerHTML = msg;
}

async function startCam() {
  try {
    if (!html5Qr) html5Qr = new Html5Qrcode("reader");
    setStatus("Starting webcam... Hold your Google Authenticator Export QR code steady in frame.");
    await html5Qr.start(
      { facingMode: "environment" },
      { fps: 12, qrbox: { width: 300, height: 300 } },
      async (decodedText) => {
        await stopCam();
        await inspectQr(decodedText);
      },
      () => {}
    );
  } catch (err) {
    setStatus("Camera error: " + err + " (You can also upload/drop a QR screenshot image above.)");
  }
}

async function stopCam() {
  if (html5Qr && html5Qr.isScanning) {
    try { await html5Qr.stop(); } catch (e) {}
  }
}

async function scanFile(input) {
  if (!input.files || !input.files[0]) return;
  try {
    if (!html5Qr) html5Qr = new Html5Qrcode("reader");
    const decodedText = await html5Qr.scanFile(input.files[0], true);
    await inspectQr(decodedText);
  } catch (err) {
    setStatus("Could not decode QR from image: " + err);
  }
}

async function inspectQr(qrText) {
  setStatus("QR detected! Decoding accounts locally...");
  const resp = await fetch("/api/inspect", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF": CSRF },
    body: JSON.stringify({ qr_text: qrText })
  });
  const data = await resp.json();
  if (!data.ok) {
    setStatus("Error: " + data.error);
    return;
  }
  setStatus("Decoded " + data.accounts.length + " account(s). Confirm service names below and click Save.");
  const list = document.getElementById("accountsList");
  list.innerHTML = "";
  data.accounts.forEach((a) => {
    const row = document.createElement("div");
    row.className = "acct";
    row.innerHTML = `
      <label style="display:flex;align-items:center;gap:10px;">
        <input type="checkbox" class="acct-chk" data-idx="${a.index}" checked>
        <div>
          <div><b>${a.issuer || "(No Issuer)"}</b> &mdash; <span style="color:#94a3b8">${a.account}</span></div>
          <div style="font-size:0.8rem;color:#64748b;">Current live code: <span class="code-badge">${a.code}</span></div>
        </div>
      </label>
      <div>
        <span style="font-size:0.8rem;color:#94a3b8;">Vault alias:</span>
        <input type="text" class="acct-svc" data-idx="${a.index}" value="${a.suggested_service}">
      </div>
    `;
    list.appendChild(row);
  });
  document.getElementById("accountsCard").style.display = "block";
}

async function importSelected() {
  const checks = [...document.querySelectorAll(".acct-chk:checked")];
  const indices = checks.map(c => parseInt(c.dataset.idx, 10));
  const overrides = {};
  document.querySelectorAll(".acct-svc").forEach(inp => {
    overrides[inp.dataset.idx] = inp.value.trim();
  });
  const resp = await fetch("/api/import", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF": CSRF },
    body: JSON.stringify({ selected_indices: indices, service_overrides: overrides })
  });
  const data = await resp.json();
  if (!data.ok) {
    setStatus("Import error: " + data.error);
    return;
  }
  const names = data.imported.map(x => `${x.secret_key_name} (code: ${x.code})`).join(", ");
  setStatus("Saved to Windows Credential Manager: <b>" + names + "</b>! Sabrina can now generate codes automatically.");
}
</script>
</body>
</html>
"""


class LocalQRHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return  # Silence access logs so nothing sensitive is ever printed

    def _send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/":
            body = HTML_PAGE.replace("__CSRF__", SESSION_CSRF).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        if self.headers.get("X-CSRF") != SESSION_CSRF:
            self._send_json(403, {"ok": False, "error": "Invalid CSRF token"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))

        if self.path == "/api/inspect":
            try:
                qr_text = str(body.get("qr_text") or "").strip()
                accounts = parse_any_qr_payload(qr_text)
                _LAST_SCANNED_QR["raw"] = qr_text
                redacted = []
                for idx, acct in enumerate(accounts):
                    svc_guess = (acct.get("issuer") or acct.get("account", "").split(":")[0] or f"account_{idx}").strip().lower()
                    totp = generate_totp(acct["otpauth_uri"])
                    redacted.append(
                        {
                            "index": idx,
                            "issuer": acct.get("issuer", ""),
                            "account": acct.get("account", ""),
                            "suggested_service": svc_guess,
                            "suggested_key": secret_key_name_for_service(svc_guess),
                            "code": totp["code"],
                        }
                    )
                self._send_json(200, {"ok": True, "accounts": redacted})
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if self.path == "/api/import":
            try:
                if not _LAST_SCANNED_QR["raw"]:
                    self._send_json(400, {"ok": False, "error": "No QR scanned in memory"})
                    return
                selected = [int(x) for x in (body.get("selected_indices") or [])]
                overrides = {int(k): str(v) for k, v in (body.get("service_overrides") or {}).items() if str(v).strip()}
                imported = import_qr_accounts_to_vault(
                    _LAST_SCANNED_QR["raw"],
                    selected_indices=selected,
                    service_overrides=overrides,
                )
                _LAST_SCANNED_QR["raw"] = ""  # Wipe raw QR payload from RAM immediately after vault save
                self._send_json(200, {"ok": True, "imported": imported})
                print(
                    "[QR Importer] Saved encrypted TOTP entries to Windows Credential Manager:",
                    [i["secret_key_name"] for i in imported],
                )
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            return

        self._send_json(404, {"ok": False, "error": "Unknown endpoint"})


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), LocalQRHandler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"[Sabrina 2FA QR Scanner] Listening strictly on {url}")
    print("Opening browser webcam scanner... Hold your Google Authenticator Export QR code up to your camera.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _LAST_SCANNED_QR["raw"] = ""
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
