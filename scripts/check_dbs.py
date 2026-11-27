import sqlite3
import os
import glob

def check_db(path):
    print("=== DB:", path)
    if not os.path.exists(path):
        print("Does not exist")
        return
    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cur.fetchall()
        print("Tables:", [t[0] for t in tables])
        for t in tables:
            tname = t[0]
            try:
                cur.execute(f"SELECT count(*) FROM {tname}")
                cnt = cur.fetchone()[0]
                print(f"  Table {tname}: {cnt} rows")
            except Exception as e:
                pass
    except Exception as e:
        print("Error:", e)

for db in glob.glob("sabrina_console_desktop/data/**/*.sqlite3", recursive=True):
    check_db(db)

for db in glob.glob("freqtrade_pivot/user_data/**/*.sqlite*", recursive=True):
    check_db(db)
