"""
tools/coven/akashic_memory.py
==============================
AkashicMemory + SwarmMetaLearner — cross-session episodic memory and
strategy learning for Sabrina's coven worker swarm.

Extracted from sabrina_coven_orchestrator_LAB.py.
"""

import os
import json
import datetime
from typing import List, Dict, Any, Optional

from .shared import STORAGE_DIR, C


# ─────────────────────────────────────────────────────────────────────────────
#  AkashicMemory — episodic probe outcome store
# ─────────────────────────────────────────────────────────────────────────────

class AkashicMemory:
    """
    Episodic memory for the swarm.
    Stores probe outcomes to JSONL; recalls winning payloads before
    re-probing similar targets. No vector DB — keyword + tech overlap scoring.
    """
    MEMORY_FILE = os.path.join(STORAGE_DIR, "swarm_memory.jsonl")

    def __init__(self):
        os.makedirs(STORAGE_DIR, exist_ok=True)
        self._entries: List[Dict[str, Any]] = []
        self._load()
        print(f"  {C.CN}[MEMORY] {len(self._entries)} episodic entries from prior runs.{C.X}")

    def _load(self):
        if not os.path.exists(self.MEMORY_FILE):
            return
        try:
            with open(self.MEMORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self._entries.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass

    def record(self, host: str, vuln_type: str, payload: str, success: bool,
               tech_stack: Optional[Dict[str, str]] = None, waf: str = "",
               evidence: str = ""):
        entry = {
            "ts":        datetime.datetime.now().isoformat(),
            "host":      host,
            "vuln_type": vuln_type,
            "payload":   payload[:500],
            "success":   success,
            "tech":      tech_stack or {},
            "waf":       waf,
            "evidence":  evidence[:200],
        }
        self._entries.append(entry)
        try:
            with open(self.MEMORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def recall(self, host: str, vuln_type: str,
               tech_stack: Optional[Dict[str, str]] = None,
               top_k: int = 5) -> List[Dict[str, Any]]:
        hits = []
        for e in self._entries:
            if not e.get("success") or e.get("vuln_type") != vuln_type:
                continue
            score = 3 if e.get("host") == host else 0
            for k, v in (tech_stack or {}).items():
                if e.get("tech", {}).get(k) == v:
                    score += 1
            hits.append((score, e))
        hits.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in hits[:top_k]]

    def stats(self) -> Dict[str, Any]:
        by_vuln: Dict[str, int] = {}
        for e in self._entries:
            vt = e.get("vuln_type", "unknown")
            by_vuln[vt] = by_vuln.get(vt, 0) + 1
        return {
            "total":     len(self._entries),
            "successes": sum(1 for e in self._entries if e.get("success")),
            "by_vuln":   by_vuln,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  SwarmMetaLearner — cross-site universal pattern bank
# ─────────────────────────────────────────────────────────────────────────────

class SwarmMetaLearner:
    """
    Cross-site universal pattern learning.
    Learns which probe patterns work across sites grouped by tech-stack and WAF
    vendor. Provides confidence-ranked recommendations before each probe.
    Persisted to storage/meta_learner.json.
    """
    META_FILE = os.path.join(STORAGE_DIR, "meta_learner.json")

    def __init__(self):
        os.makedirs(STORAGE_DIR, exist_ok=True)
        self._profiles      : Dict[str, Dict[str, Any]] = {}
        self._pattern_stats : Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        if not os.path.exists(self.META_FILE):
            return
        try:
            with open(self.META_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._profiles      = data.get("profiles", {})
            self._pattern_stats = data.get("pattern_stats", {})
        except Exception:
            pass

    def _save(self):
        try:
            with open(self.META_FILE, "w", encoding="utf-8") as f:
                json.dump({"profiles": self._profiles,
                           "pattern_stats": self._pattern_stats}, f, indent=2)
        except Exception:
            pass

    def record_result(self, site: str, pattern_type: str, success: bool,
                      tech_stack: Optional[Dict[str, str]] = None, waf: str = ""):
        p = self._profiles.setdefault(site, {
            "tech": tech_stack or {}, "waf": waf,
            "successful": [], "failed": [], "total": 0,
        })
        p["total"] += 1
        bucket = "successful" if success else "failed"
        if pattern_type not in p[bucket]:
            p[bucket].append(pattern_type)
        ps = self._pattern_stats.setdefault(pattern_type, {
            "success": 0, "failure": 0, "sites": []
        })
        if success:
            ps["success"] += 1
            if site not in ps["sites"]:
                ps["sites"].append(site)
        else:
            ps["failure"] += 1
        self._save()

    def get_recommended_patterns(self, tech_stack: Optional[Dict[str, str]] = None,
                                 waf: str = "", top_k: int = 5) -> List[str]:
        ranked: List[tuple] = []
        for pt, ps in self._pattern_stats.items():
            total = ps["success"] + ps["failure"]
            if total == 0:
                continue
            score = ps["success"] / total
            for site in ps.get("sites", []):
                prof = self._profiles.get(site, {})
                for k, v in (tech_stack or {}).items():
                    if prof.get("tech", {}).get(k) == v:
                        score += 0.15
                        break
            if waf:
                for site in ps.get("sites", []):
                    if self._profiles.get(site, {}).get("waf") == waf:
                        score += 0.10
                        break
            ranked.append((score, pt))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [pt for _, pt in ranked[:top_k]]

    def universal_patterns(self, min_sites: int = 2) -> List[str]:
        return [
            pt for pt, ps in self._pattern_stats.items()
            if len(ps.get("sites", [])) >= min_sites
        ]
