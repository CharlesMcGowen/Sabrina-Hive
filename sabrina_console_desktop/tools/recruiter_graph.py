"""Evidence-backed recruiter relationship graph.

The graph deliberately lives beside the existing messaging tables and uses the
same SQLite connection.  It is not a people-search system: all non-profile
facts come from captured messages and all profile facts come from the approved
profile file.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from tools.messaging_store import ConversationStore, digest, now


GRAPH_VERSION = "recruiter-graph-v1"
_ROLE = re.compile(r"(?:job title|title|role|position)\s*[:\-]\s*([^\n.;]{2,120})", re.I)
_COMPANY = re.compile(r"(?:hiring company|company|client|employer)\s*[:\-]\s*([^\n.;]{2,120})", re.I)
_LOCATION = re.compile(r"(?:location|located in|based in)\s*[:\-]?\s*([^\n.;]{2,120})", re.I)
_COMPENSATION = re.compile(r"(?:\$\s?[\d,]+(?:\.\d+)?\s*(?:/\s*(?:hr|hour|yr|year)|k)?(?:\s*(?:-|to)\s*\$?\s?[\d,]+(?:\.\d+)?\s*(?:/\s*(?:hr|hour|yr|year)|k)?)?)|(?:salary|rate|compensation)\s*[:\-]\s*([^\n.;]{2,120})", re.I)
_EMPLOYMENT = re.compile(r"\b(full[- ]?time|part[- ]?time|contract(?:or)?|temporary|freelance|1099|w2)\b", re.I)
_REQUIREMENTS = re.compile(r"(?:requirements?|qualifications?|must have|looking for)\s*[:\-]\s*([^\n]{2,500})", re.I)
_INTERVIEW = re.compile(r"(?:interview|screen|call)\b[^\n.]{0,200}", re.I)
_REQUESTS = {
    "resume": re.compile(r"\b(?:resume|cv)\b", re.I),
    "availability": re.compile(r"\b(?:availability|available|when can you|start date|start when)\b", re.I),
    "preferred_hourly_rate": re.compile(r"\b(?:hourly rate|desired rate|pay rate|compensation expectation|salary expectation)\b", re.I),
    "location_preference": re.compile(r"\b(?:where are you located|your work location|would you relocate|can you relocate|can you work (?:remote|hybrid|onsite)|are you (?:remote|local))\b", re.I),
    "interest": re.compile(r"\b(?:interested|interest in|open to|would you like)\b", re.I),
    "job_description": re.compile(r"\b(?:job description|job details|role details|more about the role)\b", re.I),
    "hiring_company": re.compile(r"\b(?:hiring company|end client|client name|who is the client)\b", re.I),
    "employment_type": re.compile(r"\b(?:employment type|full[- ]?time|part[- ]?time|contract)\b", re.I),
    "compensation_range": re.compile(r"\b(?:salary range|pay range|rate range|compensation range)\b", re.I),
    "location": re.compile(r"\b(?:location|worksite|remote|hybrid|on[- ]?site)\b", re.I),
    "role": re.compile(r"\b(?:which role|what role|position details|role title)\b", re.I),
}
USER_REQUEST_KEYS = {"resume", "availability", "preferred_hourly_rate", "location_preference", "interest"}
_COMMITMENT = re.compile(r"\b(?:I(?:'|’)ll|I will|I can|I am available|I’m available|I am free|I’m free|let’s talk|let us talk)\b[^.!?\n]{0,220}", re.I)
_RECRUITER_SIGNAL = re.compile(r"\b(?:recruiter|recruiting|talent acquisition|hiring|role|position|job|opportunity|client|resume|contract)\b", re.I)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _quote(text, match, limit=600):
    value = match.group(0) if hasattr(match, "group") else str(match)
    return value.strip()[:limit]


def _message_ids(thread):
    return [m["id"] for m in thread.get("messages", [])]


def _source(message, quote):
    return {
        "source_message_ids": [message["id"]],
        "source_quote": quote.strip(),
        "observed_at": message.get("timestamp") or message.get("observed_at") or now(),
        "confidence": "observed",
        "status": "observed",
    }


def _fact(field, value, message, quote, *, status="observed", confidence="high"):
    evidence = _source(message, quote)
    evidence.update({"field": field, "value": value.strip(), "status": status, "confidence": confidence})
    return evidence


def _stable_org_key(name, message, explicit_id=None):
    # A name is not a stable identity.  Without a platform identifier we keep
    # the source message in the key, so similarly named organizations cannot be
    # silently merged.
    if explicit_id:
        return "platform:" + str(explicit_id)
    return "source:" + digest([message["id"], name.strip().casefold()])[:32]


def extract_role_facts(thread):
    """Extract only source-backed, deterministic role facts from the chat."""
    facts = []
    seen = set()
    for message in thread.get("messages", []):
        if message.get("ownership") != "external":
            continue
        text = message.get("text", "")
        candidates = []
        for field, pattern in (("role", _ROLE), ("company", _COMPANY), ("location", _LOCATION),
                               ("requirements", _REQUIREMENTS)):
            match = pattern.search(text)
            if match:
                value = next((g for g in match.groups()[::-1] if g), match.group(0))
                candidates.append((field, value.strip(), _quote(text, match)))
        compensation = _COMPENSATION.search(text)
        if compensation:
            value = next((g for g in compensation.groups()[::-1] if g), compensation.group(0))
            candidates.append(("compensation", value.strip(), _quote(text, compensation)))
        employment = _EMPLOYMENT.search(text)
        if employment:
            candidates.append(("employment_type", employment.group(1), _quote(text, employment)))
        interview = _INTERVIEW.search(text)
        if interview:
            candidates.append(("interview_details", interview.group(0), _quote(text, interview)))
        # A substantial recruiter message is itself a source-backed job
        # description.  This prevents rich descriptions from disappearing when
        # they use bullets or prose rather than a label.
        if len(text.strip()) >= 180 and any(token in text.lower() for token in ("role", "position", "job", "client", "responsibilities")):
            candidates.append(("job_description", text.strip(), text.strip()[:600]))
        for field, value, quote in candidates:
            key = (field, value.casefold(), message["id"])
            if key not in seen and value:
                seen.add(key)
                facts.append(_fact(field, value, message, quote))
    return facts


def extract_commitments(thread):
    commitments = []
    for message in thread.get("messages", []):
        if message.get("ownership") != "self":
            continue
        for match in _COMMITMENT.finditer(message.get("text", "")):
            quote = _quote(message.get("text", ""), match)
            commitments.append({**_source(message, quote), "text": quote, "owner": "user", "status": "explicit"})
    return commitments


def extract_requests(thread, facts):
    requests = []
    fact_fields = {f["field"] for f in facts}
    for message in thread.get("messages", []):
        if message.get("ownership") != "external":
            continue
        text = message.get("text", "")
        for key, pattern in _REQUESTS.items():
            match = pattern.search(text)
            if not match:
                continue
            # A role fact already present means asking for that same role
            # detail would be circular; keep the evidence but mark it resolved.
            resolves_with = {
                "job_description": {"job_description", "role"},
                "role": {"role", "job_description"},
                "hiring_company": {"company"},
                "location": {"location"},
                "employment_type": {"employment_type"},
                "compensation_range": {"compensation"},
            }
            resolved = bool(fact_fields & resolves_with.get(key, set()))
            requests.append({**_source(message, _quote(text, match)), "question": key,
                             "owner": "user", "status": "resolved" if resolved else "open",
                             "exact_text": text.strip()[:1400], "due_date": None})
    unique = {}
    for request in requests:
        unique[(request["question"], request["source_message_ids"][0])] = request
    # A recruiter who opens with a generic role inquiry has not supplied a
    # usable role dossier yet.  Record one deterministic, source-backed role
    # question so strategy can request the missing details without asking for a
    # personal fact.
    external = [m for m in thread.get("messages", []) if m.get("ownership") == "external"]
    if external and not (fact_fields & {"role", "job_description"}) and not any(r["question"] in USER_REQUEST_KEYS for r in requests):
        message = external[-1]
        item = {**_source(message, message.get("text", "")[:600]), "question": "job_description",
                "owner": "user", "status": "open", "exact_text": message.get("text", "")[:1400], "due_date": None}
        unique[(item["question"], item["source_message_ids"][0])] = item
    return list(unique.values())


def _latest_owner(thread):
    messages = thread.get("messages", [])
    return messages[-1].get("ownership") if messages else None


def _classification(thread):
    external = [m for m in thread.get("messages", []) if m.get("ownership") == "external"]
    if any(re.search(r"\b(?:do not|don't|stop|remove me).{0,25}(?:contact|messag|reach|mail|list)", m.get("text", ""), re.I) for m in external):
        return "do_not_contact"
    return "recruiter" if any(_RECRUITER_SIGNAL.search(m.get("text", "")) for m in external) else "needs_classification"


def build_snapshot(thread, approved_profile=None, *, graph_version=GRAPH_VERSION, state=None):
    """Build a pure snapshot for model packets and tests.

    This function has no database side effects.  ``RecruiterGraph.observe`` is
    the durable wrapper around it.
    """
    profile = approved_profile or {"version": 0, "facts": [], "preferences": {}, "availability": None}
    role_facts = extract_role_facts(thread)
    requests = extract_requests(thread, role_facts)
    commitments = extract_commitments(thread)
    facts = [dict(f, source="conversation") for f in role_facts]
    facts.extend({"field": "approved_profile", "value": f["text"], "status": "approved",
                  "confidence": "approved", "source": "profile", "fact_id": f["id"],
                  "source_message_ids": [], "source_quote": f["text"]} for f in profile.get("facts", []))
    open_questions = [r["question"] for r in requests if r["status"] == "open"]
    # Preserve first occurrence order while removing duplicates.
    open_questions = list(dict.fromkeys(open_questions))
    person = {"platform": "linkedin", "platform_id": thread.get("peer_id", "").rstrip("/"),
              "name": thread.get("peer_name", ""), "profile_url": thread.get("url", ""),
              "observed_title": None}
    current = state or {"state": "new", "response_owner": "none", "current_objective": None,
                        "next_action": None, "turn_count": len(thread.get("messages", [])),
                        "strategy_version": None}
    snapshot = {
        "graph_version": graph_version,
        "graph_policy": GRAPH_VERSION,
        "account_id": thread.get("account_id"), "thread_id": thread.get("thread_id"),
        "context_version": thread.get("version"), "profile_version": profile.get("version", 0),
        "profile_hash": digest(profile), "person": person,
        "contact_classification": _classification(thread),
        "organizations": [], "opportunities": [], "facts": facts,
        "role_facts": role_facts, "requests": requests, "commitments": commitments,
        "open_questions": open_questions, "latest_owner": _latest_owner(thread),
        "conversation_state": current, "source_message_ids": _message_ids(thread),
    }
    snapshot["graph_version"] = digest({k: v for k, v in snapshot.items() if k not in {"graph_version", "graph_policy"}})
    return snapshot


class RecruiterGraph:
    def __init__(self, store=None):
        self.store = store or ConversationStore()
        self.db = self.store.db
        self._init_schema()

    def _init_schema(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS recruiter_graph_people(
          account TEXT NOT NULL, platform TEXT NOT NULL, platform_id TEXT NOT NULL,
          name TEXT NOT NULL, profile_url TEXT NOT NULL, observed_title TEXT,
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          PRIMARY KEY(account,platform,platform_id));
        CREATE TABLE IF NOT EXISTS recruiter_graph_organizations(
          account TEXT NOT NULL, identity TEXT NOT NULL, name TEXT NOT NULL,
          kind TEXT NOT NULL, source_message_ids TEXT NOT NULL, source_quote TEXT NOT NULL,
          observed_at TEXT NOT NULL, confidence TEXT NOT NULL, status TEXT NOT NULL,
          context_version INTEGER NOT NULL, profile_hash TEXT NOT NULL,
          PRIMARY KEY(account,identity));
        CREATE TABLE IF NOT EXISTS recruiter_graph_opportunities(
          id TEXT PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
          role TEXT, company TEXT, stage TEXT NOT NULL, location TEXT,
          employment_type TEXT, compensation TEXT, requirements TEXT,
          source_message_ids TEXT NOT NULL, source_quote TEXT NOT NULL,
          observed_at TEXT NOT NULL, confidence TEXT NOT NULL, status TEXT NOT NULL,
          context_version INTEGER NOT NULL, profile_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recruiter_graph_facts(
          id TEXT PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
          subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, field TEXT NOT NULL,
          value TEXT NOT NULL, source_message_ids TEXT NOT NULL, source_quote TEXT NOT NULL,
          observed_at TEXT NOT NULL, confidence TEXT NOT NULL, status TEXT NOT NULL,
          context_version INTEGER NOT NULL, profile_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recruiter_graph_edges(
          id TEXT PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
          source_type TEXT NOT NULL, source_id TEXT NOT NULL, relation TEXT NOT NULL,
          target_type TEXT NOT NULL, target_id TEXT NOT NULL, source_message_ids TEXT NOT NULL,
          source_quote TEXT NOT NULL, observed_at TEXT NOT NULL, confidence TEXT NOT NULL,
          status TEXT NOT NULL, context_version INTEGER NOT NULL, profile_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recruiter_graph_requests(
          id TEXT PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
          question TEXT NOT NULL, exact_text TEXT NOT NULL, owner TEXT NOT NULL,
          status TEXT NOT NULL, due_date TEXT, source_message_ids TEXT NOT NULL,
          source_quote TEXT NOT NULL, observed_at TEXT NOT NULL, confidence TEXT NOT NULL,
          context_version INTEGER NOT NULL, profile_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recruiter_graph_commitments(
          id TEXT PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
          text TEXT NOT NULL, owner TEXT NOT NULL, status TEXT NOT NULL, due_date TEXT,
          source_message_ids TEXT NOT NULL, source_quote TEXT NOT NULL,
          observed_at TEXT NOT NULL, confidence TEXT NOT NULL, context_version INTEGER NOT NULL,
          profile_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recruiter_graph_conversation_state(
          account TEXT NOT NULL, thread TEXT NOT NULL, state TEXT NOT NULL,
          response_owner TEXT NOT NULL, open_questions TEXT NOT NULL,
          current_objective TEXT, next_action TEXT, turn_count INTEGER NOT NULL,
          strategy_version TEXT, graph_version TEXT NOT NULL, profile_hash TEXT NOT NULL,
          context_version INTEGER NOT NULL, updated TEXT NOT NULL,
          PRIMARY KEY(account,thread));
        CREATE TABLE IF NOT EXISTS recruiter_graph_autonomy(
          id TEXT PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
          source_message_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
          status TEXT NOT NULL, text TEXT NOT NULL, receipt TEXT, created TEXT NOT NULL,
          UNIQUE(account,thread,source_message_id,strategy_version));
        CREATE TABLE IF NOT EXISTS recruiter_graph_autonomy_controls(
          account TEXT NOT NULL, thread TEXT NOT NULL, enabled INTEGER NOT NULL,
          reason TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(account,thread));
        ''')
        self.db.commit()

    def _upsert_fact(self, thread, profile, item, subject_type="conversation", subject_id=None):
        source_ids = item.get("source_message_ids", [])
        fid = digest([thread["account_id"], thread["thread_id"], subject_type, subject_id or thread["thread_id"], item.get("field"), item.get("value"), source_ids])
        self.db.execute('''INSERT OR REPLACE INTO recruiter_graph_facts
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (fid, thread["account_id"], thread["thread_id"], subject_type, subject_id or thread["thread_id"],
           item.get("field", "unknown"), item.get("value", ""), _json(source_ids), item.get("source_quote", ""),
           item.get("observed_at", now()), item.get("confidence", "observed"), item.get("status", "observed"),
           thread["version"], digest(profile)))
        return fid

    def _upsert_edge(self, thread, profile, *, source_type, source_id, relation, target_type, target_id, item):
        eid = digest([thread["account_id"], thread["thread_id"], source_type, source_id, relation, target_type, target_id,
                      item.get("source_message_ids", []), item.get("source_quote", "")])
        self.db.execute('''INSERT OR REPLACE INTO recruiter_graph_edges
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (eid, thread["account_id"], thread["thread_id"], source_type, source_id, relation, target_type, target_id,
           _json(item.get("source_message_ids", [])), item.get("source_quote", ""), item.get("observed_at", now()),
           item.get("confidence", "observed"), item.get("status", "observed"), thread["version"], digest(profile)))
        return eid

    def _persist_entities(self, thread, profile, role_facts, requests, commitments):
        messages = {m["id"]: m for m in thread.get("messages", [])}
        person_id = thread["peer_id"].rstrip("/")
        company_facts = [f for f in role_facts if f["field"] == "company"]
        for fact in company_facts:
            source = messages[fact["source_message_ids"][0]]
            kind = "client" if re.search(r"\bclient\b", fact.get("source_quote", ""), re.I) else "hiring_company"
            identity = _stable_org_key(fact["value"], source)
            self.db.execute('''INSERT OR REPLACE INTO recruiter_graph_organizations
              VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
              (thread["account_id"], identity, fact["value"], kind, _json(fact["source_message_ids"]), fact["source_quote"],
               fact["observed_at"], fact["confidence"], fact["status"], thread["version"], digest(profile)))
        by_field = {}
        for fact in role_facts:
            by_field.setdefault(fact["field"], []).append(fact)
        primary = next(iter(role_facts), None)
        if primary:
            source_ids = list(dict.fromkeys(i for f in role_facts for i in f["source_message_ids"]))
            quote = " ".join(f["source_quote"] for f in role_facts)[:1400]
            opportunity_id = digest([thread["account_id"], thread["thread_id"], by_field.get("role", [{}])[0].get("value"),
                                     by_field.get("company", [{}])[0].get("value"), source_ids])
            self.db.execute('''INSERT OR REPLACE INTO recruiter_graph_opportunities
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (opportunity_id, thread["account_id"], thread["thread_id"], by_field.get("role", [{}])[0].get("value"),
               by_field.get("company", [{}])[0].get("value"), "qualifying", by_field.get("location", [{}])[0].get("value"),
               by_field.get("employment_type", [{}])[0].get("value"), by_field.get("compensation", [{}])[0].get("value"),
               by_field.get("requirements", [{}])[0].get("value"), _json(source_ids), quote, primary["observed_at"],
               "observed", "observed", thread["version"], digest(profile)))
            self._upsert_edge(thread, profile, source_type="person", source_id=person_id, relation="discusses",
                              target_type="opportunity", target_id=opportunity_id,
                              item={"source_message_ids": source_ids, "source_quote": quote, "confidence": "observed"})
            for fact in company_facts:
                org_id = _stable_org_key(fact["value"], messages[fact["source_message_ids"][0]])
                text = messages[fact["source_message_ids"][0]]["text"]
                for relation, pattern in (("represents", r"\brepresent(?:ing|s)?\b"), ("works_for", r"\bwork(?:s|ing)? for\b"), ("recruits_for", r"\brecruit(?:ing|s)? for\b")):
                    if re.search(pattern, text, re.I):
                        self._upsert_edge(thread, profile, source_type="person", source_id=person_id, relation=relation,
                                          target_type="organization", target_id=org_id, item=fact)
        for request in requests:
            self._upsert_edge(thread, profile, source_type="person", source_id=person_id, relation="asked_for",
                              target_type="account", target_id=thread["account_id"], item=request)
        for commitment in commitments:
            self._upsert_edge(thread, profile, source_type="account", source_id=thread["account_id"], relation="promised",
                              target_type="person", target_id=person_id, item=commitment)
        messages_list = thread.get("messages", [])
        for idx, message in enumerate(messages_list):
            if message.get("ownership") == "self" and idx:
                previous = messages_list[idx - 1]
                if previous.get("ownership") == "external":
                    self._upsert_edge(thread, profile, source_type="account", source_id=thread["account_id"], relation="responded_to",
                                      target_type="message", target_id=previous["id"], item=_source(message, message.get("text", "")[:600]))

    def observe(self, thread, approved_profile=None):
        profile = approved_profile or {"version": 0, "facts": [], "preferences": {}, "availability": None}
        # Store normalization is intentionally performed by the caller when a
        # browser read is new.  This guard prevents graph rows for stale reads.
        if not thread.get("account_id") or not thread.get("thread_id") or not thread.get("version"):
            raise ValueError("Stable account, thread, and context version required")
        role_facts = extract_role_facts(thread)
        requests = extract_requests(thread, role_facts)
        commitments = extract_commitments(thread)
        person_id = thread.get("peer_id", "").rstrip("/")
        if not person_id:
            raise ValueError("Stable platform person ID required; names cannot resolve identity")
        profile_hash = digest(profile)
        with self.db:
            self.db.execute('''INSERT INTO recruiter_graph_people VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(account,platform,platform_id) DO UPDATE SET name=excluded.name,
              profile_url=excluded.profile_url,last_seen=excluded.last_seen''',
              (thread["account_id"], "linkedin", person_id, thread.get("peer_name", ""),
               thread.get("url", ""), None, now(), now()))
            for fact in role_facts:
                self._upsert_fact(thread, profile, fact)
            for fact in profile.get("facts", []):
                self._upsert_fact(thread, profile, {"field": "approved_profile", "value": fact["text"],
                                "source_message_ids": [], "source_quote": fact["text"], "status": "approved", "confidence": "approved"},
                                subject_type="profile", subject_id=fact["id"])
            self.db.execute("DELETE FROM recruiter_graph_requests WHERE account=? AND thread=?", (thread["account_id"], thread["thread_id"]))
            for item in requests:
                rid = digest([thread["account_id"], thread["thread_id"], item["question"], item["source_message_ids"]])
                self.db.execute('''INSERT OR REPLACE INTO recruiter_graph_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (rid, thread["account_id"], thread["thread_id"], item["question"], item["exact_text"], item["owner"], item["status"], item.get("due_date"),
                     _json(item["source_message_ids"]), item["source_quote"], item["observed_at"], item["confidence"], thread["version"], profile_hash))
            self.db.execute("DELETE FROM recruiter_graph_commitments WHERE account=? AND thread=?", (thread["account_id"], thread["thread_id"]))
            for item in commitments:
                cid = digest([thread["account_id"], thread["thread_id"], item["text"], item["source_message_ids"]])
                self.db.execute('''INSERT OR REPLACE INTO recruiter_graph_commitments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (cid, thread["account_id"], thread["thread_id"], item["text"], item["owner"], item["status"], item.get("due_date"),
                     _json(item["source_message_ids"]), item["source_quote"], item["observed_at"], item["confidence"], thread["version"], profile_hash))
            self._persist_entities(thread, profile, role_facts, requests, commitments)
            base = self.snapshot(thread, profile, persist=False)
            state = base["conversation_state"]
            self.db.execute('''INSERT OR REPLACE INTO recruiter_graph_conversation_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (thread["account_id"], thread["thread_id"], state["state"], state["response_owner"], _json(base["open_questions"]),
                 state.get("current_objective"), state.get("next_action"), state["turn_count"], state.get("strategy_version"),
                 base["graph_version"], profile_hash, thread["version"], now()))
        return self.snapshot(thread, profile)

    def snapshot(self, thread, approved_profile=None, *, persist=True):
        profile = approved_profile or {"version": 0, "facts": [], "preferences": {}, "availability": None}
        state_row = self.db.execute('SELECT * FROM recruiter_graph_conversation_state WHERE account=? AND thread=?', (thread["account_id"], thread["thread_id"])).fetchone()
        state = dict(state_row) if state_row else None
        if state:
            state = {key: state[key] for key in ("state", "response_owner", "open_questions", "current_objective",
                                                   "next_action", "turn_count", "strategy_version")}
            state["open_questions"] = json.loads(state["open_questions"])
        value = build_snapshot(thread, profile, state=state)
        stored = self.db.execute('''SELECT field,value,source_message_ids,source_quote,observed_at,
          confidence,status FROM recruiter_graph_facts WHERE account=? AND thread=?
          AND subject_type='conversation' AND context_version=?''',
          (thread["account_id"], thread["thread_id"], thread["version"])).fetchall()
        known = {(item.get("field"), item.get("value"), tuple(item.get("source_message_ids", []))) for item in value["role_facts"]}
        for stored_row in stored:
            item = {"field": stored_row["field"], "value": stored_row["value"],
                    "source_message_ids": json.loads(stored_row["source_message_ids"]), "source_quote": stored_row["source_quote"],
                    "observed_at": stored_row["observed_at"], "confidence": stored_row["confidence"], "status": stored_row["status"]}
            key = (item["field"], item["value"], tuple(item["source_message_ids"]))
            if key not in known:
                value["role_facts"].append(item)
                known.add(key)
        value["graph_version"] = digest({k: v for k, v in value.items() if k not in {"graph_version", "graph_policy", "stale"}})
        if persist and state_row and state_row["context_version"] != thread["version"]:
            value["stale"] = True
        return value

    def apply_reader(self, thread, reader_output, approved_profile=None):
        """Persist model-extracted role facts only after source validation."""
        profile = approved_profile or {"version": 0, "facts": [], "preferences": {}, "availability": None}
        messages = {m["id"]: m for m in thread.get("messages", [])}
        with self.db:
            for item in reader_output.get("known_role_facts", []):
                source = messages.get(item.get("source_id"))
                quote = item.get("quote", "")
                if not source or not quote or quote not in source.get("text", ""):
                    raise ValueError("Reader role fact lacks captured source evidence")
                self._upsert_fact(thread, profile, {"field": item["field"], "value": item["value"],
                                "source_message_ids": [item["source_id"]], "source_quote": quote,
                                "confidence": "model_validated", "status": "observed"})
        return self.snapshot(thread, profile)

    def apply_strategy(self, thread, strategy, approved_profile=None):
        profile = approved_profile or {"version": 0, "facts": [], "preferences": {}, "availability": None}
        snap = self.snapshot(thread, profile)
        with self.db:
            self.db.execute('''INSERT OR REPLACE INTO recruiter_graph_conversation_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (thread["account_id"], thread["thread_id"], strategy["relationship_state"], strategy["response_owner"], _json(strategy.get("open_questions", [])),
                 strategy.get("conversation_goal"), strategy.get("next_action"), len(thread.get("messages", [])), strategy.get("strategy_version"),
                 snap["graph_version"], digest(profile), thread["version"], now()))
        return self.snapshot(thread, profile)

    def record_autonomy(self, thread, strategy, text, status="prepared", receipt=None):
        source_id = (thread.get("messages") or [{}])[-1].get("id", "")
        aid = digest([thread["account_id"], thread["thread_id"], source_id, strategy.get("strategy_version"), text])
        with self.db:
            self.db.execute('''INSERT INTO recruiter_graph_autonomy
              (id,account,thread,source_message_id,strategy_version,status,text,receipt,created)
              VALUES(?,?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET status=excluded.status,receipt=excluded.receipt''',
              (aid, thread["account_id"], thread["thread_id"], source_id,
               strategy.get("strategy_version", ""), status, text, _json(receipt) if receipt else None, now()))
        return aid

    def autonomy_allowed(self, thread):
        row = self.db.execute('SELECT enabled FROM recruiter_graph_autonomy_controls WHERE account=? AND thread=?',
                              (thread["account_id"], thread["thread_id"])).fetchone()
        return row is None or bool(row["enabled"])

    def set_thread_autonomy(self, account, thread, enabled, reason=""):
        with self.db:
            self.db.execute('''INSERT OR REPLACE INTO recruiter_graph_autonomy_controls
              VALUES(?,?,?,?,?)''', (account, thread, 1 if enabled else 0, str(reason), now()))

    def autonomous_count_today(self, account, date_prefix=None):
        import datetime as dt
        prefix = date_prefix or dt.datetime.now(dt.timezone.utc).date().isoformat()
        return self.db.execute('''SELECT COUNT(*) AS n FROM recruiter_graph_autonomy
          WHERE account=? AND status IN ('shadow','prepared','sent') AND substr(created,1,10)=?''',
          (account, prefix)).fetchone()["n"]

    def dossier(self, thread, approved_profile=None):
        return self.snapshot(thread, approved_profile)

