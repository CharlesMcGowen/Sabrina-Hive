"""
Sabrina Swarm Autonomous Qualifier Daemon
=========================================
Provenance (Sabrina/data/swarm_autonomous_qualifier_provenance.json):
- Architecture Spec: IBM Granite (`granite3.1-dense:8b`)
- Module Implementation: Alibaba Qwen (`qwen2.5-coder:14b`)
- Cross-Family Audits: IBM Granite Jury (`sabrina-jury-granite:0.2`) & Meta Llama (`llama3.1:8b`)
- Assistant Glue: Explicitly marked inline (`import sys`, `threat` review loop, UTF-8 file open).
"""
import argparse
import sys  # [Assistant glue: restored sys import dropped in greedy CEGAR pass]
from pathlib import Path
import json
import sqlite3
import time
import urllib.request
from typing import Dict, List, Tuple

# Add root directory to sys.path
ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

# Import necessary modules
from tools.portal_policy import DEFAULT_DB, PortalPolicy
from tools.portal_evaluation import Evaluator
from tools.portal_transport import refresh_protocol
from tools.cyber_compliance_sentinel import CyberComplianceSentinel

# Constants
OLLAMA_CHAT_URL = "http://192.168.1.133:11434/api/chat"
JURY_REVIEWERS = [
    ("sabrina-jury-granite:0.2", "ego:sabrina-jury-granite:0.2"),
    ("llama3.1:8b", "ego:llama3.1:8b")
]
TARGET_GUILD_ROLES = [
    ("hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api", "structured_extraction"),
    ("hf-gradio-dkt3-digital-twin", "structured_extraction"),
    ("hf-gradio-parmeshlata-portfolio-chatbot", "structured_extraction"),
    ("hf-gradio-himakesh-parth-ai-tutor", "grounded_summarization"),
    ("hf-gradio-htchu-coderhelper", "critique"),
    ("hf-gradio-affanaiengr-rag", "critique"),
    ("hf-gradio-free234-snap-rag-assistant", "grounded_summarization"),
    ("hf-gradio-rstanley2026-rodney-digital-twin", "grounded_summarization"),
    ("hf-gradio-nakaassoumatine-einstein-chatbot", "grounded_summarization")
]

def adjudicate_pending_reviews(ev: Evaluator, sentinel: CyberComplianceSentinel, portal_id: str, run_id: str) -> dict:
    packet = ev.review_packet(run_id)
    for item in packet:
        threat = sentinel.inspect_and_report_threat(source_identifier=portal_id, content=item.get('answer', ''), context={'run_id': run_id, 'case_id': item['case_id']})
        if threat is not None:
            # [Assistant glue: ensure both reviewers record critical threat rejection]
            for _, reviewer_id in JURY_REVIEWERS:
                try:
                    ev.review(run_id, item["case_id"], item["attempt"], reviewer=reviewer_id, passed=False, notes="Blocked by CyberComplianceSentinel", critical=True)
                except ValueError:
                    pass
        else:
            for model_name, reviewer_id in JURY_REVIEWERS:
                try:
                    data = {
                        "model": model_name,
                        "stream": False,
                        "format": "json",
                        "messages": [
                            {"role": "system", "content": "Grade blinded answer against rubric. Return ONLY JSON: {\"passed\": true/false, \"critical\": false, \"notes\": \"brief reason\"}"},
                            {"role": "user", "content": json.dumps({"prompt": item["prompt"], "rubric": item["rubric"], "answer": item["answer"]}, ensure_ascii=False)}
                        ],
                        "options": {
                            "temperature": 0.1,
                            "num_predict": 220
                        }
                    }
                    req = urllib.request.Request(OLLAMA_CHAT_URL, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req, timeout=90) as response:
                        resp_data = json.load(response)
                    verdict = json.loads(resp_data["message"]["content"])
                    passed_val = bool(verdict.get("passed", False))
                    crit_val = bool(verdict.get("critical", False))
                    notes_val = str(verdict.get("notes") or "Rubric check complete").strip() or "Rubric check complete"
                except Exception:
                    passed_val, crit_val, notes_val = False, False, "Jury parse error"
                try:
                    ev.review(run_id, item["case_id"], item["attempt"], reviewer=reviewer_id, passed=passed_val, notes=notes_val, critical=crit_val)
                except ValueError:
                    pass
    return ev.summarize(run_id)

def run_cycle(policy: PortalPolicy, ev: Evaluator, sentinel: CyberComplianceSentinel, max_steps: int) -> dict:
    steps_used = 0
    portal_reports = []
    for portal_id, role in TARGET_GUILD_ROLES:
        prof = policy.profile(portal_id)
        if not prof or not prof["access_allowed"] or prof["access_state"] in ("uncertain", "auth_required", "captcha", "access_denied", "quota_exhausted"):
            portal_reports.append({"portal_id": portal_id, "role": role, "status": "skipped"})
            continue
        elig = policy.eligibility(portal_id, role)
        if elig.get("eligible") and elig.get("responsibility") == "qualified":
            portal_reports.append({"portal_id": portal_id, "role": role, "status": "already_qualified"})
            continue
        if not json.loads(prof["adapter_json"]).get("verified_protocol"):
            refresh_protocol(policy, portal_id)
        with policy.db() as c:
            row = c.execute('SELECT run_id FROM portal_eval_runs WHERE portal_id=? AND role=? AND state IN (?, ?) ORDER BY created DESC LIMIT 1', (portal_id, role, 'running', 'pending_review')).fetchone()
        if row is None:
            run_id = ev.start(portal_id, role)
        else:
            run_id = row[0]
        while steps_used < max_steps:
            res = ev.run(run_id, max_requests=1)
            steps_used += res.get("sent", 0)
            if res.get("deferred") in ("provider_cooldown", "provider_busy"):
                time.sleep(10.5)
                continue
            if res["state"] in ("complete", "pending_review") or res.get("sent", 0) == 0 or steps_used >= max_steps or res.get("deferred") in ("daily_budget_exhausted", "uncertain", "auth_required", "captcha", "access_denied", "quota_exhausted"):
                break
        summary = adjudicate_pending_reviews(ev, sentinel, portal_id, run_id)
        portal_reports.append({"portal_id": portal_id, "role": role, **summary})
        if policy.profile(portal_id)["access_state"] == "uncertain" or steps_used >= max_steps:
            break
    status_file = ROOT_DIR / "data" / "swarm_autonomous_qualifier_status.json"
    with open(status_file, 'w', encoding='utf-8') as f:  # [Assistant glue: explicit utf-8]
        json.dump({"updated_at": time.time(), "steps_used": steps_used, "reports": portal_reports}, f, indent=2)
    return {"updated_at": time.time(), "steps_used": steps_used, "reports": portal_reports}

def main():
    parser = argparse.ArgumentParser(description="Swarm Autonomous Qualifier")
    parser.add_argument('--db', default=str(DEFAULT_DB), help='Database path')
    parser.add_argument('--once', action="store_true", help='Run once and exit')
    parser.add_argument('--daemon', action="store_true", help='Run as a daemon')
    parser.add_argument('--interval-sec', type=int, default=300, help='Interval in seconds for daemon mode')
    parser.add_argument('--max-steps-per-cycle', type=int, default=6, help='Maximum steps per cycle')
    args = parser.parse_args()

    policy = PortalPolicy(args.db)
    ev = Evaluator(policy)
    sentinel = CyberComplianceSentinel()

    if args.daemon:
        while True:
            try:
                run_cycle(policy, ev, sentinel, args.max_steps_per_cycle)
            except Exception as e:
                print(f"Error in daemon loop: {e}")
            time.sleep(args.interval_sec)
    else:
        result = run_cycle(policy, ev, sentinel, args.max_steps_per_cycle)
        print(json.dumps(result))
        sys.exit(0)

if __name__ == "__main__":
    main()
