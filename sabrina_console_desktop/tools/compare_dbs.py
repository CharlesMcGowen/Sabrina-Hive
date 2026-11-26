import sqlite3
import sys

for db_name in ["sabrina_assistant.sqlite3", "sabrina_assistant_fresh.sqlite3"]:
    db_path = f"/tmp/sabrina_routing_candidate_workspace/data/{db_name}"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        print(f"=== {db_name} ===")
        print("Tables:", tables[:8])
        if "portal_settings" in tables:
            print("Settings:", [dict(r) for r in conn.execute("SELECT * FROM portal_settings").fetchall()])
        if "portal_profiles" in tables:
            c = conn.execute("SELECT COUNT(*) FROM portal_profiles WHERE access_allowed=1").fetchone()[0]
            print(f"portal_profiles access_allowed=1: {c}")
    except Exception as e:
        print(f"Error {db_name}: {e}")
