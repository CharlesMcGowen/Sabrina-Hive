"""
tools/idle_improver.py
======================
Standalone idle-time self-improvement daemon for Sabrina.

When Sabrina is not actively running a pentest loop (or is blocked waiting for
a human response), this script uses the same safe write targets the loop itself
uses -- delegate_to_engineer -> tools/engines/, save_knowledge -> lab knowledge
base, write_file -> analysis/ -- to improve pentest engines, fill coverage gaps,
and ingest session history into the RAG knowledge base.

DESIGN CONSTRAINT: This script never touches PROTECTED_PATHS (the orchestration
core). It only writes to:
  - tools/engines/<vuln_type>_engine.py  (via delegate_to_engineer logic)
  - data/processed_histories.json        (bookkeeping only)
  - analysis/idle_improvement_*.md       (report output)
  - sabrina_knowledge_rag.db             (via add_knowledge from rag_knowledge)
  - config/meta_lessons.json             (via the same run_meta_review logic)

Usage:
  python tools/idle_improver.py [--scope-file PATH] [--max-engines N]
                                [--dry-run] [--once] [--config PATH]

Flags:
  --scope-file    Path to engagement scope JSON (default: juiceshop_scope.json)
  --max-engines   Max engines to refine per run (default: from config, or 3)
  --dry-run       Print what would happen without actually calling CrewAI/writing
  --once          Run one improvement pass and exit instead of looping
  --config        Path to idle_improver_config.json
  --skip-idle-check  Run even if loop appears active (for testing)
"""

import os
import sys
import json
import time
import re
import glob
import argparse
import datetime
import hashlib
import platform
import subprocess
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


def _repo(path: str) -> Path:
    return REPO_ROOT / path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "max_engines_per_run": 3,
    "prefer_oracle": "escalate",
    "engine_gen_oracle": "local_first",
    "min_idle_seconds": 60,
    "history_ingest": True,
    "meta_lesson_sweep": True,
    "gap_fill": True,
    "poll_interval_seconds": 30,
    "analysis_dir": "analysis",
    "processed_histories_record": "data/processed_histories.json",
}


def load_config(config_path: Optional[str] = None) -> dict:
    path = Path(config_path) if config_path else _repo("config/idle_improver_config.json")
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            with open(path) as f:
                overrides = json.load(f)
            cfg.update(overrides)
        except Exception as e:
            print(f"[IdleImprover] Warning: could not load config {path}: {e}. Using defaults.")
    return cfg


# ---------------------------------------------------------------------------
# Idle detection
# ---------------------------------------------------------------------------

class IdleDetector:
    PROBE_API = "http://127.0.0.1:8086"

    def is_loop_running_via_process(self) -> bool:
        try:
            if platform.system() == "Windows":
                output = subprocess.check_output(
                    ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
                    stderr=subprocess.DEVNULL, text=True
                )
                return "sabrina_gemini_loop" in output
            else:
                output = subprocess.check_output(
                    ["pgrep", "-af", "sabrina_gemini_loop"],
                    stderr=subprocess.DEVNULL, text=True
                )
                return bool(output.strip())
        except Exception:
            return False

    def probe_api_status(self) -> Optional[dict]:
        try:
            import requests
            resp = requests.get(f"{self.PROBE_API}/api/status", timeout=3)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def is_waiting_for_human(self) -> bool:
        try:
            import requests
            resp = requests.get(f"{self.PROBE_API}/api/internal/waiting-for-human", timeout=3)
            if resp.status_code == 200:
                return bool(resp.json().get("is_waiting"))
        except Exception:
            pass
        return False

    def is_idle(self):
        process_running = self.is_loop_running_via_process()
        status = self.probe_api_status()

        if self.is_waiting_for_human():
            return True, "Loop is waiting for human input"

        if status is not None:
            loop_active = status.get("loop_active", False)
            if not loop_active:
                return True, f"probe_api reports loop_active=False"
            else:
                return False, f"probe_api reports loop_active=True"
        else:
            if not process_running:
                return True, "No sabrina_gemini_loop.py process found"
            else:
                return False, "sabrina_gemini_loop.py process is running (probe_api unreachable)"


# ---------------------------------------------------------------------------
# Engine auditor
# ---------------------------------------------------------------------------

class EngineAuditor:
    ENGINES_DIR = _repo("tools/engines")

    def list_engines(self) -> list:
        engines = []
        for p in glob.glob(str(self.ENGINES_DIR / "*_engine.py")):
            path = Path(p)
            vuln_type = re.sub(r"_engine$", "", path.stem)
            stat = path.stat()
            engines.append({
                "vuln_type": vuln_type,
                "path": str(path),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
        engines.sort(key=lambda e: (e["size"], e["mtime"]))
        return engines

    def detect_gaps(self, scope_file: Optional[str] = None) -> list:
        focus = []
        if scope_file and Path(scope_file).exists():
            try:
                with open(scope_file) as f:
                    focus = json.load(f).get("focus", [])
            except Exception:
                pass
        if not focus:
            default_scope = _repo("juiceshop_scope.json")
            if default_scope.exists():
                try:
                    with open(default_scope) as f:
                        focus = json.load(f).get("focus", [])
                except Exception:
                    pass

        existing = {re.sub(r"_engine$", "", Path(p).stem)
                    for p in glob.glob(str(self.ENGINES_DIR / "*_engine.py"))}
        gaps = []
        for item in focus:
            normalized = item.lower().strip().replace("-", "_").replace(" ", "_")
            if normalized not in existing:
                gaps.append(normalized)
        return gaps


# ---------------------------------------------------------------------------
# Engine improver
# ---------------------------------------------------------------------------

class EngineImprover:
    def __init__(self, oracle_preference: str = "local_first", dry_run: bool = False):
        self.oracle_preference = oracle_preference
        self.dry_run = dry_run

    def _skeletonize(self, code: str) -> str:
        try:
            sys.path.insert(0, str(REPO_ROOT))
            from tools.ast_pruner import skeletonize_code
            return skeletonize_code(code)
        except Exception:
            return code[:3000] + ("\n# ...[truncated]" if len(code) > 3000 else "")

    def improve_engine(self, vuln_type: str, improvement_hint: str = "") -> dict:
        engines_dir = _repo("tools/engines")
        save_path = engines_dir / f"{vuln_type}_engine.py"
        existing_code = None

        if save_path.exists():
            try:
                existing_code = save_path.read_text(encoding="utf-8")
            except Exception:
                pass

        if existing_code:
            pruned = self._skeletonize(existing_code)
            instruction = (
                f"An existing engine already exists for '{vuln_type}'. REFINE it - do not start over.\n\n"
                f"--- EXISTING CODE SKELETON ---\n{pruned}\n--- END SKELETON ---\n\n"
                f"What needs to change: Improve coverage, add missing payloads, improve error handling, "
                f"add authentication support if missing, and make main() self-contained and runnable. "
                f"{improvement_hint}"
            )
            action_label = f"REFINE '{vuln_type}' engine"
        else:
            instruction = (
                f"Build a new Python pentest engine for '{vuln_type}' vulnerabilities. "
                f"It must: import requests, define a main() accepting target_url, "
                f"implement at least 3 test cases with realistic payloads, "
                f"return a structured dict of results, and handle errors gracefully. {improvement_hint}"
            )
            action_label = f"BUILD new '{vuln_type}' engine"

        if self.dry_run:
            return {"success": True, "message": f"[DRY RUN] Would: {action_label}", "code_size": 0}

        print(f"[IdleImprover] Delegating to CrewAI: {action_label}...")
        try:
            sys.path.insert(0, str(REPO_ROOT))
            from tools.agent_skills import query_sabrina_crewai, extract_and_validate_code
            raw_output = query_sabrina_crewai(instruction, vuln_type=vuln_type)
            code, status_msg = extract_and_validate_code(raw_output, save_path=str(save_path))
            if code is None:
                return {"success": False, "message": f"No valid Python returned: {str(raw_output)[:300]}", "code_size": 0}
            return {"success": True, "message": f"{status_msg} ({action_label})", "code_size": len(code)}
        except Exception as e:
            return {"success": False, "message": f"Delegation error: {e}", "code_size": 0}


# ---------------------------------------------------------------------------
# History ingester
# ---------------------------------------------------------------------------

class HistoryIngester:
    def __init__(self, record_path: str, dry_run: bool = False):
        self.record_path = _repo(record_path)
        self.dry_run = dry_run

    def _load_processed(self) -> set:
        if self.record_path.exists():
            try:
                with open(self.record_path) as f:
                    return set(json.load(f))
            except Exception:
                pass
        return set()

    def _save_processed(self, processed: set):
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.record_path, "w") as f:
            json.dump(sorted(processed), f, indent=2)

    def _fingerprint(self, path: str) -> str:
        p = Path(path)
        stat = p.stat()
        return hashlib.md5(f"{p.name}:{stat.st_size}:{stat.st_mtime}".encode()).hexdigest()

    def _extract_findings(self, history: list) -> list:
        findings = []
        for entry in history:
            role = entry.get("role", "")
            content = str(entry.get("content", ""))
            lower = content.lower()

            if role == "Execution Result" and any(
                kw in lower for kw in ["confirmed", "200 ok", "admin panel", "flag{", "jwt", "token extracted"]
            ):
                findings.append({"topic": "Confirmed finding from session", "content": content[:2000], "outcome": "worked"})

            if role == "Execution Result" and any(
                kw in lower for kw in ["blocked:", "403", "401", "error:", "failed", "skipped:"]
            ) and len(content) > 40:
                findings.append({"topic": "Failed strategy (avoid repeating)", "content": content[:1000], "outcome": "failed"})

            if role == "Reflection" and len(content) > 100:
                findings.append({"topic": "Session reflection", "content": content[:2000], "outcome": "note"})

        seen = set()
        deduped = []
        for f in findings:
            key = f["content"][:200]
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        return deduped

    def ingest_new_histories(self) -> dict:
        processed = self._load_processed()
        history_files = sorted(glob.glob(str(REPO_ROOT / "loop_history_*.json")))
        stats = {"processed": 0, "findings": 0, "skipped": 0}

        for hfile in history_files:
            fp = self._fingerprint(hfile)
            if fp in processed:
                stats["skipped"] += 1
                continue

            print(f"[IdleImprover] Ingesting: {Path(hfile).name}...")
            try:
                with open(hfile, encoding="utf-8") as f:
                    history = json.load(f)
            except Exception as e:
                print(f"[IdleImprover]   Warning: could not load {hfile}: {e}")
                processed.add(fp)
                continue

            findings = self._extract_findings(history)
            stats["processed"] += 1
            stats["findings"] += len(findings)

            if not self.dry_run and findings:
                try:
                    sys.path.insert(0, str(REPO_ROOT))
                    from tools.lab_knowledge import upsert_knowledge
                    from tools.rag_knowledge import add_knowledge as rag_add
                except ImportError as e:
                    print(f"[IdleImprover]   Warning: import failed: {e}")
                    processed.add(fp)
                    continue

                outcome_map = {"worked": "verified_worked", "failed": "verified_failed", "note": "note"}
                for fnd in findings:
                    src = outcome_map.get(fnd["outcome"], "note")
                    try:
                        upsert_knowledge(fnd["topic"], fnd["content"], source_type=src)
                    except Exception:
                        try:
                            rag_add(fnd["topic"], fnd["content"], source_type=src)
                        except Exception as e2:
                            print(f"[IdleImprover]   Warning: could not save finding: {e2}")
            elif self.dry_run:
                print(f"[IdleImprover]   [DRY RUN] Would ingest {len(findings)} findings from {Path(hfile).name}")

            processed.add(fp)

        if not self.dry_run:
            self._save_processed(processed)
        return stats


# ---------------------------------------------------------------------------
# Meta-lesson sweep
# ---------------------------------------------------------------------------

class MetaLessonSweep:
    def __init__(self, oracle_preference: str = "escalate", dry_run: bool = False):
        self.oracle_preference = oracle_preference
        self.dry_run = dry_run

    def _call_oracle(self, prompt: str) -> str:
        try:
            sys.path.insert(0, str(REPO_ROOT))
            from tools.oracle_providers import UnifiedOracleHub
            oracle = UnifiedOracleHub()
            res, _ = oracle.query(
                prompt=prompt,
                system="You are an elite AI orchestration debugger and security engineering meta-reviewer.",
                preferred=self.oracle_preference,
            )
            return res
        except Exception as e:
            return f"[Oracle error]: {e}"

    def _load_lessons(self) -> list:
        path = _repo("config/meta_lessons.json")
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save_lessons(self, lessons: list):
        path = _repo("config/meta_lessons.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(lessons, f, indent=2)

    def run(self, history_files: list) -> dict:
        stats = {"lessons_added": 0}
        existing = self._load_lessons()
        existing_texts = {l.get("lesson", "") for l in existing}

        for hfile in history_files:
            try:
                with open(hfile, encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                continue

            tail = history[-20:]
            trace = "\n".join(
                f"[{e.get('role', '?')}]: {str(e.get('content', ''))[:300]}" for e in tail
            )
            prompt = (
                f"Below is an execution trace from a Sabrina pentest session.\n"
                f"Did the agent hallucinate tool success? Get stuck in a loop? Fail to use credentials? "
                f"Repeat actions without changing approach?\n\nTrace:\n{trace}\n\n"
                f"If you spot a clear actionable issue, output: META-LESSON: <1-2 sentence rule>\n"
                f"If the session looks clean: META-LESSON: None"
            )

            if self.dry_run:
                print(f"[IdleImprover]   [DRY RUN] Would sweep {Path(hfile).name}")
                continue

            result = self._call_oracle(prompt)
            if "META-LESSON:" not in result or "META-LESSON: None" in result:
                continue

            lesson = result.split("META-LESSON:")[1].strip()
            if not lesson or any(lesson in t or t in lesson for t in existing_texts):
                continue

            print(f"[IdleImprover]   New meta-lesson: {lesson[:120]}...")
            existing.append({"timestamp": time.time(), "lesson": lesson, "source": Path(hfile).name})
            existing_texts.add(lesson)
            stats["lessons_added"] += 1

        existing = existing[-10:]
        if not self.dry_run:
            self._save_lessons(existing)
        return stats


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_report(analysis_dir: str, results: dict, dry_run: bool = False):
    if dry_run:
        print(f"[IdleImprover] [DRY RUN] Would write report to {analysis_dir}/")
        return

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = _repo(analysis_dir) / f"idle_improvement_{ts}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"# Idle Improvement Report — {ts}", "", f"**Run completed**: {datetime.datetime.now().isoformat()}", "", "## Engine Refinements"]
    for r in results.get("engine_results", []):
        icon = "OK" if r.get("success") else "FAIL"
        lines.append(f"- [{icon}] {r.get('vuln_type')}: {r.get('message', '')[:200]}")

    lines += ["", "## Gap Fills"]
    for r in results.get("gap_results", []):
        icon = "OK" if r.get("success") else "FAIL"
        lines.append(f"- [{icon}] {r.get('vuln_type')} (new): {r.get('message', '')[:200]}")

    lines += ["", "## History Ingest"]
    ingest = results.get("ingest_stats", {})
    lines += [f"- Files processed: {ingest.get('processed', 0)}", f"- Skipped (already done): {ingest.get('skipped', 0)}", f"- Findings added to RAG: {ingest.get('findings', 0)}"]

    lines += ["", "## Meta-Lessons"]
    sweep = results.get("sweep_stats", {})
    lines.append(f"- New lessons distilled: {sweep.get('lessons_added', 0)}")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[IdleImprover] Report -> {report_path}")


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------

def run_improvement_pass(cfg: dict, scope_file: Optional[str], dry_run: bool) -> dict:
    results = {"engine_results": [], "gap_results": [], "ingest_stats": {}, "sweep_stats": {}}

    auditor = EngineAuditor()
    improver = EngineImprover(oracle_preference=cfg["engine_gen_oracle"], dry_run=dry_run)
    ingester = HistoryIngester(record_path=cfg["processed_histories_record"], dry_run=dry_run)
    sweeper = MetaLessonSweep(oracle_preference=cfg["prefer_oracle"], dry_run=dry_run)

    max_e = cfg["max_engines_per_run"]
    engines = auditor.list_engines()
    print(f"[IdleImprover] {len(engines)} existing engines. Refining up to {max_e}.")
    for engine in engines[:max_e]:
        vt = engine["vuln_type"]
        age_days = (time.time() - engine["mtime"]) / 86400
        hint = f"Engine is {age_days:.1f} days old and {engine['size']} bytes. Add missing test cases."
        result = improver.improve_engine(vt, improvement_hint=hint)
        result["vuln_type"] = vt
        results["engine_results"].append(result)
        print(f"[IdleImprover]   {vt}: {'OK' if result['success'] else 'FAIL'} - {result['message'][:100]}")

    if cfg["gap_fill"]:
        gaps = auditor.detect_gaps(scope_file)
        if gaps:
            print(f"[IdleImprover] Coverage gaps: {gaps}")
            for gap in gaps[:max_e]:
                result = improver.improve_engine(gap, improvement_hint="New engine - build from scratch.")
                result["vuln_type"] = gap
                results["gap_results"].append(result)
                print(f"[IdleImprover]   gap {gap}: {'OK' if result['success'] else 'FAIL'} - {result['message'][:100]}")
        else:
            print("[IdleImprover] No coverage gaps.")

    if cfg["history_ingest"]:
        print("[IdleImprover] History ingest...")
        results["ingest_stats"] = ingester.ingest_new_histories()
        print(f"[IdleImprover]   {results['ingest_stats']}")

    if cfg["meta_lesson_sweep"]:
        all_h = sorted(glob.glob(str(REPO_ROOT / "loop_history_*.json")))
        if all_h:
            print(f"[IdleImprover] Meta-lesson sweep over {len(all_h)} files...")
            results["sweep_stats"] = sweeper.run(all_h)
            print(f"[IdleImprover]   {results['sweep_stats']}")
        else:
            print("[IdleImprover] No history files to sweep.")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sabrina idle-time self-improvement daemon")
    parser.add_argument("--scope-file", default="", help="Path to engagement scope JSON")
    parser.add_argument("--max-engines", type=int, default=None, help="Max engines to refine per pass")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument("--once", action="store_true", help="Run one pass and exit")
    parser.add_argument("--config", default="", help="Path to idle_improver_config.json")
    parser.add_argument("--skip-idle-check", action="store_true", help="Run regardless of loop state")
    args = parser.parse_args()

    cfg = load_config(args.config or None)
    if args.max_engines is not None:
        cfg["max_engines_per_run"] = args.max_engines

    scope_file = args.scope_file or str(_repo("juiceshop_scope.json"))
    detector = IdleDetector()

    print(f"[IdleImprover] Starting. dry_run={args.dry_run}, once={args.once}")
    print(f"[IdleImprover] Config: max_engines={cfg['max_engines_per_run']}, oracle={cfg['prefer_oracle']}, poll={cfg['poll_interval_seconds']}s")

    iteration = 0
    while True:
        iteration += 1
        print(f"\n[IdleImprover] === Pass {iteration} @ {datetime.datetime.now().strftime('%H:%M:%S')} ===")

        if not args.skip_idle_check:
            idle, reason = detector.is_idle()
            if not idle:
                print(f"[IdleImprover] Loop active - waiting. ({reason})")
                if args.once:
                    print("[IdleImprover] --once set but loop is active. Exiting.")
                    sys.exit(0)
                time.sleep(cfg["poll_interval_seconds"])
                continue
            print(f"[IdleImprover] Idle confirmed: {reason}")

            wait = cfg["min_idle_seconds"]
            if wait > 0:
                print(f"[IdleImprover] Grace period: {wait}s...")
                time.sleep(wait)
                idle, reason = detector.is_idle()
                if not idle:
                    print(f"[IdleImprover] Loop became active during grace period. Backing off.")
                    continue

        results = run_improvement_pass(cfg, scope_file if Path(scope_file).exists() else None, args.dry_run)
        write_report(cfg["analysis_dir"], results, dry_run=args.dry_run)

        if args.once:
            print("[IdleImprover] --once. Exiting.")
            break

        print(f"[IdleImprover] Pass complete. Sleeping {cfg['poll_interval_seconds']}s...")
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    main()
