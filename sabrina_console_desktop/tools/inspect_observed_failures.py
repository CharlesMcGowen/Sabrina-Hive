#!/usr/bin/env python3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from tools.external_mind_scout import ExternalMindScout

scout = ExternalMindScout(db_path=REPO_ROOT / "data" / "sabrina_assistant.sqlite3")
for p in scout.list_portals():
    b = p.get("benchmark") or {}
    if float(b.get("delivery_reliability") or 0) > 0 and p.get("qualification_state") != "qualified":
        print(f"=== {p['portal_id']} ({p.get('actual_model')}) delivery={b.get('delivery_reliability')} correctness={b.get('answer_correctness')} ===")
        for t in b.get("tasks", []):
            print(f"  [{t.get('task_id')}] passed={t.get('passed')} detail={t.get('check_detail')} preview={repr(t.get('reply_preview', ''))[:160]}")
