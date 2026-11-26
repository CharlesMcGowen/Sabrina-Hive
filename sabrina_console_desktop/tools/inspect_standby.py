import sqlite3
import sys

db_path = "/tmp/sabrina_routing_candidate_workspace/data/sabrina_assistant_fresh.sqlite3"
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Look for standby_host in portal_profiles
rows = conn.execute("SELECT portal_id, access_allowed, access_reason, access_state FROM portal_profiles WHERE access_reason LIKE '%standby%' OR access_state LIKE '%standby%' LIMIT 5").fetchall()
for r in rows:
    print(dict(r))

# Look in portal_settings
settings = conn.execute("SELECT key, value FROM portal_settings").fetchall()
for s in settings:
    print(dict(s))
