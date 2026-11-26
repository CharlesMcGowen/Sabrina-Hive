import sqlite3
s = sqlite3.connect("/tmp/sabrina_routing_candidate_workspace/data/sabrina_assistant.sqlite3")
d = sqlite3.connect("/tmp/sabrina_clean.sqlite3")
s.backup(d)
s.close()
d.close()
print("BACKUP_SUCCESS")
