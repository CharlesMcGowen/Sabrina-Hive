"""Small OS/SQLite primitives shared by independent availability controllers."""
from __future__ import annotations

from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import time
import uuid


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=True)
            out.flush()
            os.fsync(out.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def notify_systemd(message="WATCHDOG=1"):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address or os.name == "nt":
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.settimeout(1)
        sock.sendto(message.encode(), address)


@contextmanager
def instance_lock(path):
    """OS-released lock, unlike a PID file left behind by a crash."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0"); handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("availability controller already running") from None
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("availability controller already running") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def backup_database(path, *, migration=None, keep=7, now=None):
    """SQLite online backup includes committed WAL data. Never copy a live DB file."""
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        return None
    folder = path.parent / "availability-backups"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    tag = "before-" + migration if migration else time.strftime("%Y%m%d", time.gmtime(now or time.time()))
    target = folder / (path.name + "." + tag + ".sqlite3")
    with instance_lock(folder / (path.name + ".lock")):
        if target.exists():
            return str(target)
        tmp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as source:
                with closing(sqlite3.connect(tmp)) as dest:
                    source.backup(dest, pages=256, sleep=0.01)
                    with closing(dest.execute("PRAGMA quick_check")) as cur:
                        if cur.fetchone()[0] != "ok":
                            raise RuntimeError("Backup integrity check failed")
                    with closing(dest.execute("PRAGMA journal_mode=DELETE")) as cur:
                        cur.fetchall()
                    dest.commit()
            os.chmod(tmp, 0o600)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        daily = sorted(folder.glob(path.name + ".[0-9]*.sqlite3"), reverse=True)
        for obsolete in daily[max(1, keep):]:
            obsolete.unlink(missing_ok=True)
    return str(target)


class IncidentLedger:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS availability_incidents (
                  component TEXT PRIMARY KEY, started REAL, updated REAL, state TEXT,
                  reason TEXT, notified INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS availability_events (
                  id INTEGER PRIMARY KEY, created REAL, component TEXT, phase TEXT, detail TEXT);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def observe(self, component, ready, reason, notify=None):
        now, notification = self.clock(), None
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM availability_incidents WHERE component=?", (component,)).fetchone()
            state = "ready" if ready else "degraded"
            started = now if old is None or old["state"] == "ready" else old["started"]
            notified = old["notified"] if old and old["state"] != "ready" else 0
            if ready and old and old["notified"]:
                notification = "recovered"
            elif not ready and now - started >= 300 and not notified:
                notified, notification = 1, "unavailable"
            if old is None or old["state"] != state or old["reason"] != reason or notification:
                db.execute("INSERT INTO availability_events(created,component,phase,detail) VALUES(?,?,?,?)",
                           (now, component, notification or state, json.dumps({"reason": reason})))
            db.execute("INSERT OR REPLACE INTO availability_incidents VALUES(?,?,?,?,?,?)",
                       (component, started, now, state, reason, notified if not ready else 0))
        if notification and notify:
            notify(component, notification, reason)

    def snapshot(self):
        with self.db() as db:
            incidents = [dict(r) for r in db.execute("SELECT * FROM availability_incidents")]
            events = [dict(r) for r in db.execute("SELECT * FROM availability_events ORDER BY id DESC LIMIT 30")]
        return {"incidents": incidents, "events": events}
