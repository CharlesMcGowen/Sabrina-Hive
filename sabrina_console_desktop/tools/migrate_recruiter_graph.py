"""Back up SQLite and backfill the evidence graph from captured threads.

The command is deliberately opt-in.  Running without ``--apply`` only reports
how many stored conversations are eligible for backfill.
"""
import argparse
import json
import sqlite3
from pathlib import Path

from tools.messaging_store import ConversationStore, ROOT, now
from tools.messaging_skills import load_profile
from tools.recruiter_graph import RecruiterGraph


def backup(path):
    target = Path(path).with_name(Path(path).name + ".before-recruiter-graph-" + now().replace(":", "-") + ".bak")
    source = sqlite3.connect(path)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    return target


def migrate(path=None, apply=False):
    database = Path(path or ROOT / "data/messaging.sqlite3")
    if not database.exists():
        raise FileNotFoundError(database)
    backup_path = backup(database) if apply else None
    store = ConversationStore(database)
    rows = store.db.execute("SELECT account,thread,snapshot FROM threads ORDER BY observed").fetchall()
    eligible = []
    skipped = []
    for row in rows:
        snapshot = json.loads(row["snapshot"])
        if snapshot.get("peer_id"):
            eligible.append(snapshot)
        else:
            skipped.append(snapshot.get("thread_id", row["thread"]))
    if not apply:
        store.close()
        return {"database": str(database), "threads": len(rows), "eligible": len(eligible), "skipped_no_stable_person_id": skipped, "applied": False}
    graph = RecruiterGraph(store)
    profile = load_profile()
    completed = []
    for snapshot in eligible:
        version = store.db.execute("SELECT version FROM threads WHERE account=? AND thread=?", (snapshot["account_id"], snapshot["thread_id"])).fetchone()["version"]
        graph.observe({**snapshot, "version": version}, profile)
        completed.append(snapshot["thread_id"])
    store.close()
    return {"database": str(database), "backup": str(backup_path), "threads": len(rows), "backfilled": completed,
            "skipped_no_stable_person_id": skipped, "applied": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(ROOT / "data/messaging.sqlite3"))
    parser.add_argument("--apply", action="store_true", help="Create graph tables and backfill after making a SQLite backup")
    args = parser.parse_args()
    print(json.dumps(migrate(args.database, args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

