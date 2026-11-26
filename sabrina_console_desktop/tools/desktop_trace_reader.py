"""Read-only reader for the Sabrina operator decision and desktop trace audit."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data/sabrina_assistant.sqlite3"


class DesktopTraceReader:
    """Read-only reader for assistant sessions, decision audit, and desktop audit events."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _tables(self, conn: sqlite3.Connection) -> set[str]:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {r[0] for r in cur.fetchall()}

    def desktop_capability_state(self, run_id: str, offset: int = 0, limit: int = 100, now=None):
        """Read-only catalog metadata and paginated artifacts; never returns images."""
        with self._connect() as db:
            if 'assistant_desktop_runtime' not in self._tables(db): return {'unavailable':True}
            row=db.execute('SELECT * FROM assistant_desktop_runtime WHERE run_id=?',(run_id,)).fetchone()
            if not row: return {'unavailable':True}
            result=dict(row);expired=bool(row['expired'] or row['expires_at'] <= (time.time() if now is None else now))
            result['is_expired']=expired
            result['permissions']=self._parse_json(row['permissions'])
            result['fingerprint']=self._parse_json(row['fingerprint'])
            result.pop('state_root',None)
            result['artifacts']=[dict(r) for r in db.execute(
                'SELECT id,kind,app,window_id,created,expires_at,sha256 FROM assistant_desktop_artifacts WHERE run_id=? ORDER BY created DESC LIMIT ? OFFSET ?',
                (run_id,max(1,min(100,limit)),max(0,offset)))]
            return result

    def desktop_image(self, run_id: str, artifact_id: str, now=None):
        with self._connect() as db:
            if 'assistant_desktop_runtime' not in self._tables(db): return None
            row=db.execute('''SELECT a.content,a.expires_at,r.expired FROM assistant_desktop_artifacts a
                JOIN assistant_desktop_runtime r ON r.run_id=a.run_id
                WHERE a.run_id=? AND a.id=? AND a.kind='screenshot' ''',(run_id,artifact_id)).fetchone()
            if not row or row['expired'] or row['expires_at'] <= (time.time() if now is None else now): return None
            return bytes(row['content']) if row['content'] else None

    def is_run_expired(self, session_row: sqlite3.Row | dict[str, Any], now: float | None = None) -> bool:
        now_ts = time.time() if now is None else float(now)
        state = session_row.get("state") if isinstance(session_row, dict) else session_row["state"]
        expires = session_row.get("desktop_audit_expires", 0.0) if isinstance(session_row, dict) else (
            session_row["desktop_audit_expires"] if "desktop_audit_expires" in session_row.keys() else 0.0
        )
        if state == "expired":
            return True
        if expires and float(expires) > 0 and float(expires) <= now_ts:
            return True
        return False

    def list_runs(self, limit: int = 50, now: float | None = None) -> list[dict[str, Any]]:
        """Return the latest runs (up to limit, default 50)."""
        limit = max(1, min(500, int(limit)))
        with self._connect() as conn:
            tables = self._tables(conn)
            if "assistant_sessions" not in tables:
                return []

            query = "SELECT * FROM assistant_sessions ORDER BY updated DESC LIMIT ?"
            rows = conn.execute(query, (limit,)).fetchall()

            has_decisions_table = "assistant_decision_audit" in tables
            has_desktop_table = "assistant_desktop_audit_events" in tables

            runs: list[dict[str, Any]] = []
            for row in rows:
                run_id = row["id"]
                expired = self.is_run_expired(row, now=now)

                dec_count = 0
                if has_decisions_table:
                    c_cur = conn.execute(
                        "SELECT COUNT(*) FROM assistant_decision_audit WHERE run_id = ?",
                        (run_id,),
                    )
                    dec_count = c_cur.fetchone()[0]

                desktop_count = 0
                if has_desktop_table:
                    d_cur = conn.execute(
                        "SELECT COUNT(*) FROM assistant_desktop_audit_events WHERE run_id = ?",
                        (run_id,),
                    )
                    desktop_count = d_cur.fetchone()[0]

                if dec_count == 0 and desktop_count == 0:
                    trace_status = "unavailable"
                elif expired:
                    trace_status = "expired"
                else:
                    trace_status = "available"

                req_dev = row["requested_device"] if "requested_device" in row.keys() else ""
                created_ts = row["created"] if "created" in row.keys() else 0.0
                updated_ts = row["updated"] if "updated" in row.keys() else 0.0
                retention_days = (
                    row["desktop_audit_retention_days"]
                    if "desktop_audit_retention_days" in row.keys()
                    else 0
                )
                expires_ts = (
                    row["desktop_audit_expires"]
                    if "desktop_audit_expires" in row.keys()
                    else 0.0
                )

                runs.append({
                    "run_id": run_id,
                    "task": row["task"] if not expired else "",
                    "state": row["state"],
                    "model": row["model"] if "model" in row.keys() else "",
                    "requested_device": req_dev,
                    "activity": row["activity"] if "activity" in row.keys() else "",
                    "created": created_ts,
                    "updated": updated_ts,
                    "retention_days": retention_days,
                    "expires_at": expires_ts,
                    "is_expired": expired,
                    "decision_count": dec_count,
                    "desktop_event_count": desktop_count,
                    "trace_status": trace_status,
                })
            return runs

    def list_events(
        self,
        run_id: str,
        offset: int = 0,
        limit: int = 100,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Paginate decision and desktop events for a run in batches of limit (default 100)."""
        offset = max(0, int(offset))
        limit = max(1, min(500, int(limit)))

        with self._connect() as conn:
            tables = self._tables(conn)
            session_row = None
            if "assistant_sessions" in tables:
                session_row = conn.execute(
                    "SELECT * FROM assistant_sessions WHERE id = ?", (run_id,)
                ).fetchone()

            if session_row is not None and self.is_run_expired(session_row, now=now):
                metadata_events = []
                if "assistant_cross_app_checkpoints" in tables:
                    for row in conn.execute("SELECT id,kind,created,content_sha256,checklist_count,checklist_with_evidence FROM assistant_cross_app_checkpoints WHERE run_id=? ORDER BY id", (run_id,)).fetchall():
                        metadata_events.append({"event_type": "cross_app_checkpoint", "id": row["id"], "created": row["created"],
                                                "completed": row["created"], "summary": f"Cross-app {str(row['kind']).upper()} checkpoint (raw expired)",
                                                "status": "EXPIRED", "is_replay": False, "has_error": False,
                                                "checklist_count": row["checklist_count"], "checklist_with_evidence": row["checklist_with_evidence"]})
                if "assistant_cross_app_routes" in tables:
                    for row in conn.execute("SELECT id,app,choice,handoff_id,created FROM assistant_cross_app_routes WHERE run_id=? ORDER BY id", (run_id,)).fetchall():
                        metadata_events.append({"event_type": "cross_app_route", "id": row["id"], "created": row["created"],
                                                "completed": row["created"], "summary": f"Route choice: {row['app']} → {row['choice']} (raw expired)",
                                                "status": "EXPIRED", "is_replay": False, "has_error": False, "handoff_id": row["handoff_id"]})
                if "assistant_cross_app_handoffs" in tables:
                    for row in conn.execute("SELECT handoff_id,app,status,started,completed,contribution,desktop_call_count,local_prompt_tokens,local_completion_tokens,local_token_counts_known FROM assistant_cross_app_handoffs WHERE run_id=? ORDER BY started", (run_id,)).fetchall():
                        metadata_events.append({"event_type": "cross_app_handoff", "id": row["handoff_id"], "created": row["started"],
                                                "completed": row["completed"], "summary": f"Handoff: {row['app']} [{row['handoff_id']}] (raw expired)",
                                                "status": str(row["status"]).upper(), "is_replay": False, "has_error": False,
                                                "handoff_id": row["handoff_id"], "metadata_only": True})
                metadata_events.sort(key=lambda item: item["created"] or 0)
                total = len(metadata_events)
                paged = metadata_events[offset:offset + limit]
                return {
                    "run_id": run_id,
                    "total": total,
                    "offset": offset,
                    "limit": limit,
                    "is_expired": True,
                    "raw_hidden": True,
                    "events": paged,
                    "message": "Desktop audit retention expired; raw content erased",
                }

            events: list[dict[str, Any]] = []

            # Decision events
            if "assistant_decision_audit" in tables:
                dec_rows = conn.execute(
                    "SELECT id, created, completed, request, metadata, response, error "
                    "FROM assistant_decision_audit WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
                for row in dec_rows:
                    meta = self._parse_json(row["metadata"])
                    req = self._parse_json(row["request"])
                    resp = self._parse_json(row["response"])
                    err = row["error"]

                    # Determine if this was an executed live action or replay/experiment
                    is_replay = bool(
                        meta.get("desktop_execution") is False
                        or meta.get("experiment")
                        or "replay" in str(meta.get("variant", "")).lower()
                        or "replay" in str(meta.get("stage", "")).lower()
                    )

                    step = meta.get("step")
                    model = req.get("model", "")
                    think = req.get("think")
                    msg = resp.get("message", {}) if resp else {}
                    tool_calls = msg.get("tool_calls", [])

                    if err:
                        status = "ERROR"
                    elif not row["completed"]:
                        status = "INCOMPLETE"
                    elif is_replay:
                        status = "REPLAY"
                    else:
                        status = "COMPLETED"

                    summary = f"Decision step {step or '?'}"
                    if model:
                        summary += f" [{model}]"
                    if think is not None:
                        summary += f" think={think}"
                    if tool_calls:
                        call_names = [c.get("function", {}).get("name", "tool") for c in tool_calls]
                        summary += f" -> calls: {', '.join(call_names)}"
                    elif msg.get("content"):
                        summary += " -> content response"

                    events.append({
                        "event_type": "decision",
                        "id": row["id"],
                        "created": row["created"],
                        "completed": row["completed"],
                        "summary": summary,
                        "status": status,
                        "is_replay": is_replay,
                        "has_error": bool(err),
                    })

            # Desktop operations
            if "assistant_desktop_audit_events" in tables:
                desktop_columns = {r[1] for r in conn.execute("PRAGMA table_info(assistant_desktop_audit_events)")}
                handoff_col = ", handoff_id" if "handoff_id" in desktop_columns else ""
                desk_rows = conn.execute(
                    "SELECT id, created, completed, app, tool, arguments, result" + handoff_col + " "
                    "FROM assistant_desktop_audit_events WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
                for row in desk_rows:
                    res = self._parse_json(row["result"])
                    ok = res.get("ok") if res else None
                    if ok is False or (res and "error" in res):
                        status = "FAILED"
                    elif ok is True:
                        status = "OK"
                    elif not row["completed"]:
                        status = "RUNNING"
                    else:
                        status = "UNKNOWN"

                    app_str = f" ({row['app']})" if row["app"] else ""
                    handoff_id = row["handoff_id"] if "handoff_id" in row.keys() else ""
                    link_str = f" [handoff {handoff_id}]" if handoff_id else ""
                    summary = f"Desktop: {row['tool']}{app_str}{link_str}"

                    events.append({
                        "event_type": "desktop",
                        "id": row["id"],
                        "created": row["created"],
                        "completed": row["completed"],
                        "summary": summary,
                        "status": status,
                        "is_replay": False,  # Replay records cannot appear as executed desktop actions!
                        "has_error": status == "FAILED",
                        "handoff_id": handoff_id,
                    })

            # Explicit cross-app route/handoff/checkpoint records; joins are by IDs, never event order.
            cross_specs = (
                ("assistant_cross_app_checkpoints", "cross_app_checkpoint"),
                ("assistant_cross_app_routes", "cross_app_route"),
                ("assistant_cross_app_handoffs", "cross_app_handoff"),
            )
            for table, event_type in cross_specs:
                if table not in tables:
                    continue
                rows = conn.execute(f"SELECT * FROM {table} WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
                for row in rows:
                    item = dict(row)
                    created = item.get("created") or item.get("started") or 0
                    if event_type == "cross_app_checkpoint":
                        label = str(item.get("kind", "checkpoint")).upper()
                        summary = f"Cross-app {label} checkpoint"
                        status = "SAVED"
                    elif event_type == "cross_app_route":
                        summary = f"Route choice: {item.get('app')} → {item.get('choice')}"
                        status = str(item.get("choice", "")).upper()
                    else:
                        summary = f"Handoff: {item.get('app')} [{item.get('handoff_id')}]"
                        status = str(item.get("status", "")).upper()
                    events.append({
                        "event_type": event_type, "id": item.get("id", item.get("handoff_id")),
                        "created": created, "completed": item.get("completed"),
                        "summary": summary, "status": status,
                        "is_replay": False, "has_error": item.get("status") in {"failed", "stopped", "unavailable"},
                        "handoff_id": item.get("handoff_id", ""),
                    })

            # Chronological order
            events.sort(key=lambda x: x["created"])
            total = len(events)
            paged = events[offset : offset + limit]

            return {
                "run_id": run_id,
                "total": total,
                "offset": offset,
                "limit": limit,
                "is_expired": False,
                "raw_hidden": False,
                "events": paged,
            }

    def get_decision_detail(self, event_id: int, now: float | None = None) -> dict[str, Any]:
        """Fetch full details for one decision audit record using background read."""
        with self._connect() as conn:
            tables = self._tables(conn)
            if "assistant_decision_audit" not in tables:
                return {"error": "assistant_decision_audit table unavailable"}

            row = conn.execute(
                "SELECT * FROM assistant_decision_audit WHERE id = ?", (int(event_id),)
            ).fetchone()
            if not row:
                return {"error": f"Decision event {event_id} not found"}

            run_id = row["run_id"]
            if "assistant_sessions" in tables:
                session_row = conn.execute(
                    "SELECT * FROM assistant_sessions WHERE id = ?", (run_id,)
                ).fetchone()
                if session_row is not None and self.is_run_expired(session_row, now=now):
                    return {
                        "event_id": event_id,
                        "run_id": run_id,
                        "is_expired": True,
                        "raw_hidden": True,
                        "message": "Desktop audit retention expired; raw content erased",
                    }

            req = self._parse_json(row["request"])
            meta = self._parse_json(row["metadata"])
            resp = self._parse_json(row["response"])
            err = row["error"]

            is_replay = bool(
                meta.get("desktop_execution") is False
                or meta.get("experiment")
                or "replay" in str(meta.get("variant", "")).lower()
                or "replay" in str(meta.get("stage", "")).lower()
            )

            # Timing & Tokens
            created = row["created"]
            completed = row["completed"]
            wall_clock_s = (completed - created) if completed else None

            total_dur_ns = resp.get("total_duration") if resp else None
            load_dur_ns = resp.get("load_duration") if resp else None
            prompt_dur_ns = resp.get("prompt_eval_duration") if resp else None
            eval_dur_ns = resp.get("eval_duration") if resp else None

            timing = {
                "created": created,
                "completed": completed,
                "wall_clock_seconds": round(wall_clock_s, 3) if wall_clock_s is not None else None,
                "total_duration_s": round(total_dur_ns / 1e9, 3) if total_dur_ns else None,
                "load_duration_s": round(load_dur_ns / 1e9, 3) if load_dur_ns else None,
                "prompt_eval_duration_s": round(prompt_dur_ns / 1e9, 3) if prompt_dur_ns else None,
                "eval_duration_s": round(eval_dur_ns / 1e9, 3) if eval_dur_ns else None,
            }

            token_counts = {
                "prompt_eval_count": resp.get("prompt_eval_count") if resp else None,
                "eval_count": resp.get("eval_count") if resp else None,
            }

            fingerprints = {
                "source_hashes": meta.get("source_hashes", {}),
                "pinned_system_prompt_sha256": meta.get("pinned_system_prompt_sha256", ""),
                "current_system_prompt_sha256": meta.get("current_system_prompt_sha256", ""),
                "prompt_changed": meta.get("prompt_changed", False),
            }

            # Distinguish requested placement from recorded placement
            requested_placement = meta.get("requested_device", "")
            recorded_placement = meta.get("placement") or meta.get("placement_note", "")

            return {
                "event_id": row["id"],
                "run_id": run_id,
                "is_expired": False,
                "raw_hidden": False,
                "is_replay": is_replay,
                "model": req.get("model", ""),
                "thinking_setting": req.get("think"),
                "generation_options": req.get("options", {}),
                "requested_placement": requested_placement,
                "recorded_placement": recorded_placement,
                "timing": timing,
                "token_counts": token_counts,
                "fingerprints": fingerprints,
                "error": err,
                "is_incomplete": bool(completed is None or not resp),
                # Detail views
                "model_input_messages": req.get("messages", []),
                "available_tool_schemas": req.get("tools", []),
                "public_model_output": resp.get("message", {}) if resp else {},
                "raw_metadata": meta,
            }

    def get_desktop_event_detail(self, event_id: int, now: float | None = None) -> dict[str, Any]:
        """Fetch full details for one desktop action event using background read."""
        with self._connect() as conn:
            tables = self._tables(conn)
            if "assistant_desktop_audit_events" not in tables:
                return {"error": "assistant_desktop_audit_events table unavailable"}

            row = conn.execute(
                "SELECT * FROM assistant_desktop_audit_events WHERE id = ?", (int(event_id),)
            ).fetchone()
            if not row:
                return {"error": f"Desktop event {event_id} not found"}

            run_id = row["run_id"]
            if "assistant_sessions" in tables:
                session_row = conn.execute(
                    "SELECT * FROM assistant_sessions WHERE id = ?", (run_id,)
                ).fetchone()
                if session_row is not None and self.is_run_expired(session_row, now=now):
                    return {
                        "event_id": event_id,
                        "run_id": run_id,
                        "is_expired": True,
                        "raw_hidden": True,
                        "message": "Desktop audit retention expired; raw content erased",
                    }

            args = self._parse_json(row["arguments"])
            result = self._parse_json(row["result"])

            created = row["created"]
            completed = row["completed"]
            elapsed = (completed - created) if completed else None

            return {
                "event_id": row["id"],
                "run_id": run_id,
                "is_expired": False,
                "raw_hidden": False,
                "app": row["app"],
                "tool": row["tool"],
                "arguments": args,
                "result": result,  # Full stored observation preserved!
                "handoff_id": row["handoff_id"] if "handoff_id" in row.keys() else "",
                "decision_id": row["decision_id"] if "decision_id" in row.keys() else None,
                "created": created,
                "completed": completed,
                "elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
                "outcome_ok": result.get("ok") if isinstance(result, dict) else None,
                "error": result.get("error") if isinstance(result, dict) else None,
            }

    def get_cross_app_event_detail(self, event_type: str, event_id: int | str, now: float | None = None) -> dict[str, Any]:
        """Fetch one explicit route, handoff, or checkpoint with its direct links."""
        table_by_type = {
            "cross_app_route": "assistant_cross_app_routes",
            "cross_app_handoff": "assistant_cross_app_handoffs",
            "cross_app_checkpoint": "assistant_cross_app_checkpoints",
        }
        table = table_by_type.get(event_type)
        if table is None:
            return {"error": "unsupported cross-app event type"}
        with self._connect() as conn:
            tables = self._tables(conn)
            if table not in tables:
                return {"error": f"{table} table unavailable"}
            key = "handoff_id" if event_type == "cross_app_handoff" else "id"
            value: Any = str(event_id) if key == "handoff_id" else int(event_id)
            row = conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
            if row is None:
                return {"error": f"{event_type} event {event_id} not found"}
            record = dict(row)
            run_id = record["run_id"]
            if "assistant_sessions" in tables:
                session = conn.execute("SELECT * FROM assistant_sessions WHERE id=?", (run_id,)).fetchone()
                if session is not None and self.is_run_expired(session, now=now):
                    metadata = {key: record.get(key) for key in (
                        "id", "run_id", "kind", "app", "choice", "handoff_id", "route_id", "status",
                        "decision_id", "started", "submitted", "completed", "contribution", "elapsed_seconds",
                        "desktop_call_count", "local_prompt_tokens", "local_completion_tokens", "local_token_counts_known",
                        "external_token_count", "content_sha256", "checklist_count", "checklist_with_evidence",
                    )}
                    metadata["event_id"] = event_id
                    metadata["is_expired"] = True
                    metadata["raw_hidden"] = True
                    metadata["metadata_only"] = True
                    metadata["message"] = "Raw desktop content expired; metadata only"
                    return metadata
            if event_type == "cross_app_checkpoint":
                record["evidence_refs"] = self._parse_json(record.get("evidence_refs"))
                record["evidence_checklist"] = self._parse_json(record.get("checklist_json"))
                record["evidence_coverage"] = {
                    "referenced_claims": record.get("checklist_with_evidence", 0),
                    "total_claims": record.get("checklist_count", 0),
                }
            if event_type == "cross_app_handoff":
                linked_decision_ids = {int(v) for v in (record.get("decision_id"), record.get("prompt_decision_id"), record.get("completion_decision_id")) if v is not None}
                stored_decision_ids = self._parse_json(record.get("local_model_decision_ids"))
                if isinstance(stored_decision_ids, list):
                    linked_decision_ids.update(int(v) for v in stored_decision_ids if str(v).isdigit())
                if "assistant_desktop_audit_events" in tables:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(assistant_desktop_audit_events)")}
                    if "handoff_id" in cols:
                        linked = conn.execute("SELECT id,created,completed,app,tool,decision_id FROM assistant_desktop_audit_events WHERE run_id=? AND handoff_id=? ORDER BY id",
                                              (run_id, record["handoff_id"])).fetchall()
                        record["linked_desktop_events"] = [dict(item) for item in linked]
                        linked_decision_ids.update(int(item["decision_id"]) for item in linked if item["decision_id"] is not None)
                    else:
                        record["linked_desktop_events"] = []
                else:
                    record["linked_desktop_events"] = []
                if "assistant_cross_app_routes" in tables:
                    route = conn.execute("SELECT decision_id FROM assistant_cross_app_routes WHERE id=?", (record.get("route_id"),)).fetchone()
                    if route and route["decision_id"] is not None:
                        linked_decision_ids.add(int(route["decision_id"]))
                token_rows = []
                if "assistant_decision_audit" in tables and linked_decision_ids:
                    placeholders = ",".join("?" for _ in linked_decision_ids)
                    token_rows = conn.execute(f"SELECT id,response FROM assistant_decision_audit WHERE run_id=? AND id IN ({placeholders})",
                                              (run_id, *sorted(linked_decision_ids))).fetchall()
                token_totals = {"prompt_eval_count": 0, "eval_count": 0}
                token_known = bool(token_rows)
                for token_row in token_rows:
                    response = self._parse_json(token_row["response"])
                    for key in token_totals:
                        value = response.get(key) if isinstance(response, dict) else None
                        if not isinstance(value, int):
                            token_known = False
                        else:
                            token_totals[key] += value
                record["measurement"] = {
                    "elapsed_seconds": record.get("elapsed_seconds") if record.get("elapsed_seconds") is not None else (round(float(record["completed"]) - float(record["started"]), 3) if record.get("completed") else None),
                    "desktop_call_count": record.get("desktop_call_count", len(record.get("linked_desktop_events", []))),
                    "local_model_decision_ids": sorted(linked_decision_ids),
                    "local_model_token_counts": ({
                        "prompt_eval_count": record.get("local_prompt_tokens"),
                        "eval_count": record.get("local_completion_tokens"),
                    } if record.get("local_token_counts_known") else (token_totals if token_known else None)),
                    "external_token_count": record.get("external_token_count"),
                    "external_token_count_status": "unavailable unless exposed in the app UI",
                }
            if event_type == "cross_app_route" and record.get("handoff_id"):
                if "assistant_cross_app_handoffs" in tables:
                    related = conn.execute("SELECT * FROM assistant_cross_app_handoffs WHERE handoff_id=?", (record["handoff_id"],)).fetchone()
                    record["handoff"] = dict(related) if related else None
            record["is_expired"] = False
            record["raw_hidden"] = False
            return record

    @staticmethod
    def _parse_json(value: Any) -> Any:
        if not value:
            return {}
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except Exception:
            return {"_raw_malformed": str(value)}

    def trace_run_information_path(self, run_id: str, now: float | None = None) -> dict[str, Any]:
        """Trace a run across task, coordinator, desktop broker, model context, audit store, and shared swarm state."""
        from tools.agent_desktop_broker import (
            extract_actionable_prompts,
            extract_window_conversation_identity,
        )

        with self._connect() as conn:
            tables = self._tables(conn)
            if "assistant_sessions" not in tables:
                return {"error": "assistant_sessions table unavailable"}
            sess = conn.execute("SELECT * FROM assistant_sessions WHERE id=?", (run_id,)).fetchone()
            if sess is None:
                return {"error": f"Unknown run_id {run_id!r}"}
            if self.is_run_expired(sess, now=now):
                return {"run_id": run_id, "is_expired": True, "error": "Desktop audit retention expired"}

            desktop_rows = (
                conn.execute(
                    "SELECT * FROM assistant_desktop_audit_events WHERE run_id=? ORDER BY id",
                    (run_id,),
                ).fetchall()
                if "assistant_desktop_audit_events" in tables
                else []
            )
            decision_rows = (
                conn.execute(
                    "SELECT * FROM assistant_decision_audit WHERE run_id=? ORDER BY id",
                    (run_id,),
                ).fetchall()
                if "assistant_decision_audit" in tables
                else []
            )
            approval_rows = (
                conn.execute(
                    "SELECT * FROM assistant_desktop_approvals WHERE run_id=? ORDER BY id",
                    (run_id,),
                ).fetchall()
                if "assistant_desktop_approvals" in tables
                else []
            )
            swarm_event_rows = (
                conn.execute(
                    "SELECT * FROM agent_events WHERE task_id=? ORDER BY id",
                    (run_id,),
                ).fetchall()
                if "agent_events" in tables
                else []
            )
            cross_profile_row = (
                conn.execute("SELECT * FROM assistant_cross_app_reviews WHERE run_id=?", (run_id,)).fetchone()
                if "assistant_cross_app_reviews" in tables else None
            )
            cross_routes = (
                conn.execute("SELECT * FROM assistant_cross_app_routes WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
                if "assistant_cross_app_routes" in tables else []
            )
            cross_handoffs = (
                conn.execute("SELECT * FROM assistant_cross_app_handoffs WHERE run_id=? ORDER BY started", (run_id,)).fetchall()
                if "assistant_cross_app_handoffs" in tables else []
            )
            cross_checkpoints = (
                conn.execute("SELECT * FROM assistant_cross_app_checkpoints WHERE run_id=? ORDER BY created,id", (run_id,)).fetchall()
                if "assistant_cross_app_checkpoints" in tables else []
            )

        decisions_parsed = []
        for drow in decision_rows:
            req = self._parse_json(drow["request"])
            resp = self._parse_json(drow["response"])
            meta = self._parse_json(drow["metadata"])
            decisions_parsed.append({
                "decision_id": drow["id"],
                "created": drow["created"],
                "completed": drow["completed"],
                "metadata": meta,
                "messages": req.get("messages", []) if isinstance(req, dict) else [],
                "output_message": resp.get("message", {}) if isinstance(resp, dict) else {},
            })

        snapshot_registry: dict[str, dict[str, Any]] = {}
        desktop_steps: list[dict[str, Any]] = []

        for erow in desktop_rows:
            ev_id = erow["id"]
            args = self._parse_json(erow["arguments"])
            res = self._parse_json(erow["result"])
            data = res.get("data") if isinstance(res, dict) and isinstance(res.get("data"), dict) else {}
            conv = extract_window_conversation_identity(res) if isinstance(res, dict) else {}
            prompts_info = extract_actionable_prompts(res) if isinstance(res, dict) else {
                "prompt_actionable": False,
                "actionable_prompts": [],
                "non_actionable_text_matches": [],
            }
            snap_id = str(data.get("snapshot_id") or "")
            if snap_id:
                snapshot_registry[snap_id] = {
                    "event_id": ev_id,
                    "created": erow["created"],
                    "completed": erow["completed"],
                    "window_id": conv.get("window_id", ""),
                    "window_title": conv.get("window_title", ""),
                    "conversation_key": conv.get("conversation_key", ""),
                    "prompts_info": prompts_info,
                }

            # Find what Sabrina actually received in the next decision turn
            model_received: dict[str, Any] | None = None
            for dec in decisions_parsed:
                if dec["created"] >= erow["created"]:
                    msgs = dec["messages"]
                    if msgs and msgs[-1].get("role") == "tool" and msgs[-1].get("tool_name") == erow["tool"]:
                        raw_content = str(msgs[-1].get("content") or "")
                        parsed_content = self._parse_json(raw_content)
                        pdata = parsed_content.get("data") if isinstance(parsed_content, dict) and isinstance(parsed_content.get("data"), dict) else {}
                        received_refs: set[str] = set()
                        for elem in pdata.get("elements", []) or []:
                            if isinstance(elem, dict) and elem.get("ref_id"):
                                received_refs.add(str(elem["ref_id"]))
                        for ap in pdata.get("actionable_prompts", []) or []:
                            if isinstance(ap, dict):
                                if ap.get("command_ref_id"):
                                    received_refs.add(str(ap["command_ref_id"]))
                                for ch in ap.get("choices", []) or []:
                                    if isinstance(ch, dict) and ch.get("ref_id"):
                                        received_refs.add(str(ch["ref_id"]))
                        lost_controls = []
                        for ap in prompts_info.get("actionable_prompts", []):
                            for ch in ap.get("choices", []):
                                if ch.get("ref_id") and str(ch["ref_id"]) not in received_refs:
                                    lost_controls.append(ch)
                        model_received = {
                            "decision_id": dec["decision_id"],
                            "content_chars": len(raw_content),
                            "summary_truncated": bool(pdata.get("summary_truncated")),
                            "omitted_elements": pdata.get("omitted_elements", 0),
                            "received_ref_ids": sorted(received_refs),
                            "received_actionable_prompts": pdata.get("actionable_prompts", []),
                            "lost_controls": lost_controls,
                            "sabrina_response": dec["output_message"],
                        }
                        break

            stale_analysis = None
            err = res.get("error") if isinstance(res, dict) and isinstance(res.get("error"), dict) else None
            if err and err.get("code") == "STALE_REF":
                ref_arg = str(args.get("ref_id") or "")
                origin_snap = ref_arg.split(":", 1)[0].lstrip("@") if ":" in ref_arg else ""
                origin_info = snapshot_registry.get(origin_snap)
                elapsed_s = (erow["created"] - origin_info["completed"]) if origin_info and origin_info.get("completed") else None
                stale_analysis = {
                    "ref_id": ref_arg,
                    "origin_snapshot_id": origin_snap,
                    "origin_event_id": origin_info["event_id"] if origin_info else None,
                    "origin_window_title": origin_info["window_title"] if origin_info else "",
                    "origin_conversation_key": origin_info["conversation_key"] if origin_info else "",
                    "elapsed_seconds_since_snapshot": round(elapsed_s, 3) if elapsed_s is not None else None,
                }

            wait_result = None
            if erow["tool"] == "desktop_wait":
                wait_result = {
                    "ok": res.get("ok") if isinstance(res, dict) else False,
                    "found": data.get("found"),
                    "text": data.get("text") or args.get("text"),
                    "elapsed_ms": data.get("elapsed_ms"),
                    "prompt_actionable": data.get("prompt_actionable"),
                    "found_in_history_only": data.get("found_in_history_only", False),
                    "error": err,
                }

            desktop_steps.append({
                "event_id": ev_id,
                "created": erow["created"],
                "completed": erow["completed"],
                "app": erow["app"],
                "tool": erow["tool"],
                "arguments": args,
                "ok": res.get("ok") if isinstance(res, dict) else False,
                "window_id": conv.get("window_id", ""),
                "window_title": conv.get("window_title", ""),
                "conversation_key": conv.get("conversation_key", ""),
                "snapshot_id": snap_id,
                "handoff_id": erow["handoff_id"] if "handoff_id" in erow.keys() else "",
                "decision_id": erow["decision_id"] if "decision_id" in erow.keys() else None,
                "wait_result": wait_result,
                "raw_audit_prompts": prompts_info,
                "model_context_received": model_received,
                "stale_analysis": stale_analysis,
                "error": err,
            })

        cross_timeline = []
        for row in cross_checkpoints:
            item = dict(row)
            item["evidence_refs"] = self._parse_json(item.get("evidence_refs"))
            item["evidence_checklist"] = self._parse_json(item.get("checklist_json"))
            cross_timeline.append({"event_type": item.get("kind"), "id": item["id"], "created": item["created"],
                                   "decision_id": item.get("decision_id"), "record": item})
        for row in cross_routes:
            item = dict(row)
            cross_timeline.append({"event_type": "route", "id": item["id"], "created": item["created"],
                                   "decision_id": item.get("decision_id"), "route_id": item["id"],
                                   "handoff_id": item.get("handoff_id"), "record": item})
        for row in cross_handoffs:
            item = dict(row)
            item["linked_desktop_events"] = [step["event_id"] for step in desktop_steps if step.get("handoff_id") == item["handoff_id"]]
            cross_timeline.append({"event_type": "handoff", "id": item["handoff_id"], "created": item["started"],
                                   "completed": item.get("completed"), "decision_id": item.get("decision_id"),
                                   "route_id": item.get("route_id"), "handoff_id": item["handoff_id"], "record": item})
        for step in desktop_steps:
            if step.get("handoff_id"):
                cross_timeline.append({"event_type": "desktop_event", "id": step["event_id"], "created": step["created"],
                                       "completed": step["completed"], "decision_id": step.get("decision_id"),
                                       "handoff_id": step["handoff_id"], "record": step})
        cross_timeline.sort(key=lambda item: (item.get("created") or 0, item.get("event_type", "")))

        return {
            "run_id": run_id,
            "task": sess["task"],
            "state": sess["state"],
            "revision": sess["revision"],
            "model": sess["model"],
            "requested_device": sess["requested_device"],
            "desktop_steps": desktop_steps,
            "cross_app_review": ({
                "enabled": bool(cross_profile_row["enabled"]),
                "state": cross_profile_row["state"],
                "apps": self._parse_json(cross_profile_row["apps"]),
                "timeline": cross_timeline,
            } if cross_profile_row else {"enabled": False, "timeline": []}),
            "forwarded_to_swarm": {
                "session_progress": sess["progress"],
                "session_activity": sess["activity"],
                "evidence_references": self._parse_json(sess["refs"]) if sess["refs"] else [],
                "approvals": [dict(r) for r in approval_rows],
                "agent_events": [
                    {**dict(r), "detail": self._parse_json(r["detail"])}
                    for r in swarm_event_rows
                ],
            },
        }


def trace_run_information_path(db_path: Path | str, run_id: str) -> dict[str, Any]:
    """Convenience entry point to trace a run's full information path."""
    return DesktopTraceReader(db_path).trace_run_information_path(run_id)

