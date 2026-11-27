from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["SABRINA_SWARM_API_BASE"] = "http://127.0.0.1:8095"
os.environ["SABRINA_SWARM_CONTROL_API_BASE"] = "http://127.0.0.1:8087"
log = None
tunnels = []


def configure_tk_environment(root_dir: Path = ROOT) -> None:
    """Configure Tcl/Tk library paths only when frozen or missing from sys.base_prefix."""
    base_tcl = Path(sys.base_prefix) / "tcl" / "tcl8.6"
    bundled_tcl = root_dir / "tcl" / "tcl8.6"
    bundled_tk = root_dir / "tcl" / "tk8.6"
    if getattr(sys, "frozen", False) or not (base_tcl / "init.tcl").is_file():
        if (bundled_tcl / "init.tcl").is_file():
            os.environ["TCL_LIBRARY"] = str(bundled_tcl)
        if bundled_tk.is_dir():
            os.environ["TK_LIBRARY"] = str(bundled_tk)
    else:
        if os.environ.get("TCL_LIBRARY") == str(bundled_tcl):
            os.environ.pop("TCL_LIBRARY", None)
        if os.environ.get("TK_LIBRARY") == str(bundled_tk):
            os.environ.pop("TK_LIBRARY", None)


configure_tk_environment()


def _show_fatal_error(title: str, message: str) -> None:
    try:
        from tkinter import messagebox

        messagebox.showerror(title, message)
    except Exception:
        if os.name == "nt":
            try:
                ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
            except Exception:
                pass


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def is_supervised_mode(argv: list[str] | None = None) -> bool:
    args = sys.argv[1:] if argv is None else argv
    if "--supervised" in args:
        return True
    flag = os.environ.get("SABRINA_LOCAL_MICROSERVICES", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def run_supervised() -> None:
    from tools.service_supervisor import MicroserviceSupervisor, default_local_service_specs

    supervisor = MicroserviceSupervisor(default_local_service_specs(ROOT, sys.executable))
    supervisor.run_forever()


def main(argv: list[str] | None = None) -> None:
    global log
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--followup-coordinator"] and len(args) == 2:
        from followup_coordinator import run_worker
        run_worker(args[1])
        return
    configure_tk_environment()
    log = (ROOT / "console-launch.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log
    if is_supervised_mode(argv):
        run_supervised()
        return
    try:
        # Optional: attempt SSH tunnel to ego if reachable (non-blocking, never crashes console)
        for port in (8087, 8095):
            if listening(port):
                continue
            try:
                process = subprocess.Popen(
                    [
                        "ssh.exe",
                        "-N",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ExitOnForwardFailure=yes",
                        "-o",
                        "ConnectTimeout=2",
                        "-o",
                        "ServerAliveInterval=30",
                        "-L",
                        f"127.0.0.1:{port}:127.0.0.1:{port}",
                        "ego",
                    ],
                    stdout=log,
                    stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                tunnels.append(process)
                deadline = time.monotonic() + 1.5
                while not listening(port) and time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    time.sleep(0.15)
            except Exception:
                pass

        try:
            from sabrina_desktop_ui import run_desktop_ui

            run_desktop_ui()
        except ImportError:
            from sabrina_operator_console import Console

            app = Console()
            app.mainloop()
    except Exception:
        traceback.print_exc()
        _show_fatal_error(
            "Sabrina Kraken Console",
            f"Could not launch the console. Details are in {ROOT / 'console-launch.log'}",
        )
    finally:
        for process in tunnels:
            process.terminate()


if __name__ == "__main__":
    main()
