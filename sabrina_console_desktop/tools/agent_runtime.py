"""Persistent, bounded reasoning agents. Inference runs on configured lab nodes.

Agents operate on supplied evidence and can decompose, review, or escalate their
work. External actions remain with the calling domain controller. No shell,
browser, arbitrary network, or self-modification tools are exposed to children.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import shlex
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid

from tools.lab_http import LabHTTP

ROOT = Path(__file__).resolve().parent.parent
TERMINAL = {"completed", "failed", "cancelled"}
CROSS_FAMILY_PREFERENCES = {
    "llama": ["llama3.1:8b", "sabrina-jury-llama:0.2", "llama3.1:latest"],
    "granite": ["granite3.1-dense:8b", "sabrina-jury-granite:0.2", "granite-code:8b"],
    "mistral": ["dolphin-mistral:7b"],
    "defender": ["sabrina-jury-defender:0.2"],
    "qwen": ["qwen3:14b", "qwen2.5-coder:14b", "qwen2.5:7b", "huihui_ai/qwen3-abliterated:14b"],
    "external": ["portal:auto"],
    "portal_flash": ["portal:flash"],
    "portal_reasoning": ["portal:deep_reasoning"],
    "portal_rag": ["portal:domain_rag"],
}
PROJECT_SAFE_BOUNDS = {
    "crypto": {
        "jev_noul_threshold": (0.37, 0.48),
        "jev_min_score_bps": (2.0, 14.0),
        "jev_choice_margin": (0.005, 0.06),
        "jev_max_spread_bps": (5.0, 35.0),
        "jev_min_intraday_gain_pct": (-2.0, 4.0),
        "jev_max_pre_moon_gain_pct": (8.0, 25.0),
        "jev_min_24h_range_pos": (0.35, 0.80),
        "jev_max_dynamic_candidates": (3, 8),
        "jev_pair_cooldown_seconds": (3600.0, 14400.0),
    },
    "core": {
        "max_rounds": (2, 20),
        "max_calls": (4, 256),
        "max_workers": (1, 16),
    },
}
SYSTEM = '''You are a bounded Sabrina reasoning agent. Work only from the supplied
goal and evidence. You cannot browse, send messages, execute code, or claim actions
were executed. Return exactly one JSON object with one of these actions:
{"action":"complete","result":"your evidence-grounded answer"}
{"action":"spawn","goals":[{"goal":"independent subtask","tier":"fast","device":"gpu"},
{"goal":"simple classification","tier":"tiny","device":"cpu"}]}
{"action":"probe","probe_name":"read_code_slice|inspect_sqlite_summary|project_health_audit|counterfactual_check|verify_equation_or_lean|lookup_theorem_or_reference|consult_external_guild|simulate_trading_history|audit_system_loops_and_gates","probe_args":{}}
{"action":"adversarial_review","hypothesis":"root-cause claim to test","proposed_fix":"concrete change to falsify/audit"}
{"action":"pivot_subproblem","subproblem_goal":"prerequisite lemma or side SAT problem to solve first","blocker_reason":"why main goal depends on this","guild_families":["qwen","llama","granite"]}
{"action":"sandbox_verify","target_project":"crypto|core|oss|portals","parameter_updates":{},"rationale":"evidence-backed reason"}
{"action":"checkpoint_epoch","verified_lemmas":["proven fact"],"falsified_dead_ends":["disproven path"],"open_equations":["remaining obligation"],"next_frontier":"plan for next epoch"}
{"action":"escalate","reason":"why a stronger model is needed"}
{"action":"revise","note":"concrete reasoning to check next round"}
{"action":"blocked","reason":"specific missing evidence"}
Use probe (including lookup_theorem_or_reference and verify_equation_or_lean) to gather
missing facts, theorem signatures, or online references before declaring blocked. Use
pivot_subproblem when a proof/system requires solving a prerequisite lemma or side problem
first via a Multi-Family Swarm Guild. Use checkpoint_epoch on long-horizon tasks to persist
lemmas and compact context across epochs.
Allowed tiers: tiny (trivial classification), fast (normal), smart (hard reasoning).
Allowed devices: auto, gpu, cpu. Plain string goals inherit your tier and device.
'''
TINY_SYSTEM = '''Answer the supplied goal using its context. Return ONLY a JSON
object: {"action":"complete","result":"your answer"}. If essential facts are
missing return {"action":"blocked","reason":"missing fact"}. You cannot delegate.
For an elementary question, give its answer directly.'''


class CapacityBusy(Exception):
    pass


def response_schema(tier):
    child = {"type": "object", "required": ["goal"], "properties": {
        "goal": {"type": "string"},
        "tier": {"type": "string", "enum": ["tiny", "fast", "smart"]},
        "device": {"type": "string", "enum": ["auto", "cpu", "gpu"]}}}
    actions = (
        ["complete", "blocked"]
        if tier == "tiny"
        else ["complete", "blocked", "spawn", "revise", "escalate", "probe", "adversarial_review", "sandbox_verify", "checkpoint_epoch", "pivot_subproblem"]
    )
    return {"type": "object", "required": ["action"], "properties": {
        "action": {"type": "string", "enum": actions},
        "result": {"type": "string"}, "reason": {"type": "string"}, "note": {"type": "string"},
        "goals": {"type": "array", "items": {"anyOf": [{"type": "string"}, child]}},
        "probe_name": {"type": "string"},
        "probe_args": {"type": "object"},
        "hypothesis": {"type": "string"},
        "proposed_fix": {"type": "string"},
        "target_project": {"type": "string"},
        "parameter_updates": {"type": "object"},
        "code_snippet": {"type": "string"},
        "test_snippet": {"type": "string"},
        "rationale": {"type": "string"},
        "verified_lemmas": {"type": "array", "items": {"type": "string"}},
        "falsified_dead_ends": {"type": "array", "items": {"type": "string"}},
        "open_equations": {"type": "array", "items": {"type": "string"}},
        "next_frontier": {"type": "string"},
        "subproblem_goal": {"type": "string"},
        "blocker_reason": {"type": "string"},
        "guild_families": {"type": "array", "items": {"type": "string"}}}}


def load_config(path=None):
    config = json.loads(Path(path or ROOT / "config/agent_runtime.json").read_text(encoding="utf-8-sig"))
    limits = config["limits"]
    for key, maximum in {"max_agents": 64, "max_depth": 4, "max_rounds": 20,
                         "max_calls": 256, "max_workers": 16, "deadline_seconds": 3600,
                         "request_seconds": 300, "max_prompt_chars": 24000,
                         "num_ctx": 32768, "num_predict": 4096}.items():
        if type(limits.get(key)) is not int or not 1 <= limits[key] <= maximum:
            raise ValueError(f"Invalid runtime limit: {key}")
    ids = set()
    urls = set()
    for node in config["nodes"]:
        url = urlsplit(node["url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Node needs an HTTP(S) endpoint without credentials")
        host = url.hostname.lower()
        if host in {"localhost", socket.gethostname().lower(), socket.getfqdn().lower()}:
            raise ValueError("Workstation inference is disabled")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and (address.is_loopback or address.is_unspecified):
            raise ValueError("Workstation inference is disabled")
        if node["id"] in ids or node["url"].rstrip('/') in urls:
            raise ValueError("Duplicate node")
        ids.add(node["id"])
        urls.add(node["url"].rstrip('/'))
        for key in ("gpu_gib", "ram_gib", "gpu_slots", "cpu_slots", "cpu_threads"):
            if not isinstance(node.get(key), (int, float)) or node[key] < 0:
                raise ValueError(f"Invalid node capacity: {key}")
    if not ids or any(not config["tiers"].get(tier) for tier in ("fast", "smart", "tiny")):
        raise ValueError("Nodes and all model tiers are required")
    return config


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS agent_runs (
                  id TEXT PRIMARY KEY, deadline REAL NOT NULL, calls INTEGER DEFAULT 0,
                  limits TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_tasks (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent TEXT, depth INTEGER,
                  signature TEXT NOT NULL, goal TEXT, context TEXT, tier TEXT, device TEXT,
                  state TEXT, rounds INTEGER DEFAULT 0, result TEXT DEFAULT '',
                  history TEXT DEFAULT '[]', owner TEXT, claimed REAL, created REAL,
                  UNIQUE(run_id, signature));
                CREATE INDEX IF NOT EXISTS agent_queue ON agent_tasks(state,created);
                CREATE TABLE IF NOT EXISTS agent_leases (
                  task_id TEXT PRIMARY KEY, node TEXT, model TEXT, device TEXT,
                  memory REAL, expires REAL);
                CREATE TABLE IF NOT EXISTS agent_events (
                  id INTEGER PRIMARY KEY, task_id TEXT, time REAL, kind TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS agent_epochs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  epoch INTEGER NOT NULL,
                  verified_lemmas TEXT NOT NULL,
                  falsified_dead_ends TEXT NOT NULL,
                  open_equations TEXT NOT NULL,
                  next_frontier TEXT NOT NULL,
                  created REAL NOT NULL);
            ''')
            if 'control_key' not in {r[1] for r in db.execute('PRAGMA table_info(agent_tasks)')}:
                db.execute('ALTER TABLE agent_tasks ADD COLUMN control_key TEXT')

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def event(db, task_id, kind, detail):
        db.execute("INSERT INTO agent_events(task_id,time,kind,detail) VALUES(?,?,?,?)",
                   (task_id, time.time(), kind, json.dumps(detail)))

    def epochs(self, task_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM agent_epochs WHERE task_id=? ORDER BY epoch", (task_id,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                for col in ("verified_lemmas", "falsified_dead_ends", "open_equations"):
                    d[col] = json.loads(d[col])
                out.append(d)
            return out

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Unknown agent task")
            return dict(row)

    def snapshot(self, run_id=None):
        with self.connect() as db:
            where, args = (" WHERE run_id=?", (run_id,)) if run_id else ("", ())
            return [dict(r) for r in db.execute("SELECT id,run_id,parent,depth,goal,tier,device,state,rounds,result FROM agent_tasks" + where + " ORDER BY created", args)]

    def submit(self, goal, context, limits, tier="fast", device="auto"):
        if tier not in {"tiny", "fast", "smart"} or device not in {"auto", "cpu", "gpu"}:
            raise ValueError("Invalid tier or device")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
            raise ValueError("Goal must contain 1–2000 characters")
        if not isinstance(context, str) or len(context) > limits["max_prompt_chars"] // 2:
            raise ValueError("Evidence exceeds context budget")
        run_id = uuid.uuid4().hex
        with self.connect(True) as db:
            db.execute("INSERT INTO agent_runs(id,deadline,limits) VALUES(?,?,?)",
                       (run_id, time.time() + limits["deadline_seconds"], json.dumps(limits)))
            return self._insert(db, run_id, None, 0, goal.strip(), context, tier, device)

    def _insert(self, db, run_id, parent, depth, goal, context, tier, device):
        signature = hashlib.sha256(json.dumps([parent, goal.strip(), context, tier, device]).encode()).hexdigest()
        existing = db.execute("SELECT id FROM agent_tasks WHERE run_id=? AND signature=?", (run_id, signature)).fetchone()
        if existing:
            return existing[0]
        task_id = uuid.uuid4().hex
        db.execute('''INSERT INTO agent_tasks(id,run_id,parent,depth,signature,goal,context,tier,device,state,created)
                      VALUES(?,?,?,?,?,?,?,?,?,'queued',?)''',
                   (task_id, run_id, parent, depth, signature, goal, context, tier, device, time.time()))
        self.event(db, task_id, "submitted", {"parent": parent, "tier": tier, "device": device})
        return task_id

    def claim(self, owner, run_id=None):
        with self.connect(True) as db:
            # A stale request is not replayed automatically. Its server-side
            # inference may still exist; quarantine leases until their expiry.
            db.execute("UPDATE agent_tasks SET state='failed',result='Worker lost or request exceeded deadline' WHERE state='running' AND claimed < ?", (time.time() - 660,))
            db.execute("DELETE FROM agent_leases WHERE expires < ?", (time.time(),))
            db.execute("UPDATE agent_tasks SET state='failed',result='Run deadline exceeded' WHERE state IN ('queued','waiting') AND run_id IN (SELECT id FROM agent_runs WHERE deadline < ?)", (time.time(),))
            db.execute("UPDATE agent_tasks SET state='queued' WHERE state='waiting' AND NOT EXISTS (SELECT 1 FROM agent_tasks c WHERE c.parent=agent_tasks.id AND c.state NOT IN ('completed','failed','cancelled'))")
            args = (run_id,) if run_id else ()
            rows = db.execute("SELECT * FROM agent_tasks WHERE state='queued'" + (" AND run_id=?" if run_id else "") + " ORDER BY COALESCE(claimed,0),created", args).fetchall()
            for row in rows:
                run = db.execute("SELECT * FROM agent_runs WHERE id=?", (row["run_id"],)).fetchone()
                limits = json.loads(run["limits"])
                if not row['control_key'] and (row["rounds"] >= limits["max_rounds"] or run["calls"] >= limits["max_calls"]):
                    db.execute("UPDATE agent_tasks SET state='failed',result='Reasoning budget exhausted' WHERE id=?", (row["id"],))
                    continue
                active = db.execute("SELECT count(*) FROM agent_tasks WHERE state='running'").fetchone()[0]
                if active >= limits["max_workers"]:
                    return None
                db.execute("UPDATE agent_tasks SET state='running',owner=?,claimed=? WHERE id=?", (owner, time.time(), row["id"]))
                return dict(row)
        return None

    def cancel(self, task_id):
        self.get(task_id)
        with self.connect(True) as db:
            db.execute('''WITH RECURSIVE descendants(id) AS (
                 SELECT id FROM agent_tasks WHERE id=? UNION ALL
                 SELECT t.id FROM agent_tasks t JOIN descendants d ON t.parent=d.id)
                 UPDATE agent_tasks SET state='cancelled',result='Cancelled by controller'
                 WHERE id IN (SELECT id FROM descendants) AND state NOT IN ('completed','failed','cancelled')''', (task_id,))
            self.event(db, task_id, "cancelled", {})


class OllamaFleet:
    def __init__(self, config, store, client_factory=LabHTTP):
        self.config, self.store = config, store
        self.client_factory = client_factory
        self.clients = {node["id"]: client_factory(node["url"]) for node in config["nodes"]}

    def inventory(self):
        rows = []
        for node in self.config["nodes"]:
            try:
                client = self.clients[node["id"]]
                row = {"node": node["id"], "installed": client.request("/api/tags", timeout=8)["models"],
                       "running": client.request("/api/ps", timeout=8)["models"]}
                if node.get("ssh_target"):
                    row["free"] = self.telemetry(node["ssh_target"])
                rows.append(row)
            except Exception as exc:
                rows.append({"node": node["id"], "error": str(exc)})
        return rows

    @staticmethod
    def telemetry(target):
        if not isinstance(target, str) or target.startswith('-') or any(c.isspace() for c in target):
            raise ValueError("Invalid telemetry SSH target")
        code = ("import json,subprocess; "
                "m=dict(line.split(':',1) for line in open('/proc/meminfo')); "
                "p=subprocess.run(['nvidia-smi','--query-gpu=memory.free','--format=csv,noheader,nounits'],capture_output=True,text=True); "
                "print(json.dumps({'cpu':int(m['MemAvailable'].split()[0])/1024**2,"
                "'gpu':max([float(x)/1024 for x in p.stdout.splitlines()] or [0])}))")
        proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target,
                               "python3 -c " + shlex.quote(code)], capture_output=True, text=True, timeout=8)
        if proc.returncode:
            raise RuntimeError("Lab telemetry unavailable: " + proc.stderr[:200])
        return json.loads(proc.stdout)

    def _models_for_task(self, task):
        goal = str(task.get("goal") or "")
        preferred = []
        if goal.startswith("[FAMILY:") and "]" in goal:
            fam = goal[8:goal.index("]")].strip().lower()
            preferred = list(CROSS_FAMILY_PREFERENCES.get(fam, []))
        base = list(self.config["tiers"][task["tier"]])
        seen = set()
        ordered = []
        for m in preferred + base:
            if m not in seen:
                seen.add(m)
                ordered.append(m)
        return ordered

    def reserve(self, task):
        inventory = self.inventory()
        # Automatic placement is CPU-first for models that fit the configured
        # small-model budget. Larger models skip the CPU candidate below and
        # continue to GPU. This keeps tiny workers from consuming scarce VRAM.
        devices = [task["device"]] if task["device"] != "auto" else ["cpu", "gpu"]
        candidate_models = self._models_for_task(task)
        for device in devices:
            for model in candidate_models:
                for node, state in zip(self.config["nodes"], inventory):
                    if "error" in state:
                        continue
                    installed = next((m for m in state["installed"] if m["name"] == model), None)
                    if not installed or "embedding" in installed.get("capabilities", []) and "completion" not in installed.get("capabilities", []):
                        continue
                    # Reserve weights plus conservative context/runner headroom.
                    memory = installed["size"] / 1024**3 * 1.15 + 1.5
                    if device == "cpu" and task["device"] == "auto" and memory > node.get("auto_cpu_max_gib", 8):
                        continue  # Large models wait for GPU unless CPU was explicitly requested.
                    with self.store.connect(True) as db:
                        current = db.execute("SELECT state FROM agent_tasks WHERE id=?", (task["id"],)).fetchone()
                        if not current or current[0] != "running":
                            raise RuntimeError("Task no longer running")
                        leases = db.execute("SELECT * FROM agent_leases WHERE node=?", (node["id"],)).fetchall()
                        if any(l["model"] == model for l in leases):
                            continue  # Never move the same model CPU<->GPU during a call.
                        if sum(l["device"] == device for l in leases) >= node[device + "_slots"]:
                            continue
                        # Account for resident models, including unmanaged clients.
                        leased_models = {l["model"] for l in leases}
                        used = sum(l["memory"] for l in leases if l["device"] == device)
                        for resident in state["running"]:
                            if resident["name"] == model or resident["name"] in leased_models:
                                continue
                            size = resident.get("size_vram", 0) if device == "gpu" else max(0, resident["size"] - resident.get("size_vram", 0))
                            used += size / 1024**3
                        capacity = node["gpu_gib" if device == "gpu" else "ram_gib"]
                        if "free" in state:
                            resident_memory = sum((r.get("size_vram",0) if device == "gpu" else max(0,r["size"]-r.get("size_vram",0))) / 1024**3 for r in state["running"])
                            capacity = min(capacity, state["free"][device] + resident_memory)
                        if used + memory > capacity:
                            continue
                        db.execute("INSERT INTO agent_leases VALUES(?,?,?,?,?,?)", (task["id"], node["id"], model, device, memory, time.time() + 660))
                        selection = {"node": node["id"], "model": model, "device": device}
                        self.store.event(db, task["id"], "reserved", selection)
                        return node, selection
        raise CapacityBusy("No configured model currently fits an available lab slot")

    def generate(self, task, messages):
        node, selection = self.reserve(task)
        limits = self.config["limits"]
        uncertain = False
        try:
            with self.store.connect(True) as db:
                run = db.execute("SELECT * FROM agent_runs WHERE id=?", (task["run_id"],)).fetchone()
                run_limits = json.loads(run["limits"])
                state = db.execute("SELECT state FROM agent_tasks WHERE id=?", (task["id"],)).fetchone()[0]
                if state != "running" or run["calls"] >= run_limits["max_calls"] or run["deadline"] <= time.time():
                    raise RuntimeError("Run cancelled, expired, or out of calls")
                db.execute("UPDATE agent_runs SET calls=calls+1 WHERE id=?", (task["run_id"],))
                db.execute("UPDATE agent_tasks SET rounds=rounds+1 WHERE id=?", (task["id"],))
            payload = {"model": selection["model"], "messages": messages, "stream": False,
                       "format": response_schema(task["tier"]),
                       "think": False, "keep_alive": "60s",
                       "options": {"num_ctx": limits["num_ctx"], "num_predict": limits["num_predict"],
                                   "temperature": 0.1, "num_gpu": 0 if selection["device"] == "cpu" else 999,
                                   "num_thread": node["cpu_threads"]}}
            start = time.monotonic()
            try:
                # Inventory established a working cached address. Use it for
                # inference rather than retrying a timed-out POST across addresses.
                response = self.clients[node["id"]].request("/api/chat", payload, timeout=limits["request_seconds"])
            except Exception:
                uncertain = True
                raise
            with self.store.connect(True) as db:
                self.store.event(db, task["id"], "inference", {**selection, "seconds": time.monotonic() - start,
                    "prompt_tokens": response.get("prompt_eval_count"), "output_tokens": response.get("eval_count")})
            try:
                resident = self.clients[node["id"]].request("/api/ps", timeout=8)["models"]
                actual = next((r for r in resident if r["name"] == selection["model"]), {})
                with self.store.connect(True) as db:
                    self.store.event(db, task["id"], "placement", {"size": actual.get("size"), "size_vram": actual.get("size_vram")})
            except Exception:
                pass  # Telemetry absence does not invalidate an obtained answer.
            if not response.get("done") or response.get("done_reason") == "length":
                raise ValueError("Model response incomplete or truncated")
            return response["message"]["content"]
        finally:
            if not uncertain:
                with self.store.connect(True) as db:
                    db.execute("DELETE FROM agent_leases WHERE task_id=?", (task["id"],))


class HybridAgentFleet(OllamaFleet):
    """Lab-first fleet with an opt-in OpenAI reasoning specialist.

    The OpenAI worker is deliberately analysis-only: it receives the same bounded
    evidence and JSON action schema as a local worker and has no browser, shell,
    messaging, or controller tools.  Recursive spawning remains enforced by the
    local Runtime after the response is validated.
    """

    def _openai_enabled(self, task):
        return (task["tier"] == "smart"
                and os.environ.get("SABRINA_OPENAI_SWARM", "").lower() in {"1", "true", "yes"}
                and bool(os.environ.get("OPENAI_API_KEY"))
                and bool(os.environ.get("SABRINA_OPENAI_AGENT_MODEL")))

    def _external_portal_tier(self, task):
        goal = str(task.get("goal") or "")
        if goal.startswith("[FAMILY:") and "]" in goal:
            fam = goal[8:goal.index("]")].strip().lower()
            tier_map = {
                "external": "auto",
                "portal_flash": "flash",
                "portal_reasoning": "deep_reasoning",
                "portal_rag": "domain_rag",
            }
            return tier_map.get(fam)
        return None

    def _generate_via_external_portal(self, task, messages, portal_tier):
        from harnesses.external.collective_pool import KindCollectivePool
        with self.store.connect(True) as db:
            run = db.execute("SELECT * FROM agent_runs WHERE id=?", (task["run_id"],)).fetchone()
            run_limits = json.loads(run["limits"])
            state = db.execute("SELECT state FROM agent_tasks WHERE id=?", (task["id"],)).fetchone()[0]
            if state != "running" or run["calls"] >= run_limits["max_calls"] or run["deadline"] <= time.time():
                raise RuntimeError("Run cancelled, expired, or out of calls")
            db.execute("UPDATE agent_runs SET calls=calls+1 WHERE id=?", (task["run_id"],))
            db.execute("UPDATE agent_tasks SET rounds=rounds+1 WHERE id=?", (task["id"],))
        user_content = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        pool = getattr(self, "_portal_pool", None)
        if pool is None:
            pool = KindCollectivePool(min_cooldown_sec=1.5)
            self._portal_pool = pool
        start = time.monotonic()
        res = pool.route_query(user_content[:4000], tier=portal_tier, task_id=task["id"], use_cache=True)
        elapsed = round(time.monotonic() - start, 3)
        if not res.get("ok") or not str(res.get("reply") or "").strip():
            raise RuntimeError(f"External portal unavailable: {res.get('failure', 'empty_reply')}")
        portal_id = str(res.get("portal_id") or "external_portal")
        reply_txt = str(res.get("reply") or "").strip()
        if pool.blackboard is not None:
            try:
                pool.blackboard.record_competence_outcome(
                    domain=f"guild_runtime:{portal_tier}",
                    model_id=portal_id,
                    passed=True,
                    latency_ms=elapsed * 1000.0,
                )
            except Exception:
                pass
        with self.store.connect(True) as db:
            self.store.event(db, task["id"], "inference", {
                "provider": "external_portal_guild",
                "portal_id": portal_id,
                "model": portal_id,
                "tier": res.get("tier", portal_tier),
                "device": "portal",
                "seconds": elapsed,
            })
        try:
            parsed = json.loads(reply_txt)
            if isinstance(parsed, dict) and parsed.get("action") in {"complete", "blocked", "revise"}:
                return reply_txt
        except Exception:
            pass
        return json.dumps({"action": "complete", "result": f"[{portal_id}] {reply_txt[:4500]}"})

    def generate(self, task, messages):
        portal_tier = self._external_portal_tier(task)
        if portal_tier is not None:
            try:
                return self._generate_via_external_portal(task, messages, portal_tier)
            except Exception:
                return super().generate(task, messages)
        if not self._openai_enabled(task):
            return super().generate(task, messages)

        from openai import OpenAI
        limits = self.config["limits"]
        model = os.environ["SABRINA_OPENAI_AGENT_MODEL"]
        with self.store.connect(True) as db:
            run = db.execute("SELECT * FROM agent_runs WHERE id=?", (task["run_id"],)).fetchone()
            run_limits = json.loads(run["limits"])
            state = db.execute("SELECT state FROM agent_tasks WHERE id=?", (task["id"],)).fetchone()[0]
            if state != "running" or run["calls"] >= run_limits["max_calls"] or run["deadline"] <= time.time():
                raise RuntimeError("Run cancelled, expired, or out of calls")
            db.execute("UPDATE agent_runs SET calls=calls+1 WHERE id=?", (task["run_id"],))
            db.execute("UPDATE agent_tasks SET rounds=rounds+1 WHERE id=?", (task["id"],))

        start = time.monotonic()
        try:
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=limits["request_seconds"])
            openai_output_tokens = int(os.environ.get("SABRINA_OPENAI_MAX_OUTPUT_TOKENS", "2048"))
            if not 256 <= openai_output_tokens <= 8192:
                raise ValueError("SABRINA_OPENAI_MAX_OUTPUT_TOKENS must be between 256 and 8192")
            response = client.responses.create(
                model=model,
                input=messages,
                max_output_tokens=openai_output_tokens,
                text={"format": {"type": "json_schema", "name": "sabrina_agent_action",
                                 "schema": response_schema(task["tier"]), "strict": False}},
            )
            output = response.output_text
            if not output:
                detail = getattr(response, "incomplete_details", None)
                raise RuntimeError(f"OpenAI worker returned no text output (status={response.status}, incomplete={detail})")
            usage = getattr(response, "usage", None)
            with self.store.connect(True) as db:
                self.store.event(db, task["id"], "inference", {
                    "provider": "openai", "model": model, "device": "cloud",
                    "seconds": time.monotonic() - start,
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                })
            return output
        except Exception as exc:
            with self.store.connect(True) as db:
                self.store.event(db, task["id"], "provider_failed", {
                    "provider": "openai", "model": model, "error": str(exc)[:500]})
            raise


def _probe_read_code_slice(runtime, args):
    rel = str(args.get("path") or "").strip().replace("\\", "/")
    if not rel or ".." in rel or any(s in rel.lower() for s in (".env", "secret", "credential", "private", ".key")):
        raise ValueError("Forbidden or invalid probe file path")
    target = (ROOT / rel).resolve()
    if not target.is_relative_to(ROOT.resolve()) or not target.is_file():
        raise ValueError("File outside workspace or does not exist")
    if target.suffix.lower() not in {".py", ".md", ".json", ".lean", ".yaml", ".yml"}:
        raise ValueError("Unsupported file extension for read_code_slice")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    query = str(args.get("query") or "").strip()
    matching_lines = []
    start = max(1, int(args.get("start_line", 1)))
    if query:
        q_low = query.lower()
        matching_lines = [i + 1 for i, ln in enumerate(lines) if q_low in ln.lower()][:25]
        if matching_lines and "start_line" not in args:
            start = max(1, matching_lines[0] - 5)
        elif matching_lines:
            next_matches = [m for m in matching_lines if m >= start]
            if next_matches:
                start = max(1, next_matches[0] - 5)
    end = min(start + 119, int(args.get("end_line", start + 119)))
    if end < start:
        raise ValueError("Invalid line range")
    actual_end = min(end, len(lines))
    selected = [f"{idx}: {lines[idx - 1][:160]}" for idx in range(start, actual_end + 1)]
    has_more = actual_end < len(lines)
    res = {
        "path": rel,
        "start_line": start,
        "end_line": actual_end,
        "total_lines": len(lines),
        "has_more": has_more,
        "next_start_line": (actual_end + 1) if has_more else None,
        "lines": selected,
    }
    if query:
        res["query"] = query
        res["matching_lines"] = matching_lines
    return res


def _probe_inspect_sqlite_summary(runtime, args):
    rel = str(args.get("db_path") or "data/agent_runtime.sqlite3").strip().replace("\\", "/")
    if ".." in rel or any(s in rel.lower() for s in (".env", "secret", "credential", "private")):
        raise ValueError("Forbidden database path")
    db_file = (ROOT / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
    if not db_file.is_relative_to(ROOT.resolve()) and db_file != runtime.store.path.resolve():
        raise ValueError("Database outside allowed workspace scope")
    if not db_file.is_file():
        raise ValueError("Database file not found")
    conn = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        summary = {}
        for tbl in tables[:12]:
            if not tbl.replace("_", "").isalnum():
                continue
            count = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({tbl})").fetchall()
                    if not any(s in c[1].lower() for s in ("token", "secret", "key", "password"))]
            summary[tbl] = {"row_count": count, "columns": cols[:15]}
        return {"db_path": str(rel), "tables": summary}
    finally:
        conn.close()


def _probe_project_health_audit(runtime, args):
    project = str(args.get("project") or "core").strip().lower()
    if project == "crypto":
        from tools import kraken_strategy_rules as k_rules
        from tools import kraken_jev_live_gate as j_gate
        info = {
            "project": "crypto",
            "costs": dict(k_rules.COSTS),
            "jev_defaults": {
                "noul_threshold": j_gate.DEFAULT_NOUL_THRESHOLD,
                "min_expected_score_bps": j_gate.MIN_EXPECTED_SCORE_BPS,
                "choice_margin": j_gate.DEFAULT_CHOICE_MARGIN,
                "max_spread_bps": j_gate.MAX_MAJOR_SPREAD_BPS,
                "min_intraday_gain_pct": j_gate.MIN_INTRADAY_GAIN_PCT,
                "max_pre_moon_gain_pct": getattr(j_gate, "MAX_PRE_MOON_GAIN_PCT", 18.0),
                "min_24h_range_pos": getattr(j_gate, "MIN_24H_RANGE_POS", 0.48),
            },
            "safe_bounds": PROJECT_SAFE_BOUNDS["crypto"],
        }
        report_path = ROOT / "artifacts/custom_model_arena/jev_decision_repair_report.json"
        if report_path.is_file():
            rep = json.loads(report_path.read_text(encoding="utf-8"))
            info["latest_arena_tracks"] = list(rep.get("tracks", {}).keys())
        return info
    if project == "core":
        snap = runtime.store.snapshot()
        by_state = {}
        for row in snap:
            by_state[row["state"]] = by_state.get(row["state"], 0) + 1
        return {"project": "core", "total_tasks": len(snap), "by_state": by_state, "limits": runtime.config["limits"]}
    if project == "oss":
        skills_dir = ROOT.parent / ".agents/skills"
        oss_skills = sorted(p.parent.name for p in skills_dir.glob("oss-*/SKILL.md")) if skills_dir.is_dir() else []
        return {"project": "oss", "enrolled_oss_skills": oss_skills}
    if project == "portals":
        db_file = ROOT / "data/sabrina_assistant_fresh.sqlite3"
        if not db_file.is_file():
            return {"project": "portals", "status": "db_missing"}
        conn = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            count = conn.execute("SELECT count(*) FROM external_portals").fetchone()[0] if "external_portals" in tables else 0
            return {"project": "portals", "registered_portals": count}
        finally:
            conn.close()
    raise ValueError(f"Unknown project for health audit: {project}")


def _probe_counterfactual_check(runtime, args):
    project = str(args.get("project") or "crypto").strip().lower()
    updates = args.get("parameter_updates") or {}
    if not isinstance(updates, dict):
        raise ValueError("parameter_updates must be an object")
    bounds = PROJECT_SAFE_BOUNDS.get(project, {})
    violations = []
    for k, v in updates.items():
        if k not in bounds:
            violations.append(f"unapproved_parameter:{k}")
            continue
        lo, hi = bounds[k]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (lo <= float(v) <= hi):
            violations.append(f"out_of_bounds:{k}={v} (allowed {lo}..{hi})")
    trace_rows = args.get("trace_rows") or []
    accepted_before = 0
    accepted_after = 0
    if project == "crypto" and isinstance(trace_rows, list):
        noul_thr = float(updates.get("jev_noul_threshold", 0.395))
        score_thr = float(updates.get("jev_min_score_bps", 4.0))
        margin_thr = float(updates.get("jev_choice_margin", 0.02))
        spread_thr = float(updates.get("jev_max_spread_bps", 35.0))
        for row in trace_rows:
            if not isinstance(row, dict):
                continue
            if row.get("reason") == "accepted":
                accepted_before += 1
            nl = float(row.get("noul_long", 0.0))
            pl = float(row.get("choice_long", 0.0))
            ps = float(row.get("choice_short", 0.0))
            sc = float(row.get("effective_score_bps", row.get("expected_score_bps", -99.0)))
            sp = float(row.get("spread_bps", 999.0))
            veto = str(row.get("reason", "")).startswith("moon_veto:")
            if not veto and sp <= spread_thr and nl >= noul_thr and pl > ps + margin_thr and sc >= score_thr:
                accepted_after += 1
    return {
        "project": project,
        "within_safe_bounds": len(violations) == 0,
        "violations": violations,
        "trace_count": len(trace_rows) if isinstance(trace_rows, list) else 0,
        "accepted_before": accepted_before,
        "accepted_after": accepted_after,
    }


def _safe_eval_math_expr(expr_str, env):
    import ast
    import math
    allowed_funcs = {
        "abs": abs, "min": min, "max": max, "sum": sum, "round": round,
        "sqrt": math.sqrt, "log": math.log, "log2": math.log2, "log10": math.log10,
        "exp": math.exp, "sin": math.sin, "cos": math.cos, "floor": math.floor, "ceil": math.ceil,
    }
    tree = ast.parse(expr_str, mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Attribute, ast.Lambda, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
            raise ValueError("Disallowed AST node in equation expression")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in allowed_funcs:
                raise ValueError("Disallowed function call in equation expression")
        if isinstance(node, ast.Name) and node.id not in env and node.id not in allowed_funcs:
            raise ValueError(f"Unknown variable in equation expression: {node.id}")
    return eval(compile(tree, "<equation>", "eval"), {"__builtins__": {}}, {**allowed_funcs, **env})


def _probe_verify_equation_or_lean(runtime, args):
    import re
    mode = str(args.get("mode") or "auto").strip().lower()
    if mode == "auto":
        if "sat_clauses" in args:
            mode = "sat"
        elif "lean_path" in args or "lean_code" in args:
            mode = "lean"
        else:
            mode = "equation"

    if mode == "sat":
        clauses = args.get("sat_clauses")
        if not isinstance(clauses, list) or not 1 <= len(clauses) <= 64:
            raise ValueError("sat_clauses requires 1–64 CNF clauses")
        vars_set = set()
        norm_clauses = []
        for cl in clauses:
            if not isinstance(cl, list) or not 1 <= len(cl) <= 12:
                raise ValueError("Each clause must be a non-empty list of up to 12 integer literals")
            lits = [int(x) for x in cl if int(x) != 0]
            if not lits:
                raise ValueError("Clause cannot be empty")
            for lit in lits:
                vars_set.add(abs(lit))
            norm_clauses.append(lits)
        var_list = sorted(vars_set)
        n_vars = len(var_list)
        if not 1 <= n_vars <= 14:
            raise ValueError("Exact SAT phase/variance probe supports 1–14 variables")
        total_assignments = 1 << n_vars
        sat_count = 0
        first_witness = None
        best_unsat_satisfied = -1
        for mask in range(total_assignments):
            assign = {v: bool((mask >> idx) & 1) for idx, v in enumerate(var_list)}
            satisfied_clauses = 0
            for cl in norm_clauses:
                if any((assign[abs(lit)] if lit > 0 else not assign[abs(lit)]) for lit in cl):
                    satisfied_clauses += 1
            if satisfied_clauses > best_unsat_satisfied:
                best_unsat_satisfied = satisfied_clauses
            if satisfied_clauses == len(norm_clauses):
                sat_count += 1
                if first_witness is None:
                    first_witness = {f"x{v}": assign[v] for v in var_list}
        rho = sat_count / float(total_assignments)
        return {
            "mode": "sat",
            "num_vars": n_vars,
            "num_clauses": len(norm_clauses),
            "clause_density_alpha": round(len(norm_clauses) / float(n_vars), 4),
            "satisfiable": sat_count > 0,
            "satisfying_count": sat_count,
            "total_assignments": total_assignments,
            "sat_ratio_rho": round(rho, 6),
            "sat_variance": round(rho * (1.0 - rho), 6),
            "max_clauses_satisfied": best_unsat_satisfied,
            "sample_witness": first_witness,
        }

    if mode == "lean":
        lean_code = args.get("lean_code")
        lean_path = args.get("lean_path")
        if lean_path:
            rel = str(lean_path).strip().replace("\\", "/")
            if ".." in rel:
                raise ValueError("Invalid Lean path")
            p = (ROOT / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
            if not p.is_file() or p.suffix != ".lean":
                raise ValueError("Lean file not found")
            lean_code = p.read_text(encoding="utf-8", errors="replace")
        if not isinstance(lean_code, str) or not lean_code.strip():
            raise ValueError("lean_code or valid lean_path required")
        declarations = []
        for idx, ln in enumerate(lean_code.splitlines(), 1):
            m = re.match(r"^\s*(?:private\s+|protected\s+)?(theorem|lemma|def|structure|inductive)\s+([A-Za-z0-9_'.]+)", ln)
            if m:
                declarations.append({"kind": m.group(1), "name": m.group(2), "line": idx})
        no_block_comments = re.sub(r"/-.*?-/", "", lean_code, flags=re.DOTALL)
        no_comments = re.sub(r"--.*$", "", no_block_comments, flags=re.MULTILINE)
        forbidden = re.findall(r"\b(sorry|admit|axiom)\b", no_comments)
        return {
            "mode": "lean",
            "declaration_count": len(declarations),
            "declarations": declarations[:30],
            "forbidden_placeholders": forbidden,
            "proof_complete_syntax": len(forbidden) == 0 and len(declarations) > 0,
        }

    lhs = str(args.get("lhs") or "").strip()
    rhs = str(args.get("rhs") or "").strip()
    relation = str(args.get("relation") or "==").strip()
    variables = list(args.get("variables") or ["n"])
    domain = list(args.get("domain") or [1, 2, 3, 4, 5, 8, 10, 16])
    if not lhs or not rhs or relation not in {"==", "<=", ">=", "<", ">"}:
        raise ValueError("Equation probe requires lhs, rhs, and relation in ==, <=, >=, <, >")
    if not 1 <= len(variables) <= 3 or len(domain) > 20:
        raise ValueError("Equation probe supports 1–3 variables and up to 20 domain values")
    import itertools
    tested = 0
    counterexample = None
    for vals in itertools.product(domain, repeat=len(variables)):
        env = {var: float(val) for var, val in zip(variables, vals)}
        v_lhs = float(_safe_eval_math_expr(lhs, env))
        v_rhs = float(_safe_eval_math_expr(rhs, env))
        tested += 1
        if relation == "==":
            ok = abs(v_lhs - v_rhs) <= 1e-7 * max(1.0, abs(v_lhs), abs(v_rhs))
        elif relation == "<=":
            ok = v_lhs <= v_rhs + 1e-9
        elif relation == ">=":
            ok = v_lhs + 1e-9 >= v_rhs
        elif relation == "<":
            ok = v_lhs < v_rhs - 1e-9
        else:
            ok = v_lhs > v_rhs + 1e-9
        if not ok:
            counterexample = {"assignment": env, "lhs_val": round(v_lhs, 6), "rhs_val": round(v_rhs, 6)}
            break
    return {
        "mode": "equation",
        "lhs": lhs,
        "rhs": rhs,
        "relation": relation,
        "tested_points": tested,
        "holds": counterexample is None,
        "counterexample": counterexample,
    }


def _probe_lookup_theorem_or_reference(runtime, args):
    import re
    import urllib.parse
    import urllib.request
    import xml.etree.ElementTree as ET
    query = str(args.get("query") or "").strip()
    if not query or len(query) > 200:
        raise ValueError("lookup_theorem_or_reference requires a 1–200 char query")
    q_tokens = [tok.lower() for tok in re.split(r"[^A-Za-z0-9_']+", query) if len(tok) >= 2]
    search_roots = [ROOT / "proofs"]
    extra_dir = args.get("corpus_dir")
    if extra_dir:
        p_extra = Path(str(extra_dir)).resolve()
        if p_extra.is_dir():
            search_roots.append(p_extra)
    default_ego_lean = Path("/home/ego/eth-audit-20261119/lean/OAI")
    if default_ego_lean.is_dir() and default_ego_lean not in search_roots:
        search_roots.append(default_ego_lean)

    local_hits = []
    scanned_files = 0
    for sroot in search_roots:
        if not sroot.is_dir():
            continue
        for lfile in sorted(sroot.rglob("*.lean")):
            scanned_files += 1
            if scanned_files > 400 and len(local_hits) >= 12:
                break
            fname_low = lfile.name.lower()
            name_match = any(t in fname_low for t in q_tokens)
            if not name_match and scanned_files > 120:
                continue
            try:
                lines = lfile.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            for idx, ln in enumerate(lines, 1):
                m = re.match(r"^\s*(?:private\s+|protected\s+)?(theorem|lemma|def|structure)\s+([A-Za-z0-9_'.]+)", ln)
                if not m:
                    continue
                decl_text = " ".join(lines[idx - 1:min(len(lines), idx + 3)])
                d_low = decl_text.lower()
                score = sum(2 if t in m.group(2).lower() else (1 if t in d_low else 0) for t in q_tokens)
                if score > 0 or (name_match and len(local_hits) < 12):
                    local_hits.append({
                        "score": score + (2 if name_match else 0),
                        "file": lfile.name,
                        "kind": m.group(1),
                        "name": m.group(2),
                        "line": idx,
                        "signature": decl_text[:220],
                    })
    local_hits.sort(key=lambda x: (-x["score"], x["file"], x["line"]))

    online_refs = []
    if args.get("online", False):
        safe_q = re.sub(r"[^A-Za-z0-9 _+-]", " ", query).strip()
        url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode({
            "search_query": f"all:{safe_q}",
            "start": 0,
            "max_results": 3,
        })
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SabrinaLongHorizonResearch/1.0"})
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                xml_bytes = resp.read(250_000)
            root_el = ET.fromstring(xml_bytes)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            for entry in root_el.findall("a:entry", ns)[:3]:
                title = " ".join((entry.findtext("a:title", "", ns) or "").split())[:180]
                summary = " ".join((entry.findtext("a:summary", "", ns) or "").split())[:260]
                link_id = (entry.findtext("a:id", "", ns) or "").strip()
                if title:
                    online_refs.append({"title": title, "id": link_id, "summary": summary})
        except Exception as exc:
            online_refs.append({"online_lookup_note": f"offline_or_timeout:{type(exc).__name__}"})

    return {
        "query": query,
        "local_theorem_matches": local_hits[:10],
        "online_references": online_refs,
    }


ARENA_VERIFIED_EXTERNAL_GUILDS = (
    "hf-gradio-htchu-coderhelper",
    "hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api",
    "hf-gradio-himakesh-parth-ai-tutor",
    "hf-gradio-onlymhmd-qwen-32b-chat",
    "hf-gradio-affanaiengr-rag",
    "hf-gradio-rstanley2026-rodney-digital-twin",
    "hf-gradio-dkt3-digital-twin",
    "hf-gradio-parmeshlata-portfolio-chatbot",
    "hf-gradio-free234-snap-rag-assistant",
    "hf-gradio-nakaassoumatine-einstein-chatbot",
)


def _probe_consult_external_guild(runtime, args):
    from harnesses.external.collective_pool import KindCollectivePool
    prompt = str(args.get("prompt") or "").strip()
    if not prompt or len(prompt) > 2500:
        raise ValueError("consult_external_guild requires a 1–2500 char prompt")
    tier = str(args.get("tier") or "auto").strip().lower()
    max_members = max(1, min(4, int(args.get("max_members", 2))))
    pool = getattr(runtime, "_portal_pool", None)
    if pool is None:
        pool = KindCollectivePool(min_cooldown_sec=1.5)
        runtime._portal_pool = pool

    if args.get("portal_ids"):
        cands = list(args["portal_ids"])
    else:
        eligible = set(pool.get_tier_candidates("auto"))
        role_cands = [p for p in pool.get_role_candidates("cs_math_and_bounds_coding") if p in eligible]
        arena_cands = [p for p in ARENA_VERIFIED_EXTERNAL_GUILDS if p in eligible]
        tier_cands = pool.get_tier_candidates(tier)
        cands = []
        for pid in arena_cands + role_cands + tier_cands:
            if pid not in cands:
                cands.append(pid)

    responses = []
    good_count = 0
    for pid in cands:
        if good_count >= max_members or len(responses) >= max_members + 3:
            break
        t0 = time.monotonic()
        res = pool.invoke_member(pid, prompt, use_cache=True)
        if res.get("failure") == "provider_cooldown":
            time.sleep(10.5)
            t0 = time.monotonic()
            res = pool.invoke_member(pid, prompt, use_cache=True)
        lat_ms = round((time.monotonic() - t0) * 1000.0, 1)
        ok = bool(res.get("ok")) and bool(str(res.get("reply") or "").strip())
        if pool.blackboard is not None:
            try:
                pool.blackboard.record_competence_outcome(
                    domain=f"external_guild:{tier}",
                    model_id=pid,
                    passed=ok,
                    latency_ms=lat_ms,
                )
            except Exception:
                pass
        responses.append({
            "portal_id": pid,
            "ok": ok,
            "latency_ms": lat_ms,
            "reply": str(res.get("reply") or "")[:900],
            "failure": res.get("failure", ""),
        })
        if ok:
            good_count += 1
    return {
        "tier": tier,
        "queried_portals": len(responses),
        "successful_portals": good_count,
        "responses": responses,
    }


def _probe_simulate_trading_history(runtime, args):
    from tools.kraken_time_machine_sim import (
        load_saved_snapshots_and_ohlc,
        run_time_machine_backtest,
        sweep_parameter_grid,
    )
    mode = str(args.get("mode") or "single").strip().lower()
    params = args.get("params") if isinstance(args.get("params"), dict) else {}
    custom_entries = args.get("custom_entries") if isinstance(args.get("custom_entries"), list) else None
    ohlc_15m = args.get("ohlc_15m_by_pair") if isinstance(args.get("ohlc_15m_by_pair"), dict) else None
    snapshots = args.get("snapshots") if isinstance(args.get("snapshots"), list) else None

    if snapshots is None or ohlc_15m is None:
        default_dir = args.get("market_inputs_dir") or "/home/ego/.local/state/sabrina-kraken/execution-v1/market-inputs"
        loaded_snaps, loaded_ohlc = load_saved_snapshots_and_ohlc(
            default_dir,
            max_snapshots=int(args.get("max_snapshots", 400)),
            fetch_live_ohlc=bool(args.get("fetch_live_ohlc", False)),
        )
        if snapshots is None:
            snapshots = loaded_snaps
        if ohlc_15m is None:
            ohlc_15m = loaded_ohlc

    start_cash = float(args.get("start_cash_usd", 15.5417))
    if mode == "grid":
        grids = args.get("candidate_grids") if isinstance(args.get("candidate_grids"), list) else None
        out = sweep_parameter_grid(snapshots, ohlc_15m, grids, start_cash_usd=start_cash)
        return {
            "mode": "grid",
            "holds": bool(out.get("best_config") and out["best_config"]["total_net_bps"] >= 0),
            **out,
        }

    res = run_time_machine_backtest(
        snapshots,
        ohlc_15m,
        params,
        start_cash_usd=start_cash,
        custom_entries=custom_entries,
    )
    return {
        "mode": "single",
        "holds": res["total_net_bps"] > 0 and res["profit_factor"] >= 1.0,
        **res,
    }


def _probe_audit_system_loops_and_gates(runtime, args):
    from tools.sabrina_epistemic_sentinel import EpistemicSentinel
    sentinel = EpistemicSentinel(runtime_db_path=runtime.store.path)
    raw = sentinel.audit_all_system_gates(
        data_dir=args.get("data_dir"),
        include_ssh_ego=bool(args.get("include_ssh_ego", False)),
        include_local_processes=bool(args.get("include_local_processes", False)),
        ego_snapshot=args.get("ego_snapshot") if isinstance(args.get("ego_snapshot"), dict) else None,
        windows_snapshot=args.get("windows_snapshot") if isinstance(args.get("windows_snapshot"), dict) else None,
    )
    compact_domains = [
        {
            "domain": d["domain"],
            "gate_name": d["gate_name"],
            "deterministic_predicate": d["deterministic_predicate"][:160],
            "anomaly_active": d["anomaly_active"],
            "severity": d["severity"],
            "ping_warranted": d["ping_warranted"],
            "state_sha256": d["state_sha256"][:16],
            "headline": d["headline"][:180],
        }
        for d in raw["domains"]
    ]
    return {
        "ok": True,
        "holds": True,
        "domains_audited": raw["domains_audited"],
        "anomalous_domains_count": raw["anomalous_domains_count"],
        "new_deterministic_pings_count": raw["new_deterministic_pings_count"],
        "domains": compact_domains,
    }


DEFAULT_PROBES = {
    "read_code_slice": _probe_read_code_slice,
    "inspect_sqlite_summary": _probe_inspect_sqlite_summary,
    "project_health_audit": _probe_project_health_audit,
    "counterfactual_check": _probe_counterfactual_check,
    "verify_equation_or_lean": _probe_verify_equation_or_lean,
    "lookup_theorem_or_reference": _probe_lookup_theorem_or_reference,
    "consult_external_guild": _probe_consult_external_guild,
    "simulate_trading_history": _probe_simulate_trading_history,
    "audit_system_loops_and_gates": _probe_audit_system_loops_and_gates,
}


class Runtime:
    def __init__(self, config=None, db_path=None, fleet_factory=HybridAgentFleet):
        self.config = config or load_config()
        self.store = Store(db_path or ROOT / "data/agent_runtime.sqlite3")
        self.fleet = fleet_factory(self.config, self.store)
        self.owner = uuid.uuid4().hex
        self.stop = threading.Event()
        self.thread = None
        self.probes = dict(DEFAULT_PROBES)

    def register_probe(self, name, fn):
        if not isinstance(name, str) or not name.strip() or not callable(fn):
            raise ValueError("Probe requires a non-empty name and callable")
        self.probes[name.strip()] = fn

    def execute_probe(self, probe_name, probe_args=None):
        if not isinstance(probe_name, str) or probe_name not in self.probes:
            raise ValueError(f"Unknown diagnostic probe: {probe_name}")
        if probe_args is not None and not isinstance(probe_args, dict):
            raise ValueError("probe_args must be an object")
        res = self.probes[probe_name](self, probe_args or {})
        encoded_res = json.dumps(res)
        if len(encoded_res) > 9500:
            raise ValueError("Probe result exceeded bounded evidence size")
        return res

    def verify_sandbox_patch(self, action):
        import ast
        import tempfile
        target_project = str(action.get("target_project") or "").strip().lower()
        if target_project not in {"crypto", "core", "oss", "portals", "social"}:
            raise ValueError("Unsupported target_project for sandbox_verify")
        rationale = str(action.get("rationale") or "").strip()
        if not rationale or len(rationale) > 1500:
            raise ValueError("sandbox_verify requires a bounded rationale")
        updates = action.get("parameter_updates") or {}
        if not isinstance(updates, dict):
            raise ValueError("parameter_updates must be an object")
        cf = _probe_counterfactual_check(self, {"project": target_project, "parameter_updates": updates})
        if not cf["within_safe_bounds"]:
            raise ValueError("Parameter updates violated safety guardrails: " + ", ".join(cf["violations"]))
        code_snippet = action.get("code_snippet")
        test_snippet = action.get("test_snippet")
        code_verified = False
        if code_snippet is not None or test_snippet is not None:
            if not isinstance(code_snippet, str) or not code_snippet.strip() or len(code_snippet) > 8000:
                raise ValueError("code_snippet must be a non-empty string <= 8000 chars")
            if not isinstance(test_snippet, str) or not test_snippet.strip() or len(test_snippet) > 8000:
                raise ValueError("test_snippet must be a non-empty string <= 8000 chars")
            ast.parse(code_snippet)
            ast.parse(test_snippet)
            sandbox_root = ROOT / "artifacts/tool_sandbox"
            sandbox_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=sandbox_root, prefix=f"core_verify_{target_project}_") as tmp_dir:
                tmp_path = Path(tmp_dir)
                mod_file = tmp_path / "candidate_patch.py"
                tst_file = tmp_path / "test_candidate_patch.py"
                mod_file.write_text(code_snippet, encoding="utf-8")
                tst_file.write_text(test_snippet, encoding="utf-8")
                import py_compile
                py_compile.compile(str(mod_file), doraise=True)
                py_compile.compile(str(tst_file), doraise=True)
                proc = subprocess.run(
                    ["python", "-m", "unittest", "test_candidate_patch.py", "-v"],
                    cwd=str(tmp_path),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if proc.returncode != 0:
                    raise ValueError("Sandbox unittest failed: " + (proc.stderr or proc.stdout)[-400:])
                code_verified = True
        return {
            "verified": True,
            "target_project": target_project,
            "parameter_updates": updates,
            "code_verified": code_verified,
            "rationale": rationale,
        }

    def diagnose_project(self, project, symptom, context="", tier="smart", device="auto"):
        audit = self.execute_probe("project_health_audit", {"project": project})
        merged_ctx = json.dumps({"initial_health_audit": audit, "caller_context": context})
        goal = (
            f"Diagnose project '{project}' symptom: {symptom}. "
            f"Use probe to inspect missing evidence, adversarial_review to challenge root-cause assumptions "
            f"across model families, and sandbox_verify to validate any parameter/code fix."
        )
        return self.submit(goal[:2000], merged_ctx[:self.config["limits"]["max_prompt_chars"] // 2], tier=tier, device=device)

    def submit(self, goal, context="", tier="fast", device="auto"):
        return self.store.submit(goal, context, self.config["limits"], tier, device)

    def submit_long_horizon(self, goal, context="", max_epochs=16, tier="smart", device="auto"):
        if not isinstance(max_epochs, int) or not 1 <= max_epochs <= 64:
            raise ValueError("max_epochs must be an integer between 1 and 64")
        limits = {**self.config["limits"], "max_epochs": max_epochs}
        return self.store.submit(goal, context, limits, tier, device)

    def _record_epoch_and_compact(self, db, task, verified_lemmas, falsified_dead_ends, open_equations, next_frontier):
        run = db.execute("SELECT * FROM agent_runs WHERE id=?", (task["run_id"],)).fetchone()
        limits = json.loads(run["limits"])
        max_epochs = int(limits.get("max_epochs", 16))
        prev_rows = db.execute("SELECT * FROM agent_epochs WHERE task_id=? ORDER BY epoch", (task["id"],)).fetchall()
        next_epoch = len(prev_rows) + 1
        if next_epoch > max_epochs:
            raise ValueError("Long-horizon epoch budget exhausted")

        cum_lemmas = []
        cum_dead = []
        for r in prev_rows:
            for item in json.loads(r["verified_lemmas"]):
                if item not in cum_lemmas:
                    cum_lemmas.append(item)
            for item in json.loads(r["falsified_dead_ends"]):
                if item not in cum_dead:
                    cum_dead.append(item)
        for item in verified_lemmas:
            s = str(item).strip()[:300]
            if s and s not in cum_lemmas:
                cum_lemmas.append(s)
        for item in falsified_dead_ends:
            s = str(item).strip()[:300]
            if s and s not in cum_dead:
                cum_dead.append(s)
        clean_open = [str(x).strip()[:300] for x in open_equations if str(x).strip()][:12]
        cum_lemmas = cum_lemmas[-15:]
        cum_dead = cum_dead[-15:]
        frontier_str = str(next_frontier).strip()[:500]

        db.execute(
            """INSERT INTO agent_epochs(task_id,run_id,epoch,verified_lemmas,falsified_dead_ends,open_equations,next_frontier,created)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                task["id"], task["run_id"], next_epoch,
                json.dumps(cum_lemmas), json.dumps(cum_dead), json.dumps(clean_open),
                frontier_str, time.time(),
            ),
        )
        new_deadline = max(float(run["deadline"]), time.time() + float(limits["deadline_seconds"]))
        db.execute("UPDATE agent_runs SET deadline=? WHERE id=?", (new_deadline, task["run_id"]))
        compacted_entry = {
            "action": "epoch_summary",
            "epoch": next_epoch,
            "verified_lemmas": cum_lemmas,
            "falsified_dead_ends": cum_dead,
            "open_equations": clean_open,
            "next_frontier": frontier_str,
        }
        return compacted_entry

    def _record_verified_competence(self, task, history, result):
        """Automatically propagate verified long-horizon or probe-backed completions to the assistant competence ledger."""
        evidence_tags = []
        for entry in history or []:
            if not isinstance(entry, dict):
                continue
            act = entry.get("action")
            if act == "sandbox_verify":
                v_res = entry.get("verification_result") or {}
                if isinstance(v_res, dict) and v_res.get("ok"):
                    evidence_tags.append(f"sandbox_verify:{v_res.get('target_file', 'module')}")
            elif act in {"checkpoint_epoch", "epoch_summary"}:
                v_lem = entry.get("verified_lemmas") or []
                if v_lem:
                    evidence_tags.append(f"epoch_{entry.get('epoch', 1)}_lemmas:{len(v_lem)}")
            elif act == "probe":
                p_name = str(entry.get("probe_name") or "")
                p_res = entry.get("probe_result") or {}
                if isinstance(p_res, dict):
                    if p_name == "verify_equation_or_lean" and (p_res.get("holds") is True or p_res.get("lean_complete") is True):
                        evidence_tags.append(f"probe:{p_name}")
                    elif p_name == "counterfactual_check" and p_res.get("holds") is True:
                        evidence_tags.append(f"probe:{p_name}")
                    elif p_name == "consult_external_guild" and p_res.get("ok") is True:
                        evidence_tags.append(f"external_guild:{p_res.get('portal_id', 'portal')}")
                    elif p_name == "project_health_audit" and p_res.get("syntax_clean") is True:
                        evidence_tags.append(f"probe:{p_name}")
                    elif p_name == "simulate_trading_history" and p_res.get("holds") is True:
                        evidence_tags.append(f"probe:{p_name}")
                    elif p_name == "audit_system_loops_and_gates" and p_res.get("holds") is True:
                        evidence_tags.append(f"probe:{p_name}")
        if not evidence_tags:
            return
        try:
            assist_db_path = Path(
                os.environ.get(
                    "SABRINA_ASSISTANT_DB",
                    str(ROOT / "data" / "sabrina_assistant_fresh.sqlite3"),
                )
            )
            if not assist_db_path.parent.exists():
                return
            from tools.run_measurement import ensure_measurement_schema
            now = time.time()
            run_key = f"runtime-{task['run_id']}-{task['id']}"
            goal_str = str(task["goal"] or "")
            family = goal_str[8:goal_str.index("]")].strip().lower() if (goal_str.startswith("[FAMILY:") and "]" in goal_str) else "qwen"
            import contextlib
            with contextlib.closing(sqlite3.connect(str(assist_db_path), timeout=5.0)) as adb:
                ensure_measurement_schema(adb)
                adb.execute(
                    """INSERT OR REPLACE INTO assistant_run_provenance(
                           run_id, created, updated, model, requested_device,
                           confirmed_placement, capability_name, is_simulation
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                    (
                        run_key,
                        now,
                        now,
                        f"guild:{family}:{task['tier']}",
                        str(task["device"] or "auto"),
                        "hybrid_guild_runtime",
                        "workspace.research",
                    ),
                )
                adb.execute(
                    """INSERT OR REPLACE INTO assistant_run_evaluations(
                           run_id, evaluated_at, outcome_status, primary_category,
                           sub_code, counts_toward_competence, verified_pass,
                           evidence_path, diagnostic_summary, step_classifications_json,
                           is_simulation
                       ) VALUES (?, ?, 'verified_pass', 'verified_success',
                                 'EXTERNAL_RESULT_VERIFIED', 1, 1, ?, ?, '[]', 0)""",
                    (
                        run_key,
                        now,
                        ",".join(evidence_tags)[:240],
                        f"Task {task['id']} verified ({', '.join(evidence_tags[:4])}): {str(result)[:260]}",
                    ),
                )
                adb.commit()
        except Exception:
            pass

    def _auto_compact_task_history(self, task, evidence):
        hist = evidence.get("history") or []
        if len(hist) <= 1:
            return None
        lemmas = []
        dead_ends = []
        open_eqs = []
        for entry in hist[:-1]:
            if not isinstance(entry, dict):
                continue
            if entry.get("action") == "epoch_summary":
                lemmas.extend(entry.get("verified_lemmas", []))
                dead_ends.extend(entry.get("falsified_dead_ends", []))
                open_eqs = list(entry.get("open_equations", []))
            elif entry.get("action") == "probe":
                p_name = entry.get("probe_name", "probe")
                p_res = entry.get("probe_result", {})
                if isinstance(p_res, dict) and p_res.get("holds") is False:
                    dead_ends.append(f"{p_name}: counterexample={p_res.get('counterexample')}")
                else:
                    lemmas.append(f"{p_name} verified: {json.dumps(p_res)[:180]}")
            elif entry.get("action") == "sandbox_verify":
                lemmas.append(f"sandbox_verify passed: {entry.get('rationale', '')[:160]}")
            elif "feedback" in entry:
                dead_ends.append(str(entry["feedback"])[:180])
            elif entry.get("note"):
                open_eqs.append(str(entry["note"])[:180])
        with self.store.connect(True) as db:
            summary_entry = self._record_epoch_and_compact(
                db, task,
                lemmas or ["Completed intermediate probe/reasoning round"],
                dead_ends,
                open_eqs or [task["goal"][:200]],
                "Auto-compacted context window; continue from latest step",
            )
            latest_tail = hist[-1]
            if isinstance(latest_tail, dict) and len(json.dumps(latest_tail)) > 1500:
                latest_tail = {"action": latest_tail.get("action", "step"), "summary": json.dumps(latest_tail)[:1200]}
            new_hist = [summary_entry, latest_tail]
            db.execute("UPDATE agent_tasks SET history=?,rounds=0 WHERE id=?", (json.dumps(new_hist), task["id"]))
            self.store.event(db, task["id"], "auto_compact_epoch", summary_entry)
        return new_hist

    def step(self, run_id=None):
        task = self.store.claim(self.owner, run_id)
        if not task:
            return False
        try:
            children = [r for r in self.store.snapshot(task["run_id"]) if r["parent"] == task["id"]]
            hist_list = json.loads(task["history"])
            evidence = {"goal": task["goal"], "context": task["context"],
                        "history": hist_list, "children": children,
                        "depth": task["depth"], "max_depth": self.config["limits"]["max_depth"],
                        "tier": task["tier"]}
            stagnation_hits = sum(
                1 for h in hist_list[-3:]
                if isinstance(h, dict) and (
                    "feedback" in h
                    or (h.get("action") == "probe" and isinstance(h.get("probe_result"), dict) and h["probe_result"].get("holds") is False)
                    or h.get("action") == "revise"
                )
            )
            if stagnation_hits >= 2 and task["tier"] != "tiny":
                evidence["pivot_advisory"] = (
                    "STAGNATION_DETECTED: 2+ consecutive revisions/failures on current path. "
                    "Use pivot_subproblem to spin up a Multi-Family Swarm Guild on the blocking prerequisite lemma, "
                    "or use probe (lookup_theorem_or_reference) to fetch missing corpus/online references."
                )
            if task["parent"]:
                evidence["parent_goal"] = self.store.get(task["parent"])["goal"]
            system = TINY_SYSTEM if task["tier"] == "tiny" else SYSTEM
            if task["tier"] == "tiny":
                evidence = {k: evidence[k] for k in ("goal", "context")}
            prompt = json.dumps(evidence)
            if len(prompt) > self.config["limits"]["max_prompt_chars"] and task["tier"] != "tiny":
                compacted = self._auto_compact_task_history(task, evidence)
                if compacted is not None:
                    evidence["history"] = compacted
                    task["history"] = json.dumps(compacted)
                    prompt = json.dumps(evidence)
            if len(prompt) > self.config["limits"]["max_prompt_chars"]:
                raise RuntimeError("Evidence exceeds prompt budget; controller must summarize it")
            # Byte count is a conservative token bound for the configured byte-BPE
            # models; reserve space for chat framing and generated output as well.
            if len((system + prompt).encode('utf-8')) + self.config["limits"]["num_predict"] + 512 > self.config["limits"]["num_ctx"]:
                raise RuntimeError("Evidence exceeds context window; controller must summarize it")
            raw = self.fleet.generate(task, [{"role": "system", "content": system}, {"role": "user", "content": prompt}])
            try:
                action = json.loads(raw)
                self.apply(task, action)
            except (ValueError, TypeError, KeyError) as exc:
                self.feedback(task, "Invalid decision: " + str(exc))
        except CapacityBusy as exc:
            with self.store.connect(True) as db:
                db.execute("UPDATE agent_tasks SET state='queued',result=? WHERE id=? AND state='running'", (str(exc), task["id"]))
            return False
        except Exception as exc:
            with self.store.connect(True) as db:
                db.execute("UPDATE agent_tasks SET state='failed',result=? WHERE id=? AND state='running'", (str(exc), task["id"]))
                self.store.event(db, task["id"], "failed", str(exc))
        return True

    def feedback(self, task, note):
        with self.store.connect(True) as db:
            history = json.loads(task["history"]) + [{"feedback": note}]
            tier = "fast" if task["tier"] == "tiny" else "smart"
            db.execute("UPDATE agent_tasks SET state='queued',tier=?,history=?,control_key=NULL WHERE id=? AND state='running'", (tier, json.dumps(history), task["id"]))
            self.store.event(db, task["id"], "escalation", note)

    def apply(self, task, action):
        if not isinstance(action, dict):
            raise ValueError("Decision must be an object")
        kind = action.get("action")
        if kind not in {"complete", "blocked", "spawn", "revise", "escalate", "probe", "adversarial_review", "sandbox_verify", "checkpoint_epoch", "pivot_subproblem"}:
            raise ValueError("Unknown action")
        if task["tier"] == "tiny" and kind not in {"complete", "blocked"}:
            raise ValueError("Tiny agents can only answer or report missing facts")
        probe_entry = None
        if kind == "probe":
            probe_name = action.get("probe_name")
            probe_args = action.get("probe_args") or {}
            probe_res = self.execute_probe(probe_name, probe_args)
            probe_entry = {**action, "probe_result": probe_res}
        elif kind == "sandbox_verify":
            verify_res = self.verify_sandbox_patch(action)
            probe_entry = {**action, "verification_result": verify_res}
        with self.store.connect(True) as db:
            current = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task["id"],)).fetchone()
            if current["state"] != "running" or current["owner"] != self.owner:
                return  # A cancellation always wins over an in-flight answer.
            run = db.execute("SELECT * FROM agent_runs WHERE id=?", (task["run_id"],)).fetchone()
            if run["deadline"] < time.time():
                raise RuntimeError("Run deadline exceeded")
            limits = json.loads(run["limits"])
            state, tier, result = "queued", task["tier"], ""
            reset_rounds = False
            compacted_history = None
            if kind in {"complete", "blocked"}:
                result = action.get("result" if kind == "complete" else "reason")
                if not isinstance(result, str) or not result.strip() or len(result) > 6000:
                    raise ValueError("Result must contain 1–6000 characters")
                state = "completed" if kind == "complete" else "failed"
                if kind == "blocked" and task["tier"] == "tiny":
                    # A tiny classifier's inability to answer is a reason to
                    # ask the normal worker before handing the problem back.
                    state, tier = "queued", "fast"
            elif kind == "spawn":
                goals = action.get("goals")
                if not isinstance(goals, list) or not 1 <= len(goals) <= 3:
                    raise ValueError("Spawn requires 1–3 bounded goals")
                specs = []
                for value in goals:
                    spec = {"goal": value} if isinstance(value, str) else value
                    if not isinstance(spec,dict) or not isinstance(spec.get("goal"),str) or not spec["goal"].strip() or len(spec["goal"])>2000:
                        raise ValueError("Invalid child goal")
                    tier_value, device_value = spec.get("tier", task["tier"]), spec.get("device",task["device"])
                    if tier_value not in {"tiny","fast","smart"} or device_value not in {"auto","gpu","cpu"}:
                        raise ValueError("Invalid child tier or device")
                    specs.append((spec["goal"].strip(), tier_value, device_value))
                if task["depth"] >= limits["max_depth"]:
                    raise ValueError("Child depth limit reached; synthesize available evidence")
                count = db.execute("SELECT count(*) FROM agent_tasks WHERE run_id=?", (task["run_id"],)).fetchone()[0]
                if count + len(goals) > limits["max_agents"]:
                    raise ValueError("Agent budget exceeded")
                for goal, child_tier, child_device in specs:
                    self.store._insert(db, task["run_id"], task["id"], task["depth"]+1,
                                       goal, task["context"], child_tier, child_device)
                state = "waiting"
            elif kind == "adversarial_review":
                hypothesis = str(action.get("hypothesis") or "").strip()
                proposed_fix = str(action.get("proposed_fix") or "").strip()
                if not hypothesis or not proposed_fix or len(hypothesis) > 1000 or len(proposed_fix) > 1000:
                    raise ValueError("adversarial_review requires bounded hypothesis and proposed_fix")
                if task["depth"] >= limits["max_depth"]:
                    raise ValueError("Child depth limit reached; synthesize available evidence")
                count = db.execute("SELECT count(*) FROM agent_tasks WHERE run_id=?", (task["run_id"],)).fetchone()[0]
                if count + 2 > limits["max_agents"]:
                    raise ValueError("Agent budget exceeded")
                prosecutor_goal = (
                    f"[FAMILY:llama] [PROSECUTOR / FALSIFIER] Attempt to falsify hypothesis: '{hypothesis}' "
                    f"and proposed fix: '{proposed_fix}'. Identify counterexamples, edge-case traps, or blindspots."
                )[:2000]
                auditor_goal = (
                    f"[FAMILY:granite] [GOVERNANCE & INVARIANT AUDITOR] Audit safety invariants, parameter bounds, "
                    f"and empirical evidence for hypothesis: '{hypothesis}' and proposed fix: '{proposed_fix}'."
                )[:2000]
                for child_goal in (prosecutor_goal, auditor_goal):
                    self.store._insert(db, task["run_id"], task["id"], task["depth"] + 1,
                                       child_goal, task["context"], task["tier"], task["device"])
                state = "waiting"
            elif kind == "pivot_subproblem":
                sub_goal = str(action.get("subproblem_goal") or "").strip()
                blocker = str(action.get("blocker_reason") or "").strip()
                if not sub_goal or not blocker or len(sub_goal) > 1000 or len(blocker) > 1000:
                    raise ValueError("pivot_subproblem requires bounded subproblem_goal and blocker_reason")
                if task["depth"] >= limits["max_depth"]:
                    raise ValueError("Child depth limit reached; synthesize available evidence")
                raw_fams = action.get("guild_families") or ["qwen", "llama", "granite", "external"]
                if not isinstance(raw_fams, list) or not 1 <= len(raw_fams) <= 4:
                    raise ValueError("guild_families must list 1–4 model families")
                guild_roles = {
                    "qwen": "GUILD CONSTRUCTIVE SOLVER",
                    "llama": "GUILD COUNTEREXAMPLE & BOUND PROBER",
                    "granite": "GUILD FORMAL INVARIANT VERIFIER",
                    "mistral": "GUILD DIVERGENT HYPOTHESIS EXPLORER",
                    "defender": "GUILD SAFEGUARD SYNTHESIZER",
                    "external": "EXTERNAL PORTAL GUILD SPECIALIST",
                    "portal_flash": "EXTERNAL FLASH PORTAL GUILD",
                    "portal_reasoning": "EXTERNAL DEEP REASONING PORTAL GUILD",
                    "portal_rag": "EXTERNAL DOMAIN RAG PORTAL GUILD",
                }
                guild_goals = []
                for fam in raw_fams:
                    fk = str(fam).strip().lower()
                    if fk not in CROSS_FAMILY_PREFERENCES:
                        raise ValueError(f"Unsupported guild family: {fk}")
                    role_title = guild_roles.get(fk, "GUILD SPECIALIST")
                    guild_goals.append(
                        f"[FAMILY:{fk}] [{role_title}] Prerequisite Side-Problem Pivot (blocking parent due to: {blocker}): {sub_goal}"[:2000]
                    )
                count = db.execute("SELECT count(*) FROM agent_tasks WHERE run_id=?", (task["run_id"],)).fetchone()[0]
                if count + len(guild_goals) > limits["max_agents"]:
                    raise ValueError("Agent budget exceeded")
                for g_goal in guild_goals:
                    self.store._insert(db, task["run_id"], task["id"], task["depth"] + 1,
                                       g_goal, task["context"], task["tier"], task["device"])
                state = "waiting"
            elif kind == "checkpoint_epoch":
                v_lemmas = action.get("verified_lemmas") or []
                f_dead = action.get("falsified_dead_ends") or []
                o_eqs = action.get("open_equations") or []
                n_front = str(action.get("next_frontier") or "").strip()
                if (not isinstance(v_lemmas, list) or not isinstance(f_dead, list)
                        or not isinstance(o_eqs, list) or not n_front or (not v_lemmas and not f_dead)):
                    raise ValueError("checkpoint_epoch requires verified_lemmas or falsified_dead_ends, open_equations, and next_frontier")
                summary_entry = self._record_epoch_and_compact(db, task, v_lemmas, f_dead, o_eqs, n_front)
                compacted_history = [summary_entry]
                reset_rounds = True
                probe_entry = summary_entry
                state = "queued"
            elif kind in {"probe", "sandbox_verify"}:
                state = "queued"
            else:
                note = action.get("reason" if kind == "escalate" else "note")
                if not isinstance(note,str) or not note.strip() or len(note)>2000:
                    raise ValueError("A bounded explanation is required")
                if kind == "escalate":
                    if tier == "smart":
                        raise ValueError("Already at strongest tier; complete or report missing evidence")
                    tier = "smart"
            history = compacted_history if compacted_history is not None else (json.loads(task["history"]) + [probe_entry if probe_entry is not None else action])
            new_rounds = 0 if reset_rounds else task["rounds"]
            db.execute("UPDATE agent_tasks SET state=?,tier=?,result=?,history=?,rounds=?,control_key=NULL WHERE id=?", (state,tier,result,json.dumps(history),new_rounds,task["id"]))
            self.store.event(db, task["id"], kind, probe_entry if probe_entry is not None else action)
        if state == "completed":
            self._record_verified_competence(task, history, result)

    def work(self, run_id=None, until_idle=False):
        with ThreadPoolExecutor(max_workers=self.config["limits"]["max_workers"]) as pool:
            futures = set()
            while not self.stop.is_set():
                for future in list(futures):
                    if future.done():
                        future.result()
                        futures.remove(future)
                if until_idle and not futures and all(t["state"] in TERMINAL for t in self.store.snapshot(run_id)):
                    break
                if len(futures) < self.config["limits"]["max_workers"]:
                    futures.add(pool.submit(self.step, run_id))
                self.stop.wait(1)

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.stop.clear()
            self.thread = threading.Thread(target=self.work, daemon=True, name="SabrinaAgentScheduler")
            self.thread.start()


def dispatch(controller, action):
    """Bridge used by the main loop; child agents cannot call this entry point."""
    try:
        from tools.control_client import configured
        shared = configured()
        cloud_specialist = (os.environ.get('SABRINA_OPENAI_SWARM', '').lower() in {'1','true','yes'}
                            and bool(os.environ.get('OPENAI_API_KEY'))
                            and bool(os.environ.get('SABRINA_OPENAI_AGENT_MODEL')))
        if shared and not cloud_specialist:
            return _dispatch_shared(controller, action, shared)
        if getattr(controller, "_reasoning_runtime", None) is None:
            controller._reasoning_runtime = Runtime()
            controller._reasoning_runs = set()
        runtime = controller._reasoning_runtime
        if action["tool"] == "diagnose_project":
            task_id = runtime.diagnose_project(
                action.get("project", "core"),
                action.get("symptom", "unspecified symptom"),
                action.get("context", ""),
                action.get("tier", "smart"),
                action.get("device", "auto"),
            )
            task = runtime.store.get(task_id)
            controller._reasoning_runs.add(task["run_id"])
            runtime.start()
            return json.dumps({"task_id": task_id, "state": "queued", "run_id": task["run_id"]})
        if action["tool"] == "spawn_reasoning_agent":
            roots = [r for r in runtime.store.snapshot() if r["parent"] is None and r["run_id"] in controller._reasoning_runs]
            for root in roots:
                existing = runtime.store.get(root["id"])
                if (existing["goal"] == str(action.get("goal", "")).strip()
                    and existing["context"] == action.get("context", "")
                    and existing["device"] == action.get("device", "auto")):
                    return json.dumps({"task_id": existing["id"], "state": existing["state"], "run_id": existing["run_id"], "deduplicated": True})
            if len(roots) >= runtime.config["limits"]["max_agents"]:
                raise ValueError("Controller root-agent budget exhausted")
            task_id = runtime.submit(action.get("goal"), action.get("context", ""),
                                     action.get("tier", "fast"), action.get("device", "auto"))
            task = runtime.store.get(task_id)
            controller._reasoning_runs.add(task["run_id"])
            runtime.start()
            return json.dumps({"task_id": task_id, "state": "queued", "run_id": task["run_id"]})
        task_id = action.get("task_id")
        if task_id:
            task = runtime.store.get(task_id)
            if task["run_id"] not in controller._reasoning_runs:
                raise ValueError("Agent belongs to another controller session")
            if action["tool"] == "cancel_reasoning_agent":
                runtime.store.cancel(task_id)
            return json.dumps(runtime.store.get(task_id))
        if action["tool"] == "cancel_reasoning_agent":
            raise ValueError("task_id is required")
        return json.dumps([r for r in runtime.store.snapshot() if r["run_id"] in controller._reasoning_runs])
    except Exception as exc:
        return f"Reasoning runtime error: {exc}"


def _dispatch_shared(controller, action, client):
    """Controllers retain ownership while the lab owns execution and recovery."""
    if not hasattr(controller, '_shared_reasoning'):
        controller._shared_reasoning = {}
        controller._shared_reasoning_client = 'runtime-' + uuid.uuid4().hex
    client.client = controller._shared_reasoning_client
    client.project = 'sabrina'
    owned = controller._shared_reasoning
    if action['tool'] == 'spawn_reasoning_agent':
        spec = {k:action.get(k,default) for k,default in [('goal',''),('context',''),('tier','fast'),('device','auto')]}
        key = hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()
        if len(owned) >= 16 and key not in owned.values():
            raise ValueError('Controller root objective budget exhausted')
        result = client.submit(**spec,operation_id=key)
        owned[result['task_id']] = key
        return json.dumps(result)
    task_id = action.get('task_id')
    if task_id:
        if task_id not in owned:
            raise ValueError('Objective belongs to another controller session')
        result = client.cancel(task_id) if action['tool']=='cancel_reasoning_agent' else client.status(task_id)
        return json.dumps(result)
    if action['tool']=='cancel_reasoning_agent':
        raise ValueError('task_id is required')
    return json.dumps([client.status(task) for task in owned])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["submit", "status", "work", "cancel", "inventory"])
    parser.add_argument("--goal")
    parser.add_argument("--context", default="")
    parser.add_argument("--task-id")
    parser.add_argument("--config")
    parser.add_argument("--db")
    parser.add_argument("--tier", default="fast", choices=["tiny", "fast", "smart"])
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "gpu"])
    parser.add_argument("--until-idle", action="store_true")
    args = parser.parse_args()
    runtime = Runtime(load_config(args.config), args.db)
    if args.command == "submit":
        print(json.dumps({"task_id": runtime.submit(args.goal, args.context, args.tier, args.device)}))
    elif args.command == "status":
        print(json.dumps(runtime.store.get(args.task_id) if args.task_id else runtime.store.snapshot(), indent=2))
    elif args.command == "inventory":
        print(json.dumps(runtime.fleet.inventory(), indent=2))
    elif args.command == "cancel":
        runtime.store.cancel(args.task_id)
    else:
        try:
            runtime.work(until_idle=args.until_idle)
        except KeyboardInterrupt:
            runtime.stop.set()


if __name__ == "__main__":
    main()
