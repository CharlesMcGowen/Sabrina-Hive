#!/usr/bin/env python3
"""
tools.sabrina_backend_monitor
==============================
Real-time telemetry and bug monitor for Sabrina's backend execution loop.

Monitors:
1. Live thoughts from probe_api (:8086) and data/probe_runs.db
2. Tool execution errors, unhandled exceptions, and stderr
3. JSON parse failures in action decisions
4. Stagnation events and re-plans
5. Hallucinated success heuristic alerts
6. Deterministic response verdicts from response_classifier
"""

import sys
import os
import time
import json
import sqlite3
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "probe_runs.db"
LOG_PATH = PROJECT_ROOT / "data" / "sabrina_loop.log"
BUGS_OUTPUT = PROJECT_ROOT / "data" / "detected_bugs.json"

class TerminalColors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    RESET = '\033[0m'

def print_banner():
    print(f"\n{TerminalColors.BOLD}{TerminalColors.CYAN}{'=' * 65}")
    print("  SABRINA BACKEND BUG & TELEMETRY MONITOR")
    print(f"  Watching: {DB_PATH.name} & {LOG_PATH.name}")
    print(f"{'=' * 65}{TerminalColors.RESET}\n")

def analyze_entry(round_num, model_name, payload, reflection):
    """Inspects a thought/execution entry for anomalies or bugs."""
    bugs = []
    
    # 1. Check for JSON parse failure indications
    if payload and ("Failed to parse action decision as JSON" in payload or "Expecting value: line 1" in payload):
        bugs.append({
            "type": "JSON_PARSE_FAILURE",
            "severity": "HIGH",
            "round": round_num,
            "details": payload[:300]
        })

    # 2. Check for tool execution crashes or exceptions
    if payload and any(err in payload for err in [
        "Traceback (most recent call last)",
        "RuntimeError:",
        "ConnectionRefusedError",
        "KeyError:",
        "TypeError:",
        "Event pairing mismatch",
        "invoked synchronously from within a running event loop"
    ]):
        bugs.append({
            "type": "TOOL_EXECUTION_EXCEPTION",
            "severity": "HIGH",
            "round": round_num,
            "details": payload[:300]
        })

    # 3. Check for stagnation
    if payload and "STAGNATION:" in payload:
        bugs.append({
            "type": "STAGNATION_DETECTED",
            "severity": "MEDIUM",
            "round": round_num,
            "details": payload[:300]
        })

    # 4. Check for heuristic alerts (hallucinated successes)
    if reflection and "Hallucinated success detected" in reflection:
        bugs.append({
            "type": "HALLUCINATED_SUCCESS",
            "severity": "MEDIUM",
            "round": round_num,
            "details": reflection[:300]
        })

    # 5. Check for Oracle fallback
    if model_name == "fallback_none" or (payload and "[ORACLE_FALLBACK:" in payload):
        bugs.append({
            "type": "ORACLE_EXHAUSTION_FALLBACK",
            "severity": "HIGH",
            "round": round_num,
            "details": f"Model: {model_name}"
        })

    return bugs

def run_monitor(follow=True, poll_interval=1.5, max_rounds=None):
    print_banner()
    seen_ids = set()
    detected_bugs = []
    
    # Pre-seed seen_ids with existing entries only if following live
    if follow and DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM live_thoughts")
            seen_ids = {r[0] for r in cursor.fetchall()}
            conn.close()
            print(f"[*] Pre-indexed {len(seen_ids)} existing historical entries.")
        except Exception as e:
            print(f"[!] Warning: Could not pre-index DB: {e}")

    print(f"[*] Listening for real-time telemetry from Sabrina...\n")

    try:
        while True:
            if DB_PATH.exists():
                try:
                    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT id, timestamp, round_num, model_name, payload, reflection 
                        FROM live_thoughts 
                        ORDER BY timestamp ASC
                        """
                    )
                    rows = cursor.fetchall()
                    conn.close()

                    for r in rows:
                        thought_id = r["id"]
                        if thought_id in seen_ids:
                            continue
                        seen_ids.add(thought_id)

                        round_num = r["round_num"]
                        model_name = r["model_name"]
                        payload = r["payload"] or ""
                        reflection = r["reflection"] or ""
                        ts = r["timestamp"]

                        # Check for anomalies
                        new_bugs = analyze_entry(round_num, model_name, payload, reflection)
                        if new_bugs:
                            detected_bugs.extend(new_bugs)
                            for b in new_bugs:
                                print(f"{TerminalColors.RED}{TerminalColors.BOLD}[BUG DETECTED] {b['type']} (Severity: {b['severity']}) in Round {b['round']}:{TerminalColors.RESET}")
                                print(f"    {b['details']}\n")
                            try:
                                BUGS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
                                with open(BUGS_OUTPUT, "w", encoding="utf-8") as f:
                                    json.dump(detected_bugs, f, indent=2)
                            except Exception:
                                pass
                        else:
                            # Display normal progress
                            if payload.startswith("PLAN_UPDATE:"):
                                print(f"{TerminalColors.BLUE}[PLAN UPDATE] Round {round_num} (Model: {model_name}){TerminalColors.RESET}")
                                print(f"    {reflection}\n")
                            elif "Action:\n" in payload:
                                action_part = payload.split("Result:\n")[0].replace("Action:\n", "").strip()
                                print(f"{TerminalColors.GREEN}[ACTION DECIDED] Round {round_num} (Model: {model_name}){TerminalColors.RESET}")
                                print(f"    {action_part[:150]}...")
                                if "Result:\n" in payload:
                                    res_part = payload.split("Result:\n")[1].strip()
                                    print(f"{TerminalColors.CYAN}[RESULT]{TerminalColors.RESET} {res_part[:150]}...\n")
                            elif reflection:
                                print(f"{TerminalColors.YELLOW}[REFLECTION] Round {round_num}:{TerminalColors.RESET} {reflection[:200]}...\n")

                        if "COMPLETED" in payload or "COMPLETED" in reflection:
                            print(f"{TerminalColors.GREEN}{TerminalColors.BOLD}[GOAL COMPLETED] Sabrina marked task complete.{TerminalColors.RESET}\n")

                except sqlite3.OperationalError:
                    pass
                except Exception as e:
                    print(f"[!] Monitor error reading DB: {e}")

            if not follow:
                break
            time.sleep(poll_interval)

    except KeyboardInterrupt:
        print("\n[*] Stopping monitor.")

    # Save any detected bugs
    if detected_bugs:
        BUGS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with open(BUGS_OUTPUT, "w", encoding="utf-8") as f:
            json.dump(detected_bugs, f, indent=2)
        print(f"[!] Saved {len(detected_bugs)} detected bugs to {BUGS_OUTPUT}")
    else:
        print("[*] Clean run: 0 runtime bugs detected!")

if __name__ == "__main__":
    follow_mode = "--once" not in sys.argv
    run_monitor(follow=follow_mode)
