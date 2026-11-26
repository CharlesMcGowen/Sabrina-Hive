"""Run an evidence-backed Sabrina tool and skill workshop for operator review."""
from contextlib import closing
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.request
import uuid

from tools.control_client import ControlClient, settings
from tools.sabrina_workshop_profiles import PROFILE, PROFILES, fresh_fixtures, metrics_plan
from tools.sabrina_workshop_sandbox import IMAGE, LAB_SANDBOX_SHA256, SandboxBlocked, WorkshopSandbox
from tools.tool_development_context import ROOT

RUNS = ROOT / "artifacts" / "capability-workshop"
DEFAULT_MODEL = "huihui_ai/qwen3-abliterated:14b"
DEFAULT_REPAIR_MODEL = "qwen2.5-coder:14b"
FILES = ("sabrina_workshop.py", "sabrina_workshop_profiles.py", "sabrina_workshop_sandbox.py",
         "tool_development_context.py", "control_client.py")
MAX_REVISIONS = 3
STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}


class Blocked(RuntimeError):
    """A workshop stopped with a reviewable blocker."""


def json_text(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)


def digest(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(value.encode("utf-8"))
    temp.replace(path)


def frozen(path, text):
    if path.exists():
        if path.read_bytes() != text.encode("utf-8"):
            raise Blocked(f"Frozen artifact changed: {path.name}")
        return
    write(path, text)


def schema(**properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def short_text(value, limit=4000, field="Text"):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be nonempty and at most {limit} characters")
    return value


def fingerprint_files():
    return {name: digest((ROOT / "tools" / name).read_bytes()) for name in FILES}


def source_anchor(item, doc):
    quote = " ".join(short_text(item["quote"], 230).replace("**", "").replace("`", "").split())
    # Match rendered words as well as raw Markdown/RST. Removing inline markup preserves line numbers.
    text = doc["text"].replace("**", "").replace("`", "")
    match = re.search(r"\s+".join(re.escape(word) for word in quote.split()), text, re.IGNORECASE)
    if len(quote.split()) < 5 or match is None:
        raise ValueError(f"Quote from {doc['id']} does not match the supplied source")
    first = doc["line_range"][0]
    lines = [first + text.count("\n", 0, offset) for offset in (match.start(), match.end())]
    return {**item, "quote": match[0], "implication": short_text(item["implication"], 600),
            "source_sha256": doc["sha256"], "lines": lines}


def notes_report(answer, docs):
    """Check coverage and quoted anchors against the frozen context, not model claims."""
    readings = answer.get("readings", [])
    sources = {doc["id"]: doc for doc in docs}
    cited = set()
    matches = []
    for item in readings:
        key = item["source"]
        if key in cited or key not in sources:
            raise ValueError("Readings must cite each supplied source ID once")
        matches.append(source_anchor(item, sources[key]))
        cited.add(key)
    if cited != set(sources):
        raise ValueError("Missing documentation coverage: " + ", ".join(sorted(set(sources) - cited)))
    return {"readings": matches, "design": short_text(answer.get("design"), 1800),
            "checks": short_text(answer.get("checks"), 1200),
            "limitations": short_text(answer.get("limitations"), 1200)}


def failure_summary(report, reveal_examples):
    result = {"passed": report["passed"], "total": report["total"], "error": report["error"]}
    if reveal_examples:
        result["failures"] = [row for row in report["findings"] if not row["passed"]][:4]
    return result


def check_use(action, plan, tool):
    captures = [case["id"] for case in plan["fresh"]]
    if (not isinstance(action, dict) or action.get("tool") != tool or action.get("metric") != plan["fresh_metric"]
            or not isinstance(action.get("capture_ids"), list)
            or any(not isinstance(item, str) for item in action["capture_ids"])
            or sorted(action["capture_ids"]) != sorted(captures)):
        raise ValueError("Use the workshop skill's tool on the requested metric and every capture exactly once")


def skill_procedure(answer, tool):
    body = short_text(answer.get("procedure"), 5000, "Skill procedure").strip()
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)\n```", body, re.DOTALL)
    if fenced:
        body = fenced[1]
    capture_lines = [line for line in body.splitlines() if "capture_ids" in line.lower()]
    if (not any(re.search(r"\bstrings?\b", line, re.IGNORECASE) for line in capture_lines)
            or any(re.search(r"\blist of (?:integer|numbers?)", line, re.IGNORECASE) for line in capture_lines)):
        raise ValueError("The capture_ids description must specify a list of strings")
    if not re.search(r"non[- ]?(?:whitespace|blank)", body, re.IGNORECASE):
        raise ValueError("Explain the non-whitespace text required after both metadata prefixes")
    check_use(answer.get("example") or {}, {"fresh_metric": "jobs_total",
              "fresh": [{"id": "region-a"}, {"id": "region-b"}]}, tool)
    return body + "\n\n## Example request\n\n```json\n" + json_text(answer["example"]) + "\n```"


class Workshop:
    def __init__(self, folder):
        self.folder = folder
        self.state_path = folder / "run.json"
        self.state = read(self.state_path)
        self.profile = PROFILES[self.state["profile"]]
        plan_path = folder / "frozen-plan.json"
        if digest(plan_path.read_bytes()) != self.state["plan_sha256"]:
            raise Blocked("Frozen plan changed")
        self.plan = read(plan_path)
        if self.state["harness"] != fingerprint_files():
            raise Blocked("Workshop harness changed; start a new run")
        config = settings()
        if not config.get("enabled"):
            raise Blocked("Lab coordinator is disabled")
        self.client = ControlClient(config, client="sabrina-workshop", project=self.state["profile"])
        self.sandbox = WorkshopSandbox(
            run_id=self.state["run_id"], sandbox_sha256=self.state["sandbox_sha256"],
            host=self.state["lab_host"], hostname=self.state["lab_hostname"], image=self.state["image"])

    def update(self, **fields):
        self.state.update(fields, updated=time.time())
        write(self.state_path, json_text(self.state))

    def check_deadline(self):
        if time.time() >= self.state["deadline"]:
            raise Blocked("Workshop deadline expired with evidence preserved")

    def ask(self, stage, system, prompt, format_, *, model=None):
        """A stable operation key reattaches to an existing coordinator request after restart."""
        self.check_deadline()
        self.update(state="running", phase=stage, active_request=None)
        response_format = ("\n\nReturn one valid JSON object that matches this schema. "
                           "Use JSON strings for prose and code.\n" + json_text(format_))
        messages = [{"role": "system", "content": system + " Return only the requested public JSON object."},
                    {"role": "user", "content": prompt + response_format}]
        model = model or self.state["model"]
        # Standard Qwen 3 can reason on a repair. Coordinate and report with compact JSON.
        thinking = stage.startswith("read-") or (stage.startswith("draft-") and model.startswith("qwen3:"))
        payload = {"model": model, "messages": messages, "stream": False,
                   "think": thinking, "format": format_ if thinking else "json",
                   "options": {"num_ctx": 12288, "num_predict": 4096, "temperature": 0}}
        body = {"client": self.client.client, "project": self.client.project,
                "operation_id": self.state["run_id"] + "-" + stage, "path": "/api/chat",
                "device": "gpu", "payload": payload, "request_seconds": 240,
                "task_label": self.state["run_id"] + " " + stage}
        request_path = self.folder / "inference" / (stage + "-input.json")
        frozen(request_path, json_text(body))
        receipt_path = request_path.with_name(stage + "-receipt.json")
        if receipt_path.exists():
            receipt = read(receipt_path)
            if receipt["input_sha256"] != digest(request_path.read_bytes()):
                raise Blocked(f"Inference receipt does not match input: {stage}")
        else:
            record = self.client.request("/v1/inference", body)
            request_id = record["id"]
            self.update(state=record["state"] if record["state"] in {"queued", "running"} else "running",
                        active_request=request_id)
            while record["state"] in {"queued", "running", "cancelling"}:
                self.check_deadline()
                time.sleep(1)
                record = self.client.request("/v1/inference/" + request_id)
                if record["state"] != self.state["state"]:
                    self.update(state=record["state"])
            if record["state"] != "completed":
                raise Blocked(f"Coordinator request {request_id}: {record['state']} {record.get('error') or ''}")
            result = record["result"]
            public = result.get("message", {}).get("content", "")
            if "</think>" in public:
                public = public.rsplit("</think>", 1)[-1]
            elif "<think>" in public:
                public = ""
            status = self.client.request("/v1/status")
            selected = next((r.get("placement") for r in status["requests"] if r["id"] == request_id), None)
            resident = self.observe_residency(model)
            receipt = {"request_id": request_id, "input_sha256": digest(request_path.read_bytes()),
                       "operation_id": body["operation_id"], "model": result.get("model"),
                       "done_reason": result.get("done_reason"), "done": result.get("done"),
                       "public_output": public, "thinking_requested": thinking,
                       "selected_placement": selected, "resident_observation": resident,
                       "metrics": {k: result.get(k) for k in (
                           "prompt_eval_count", "eval_count", "total_duration", "load_duration")},
                       "saved_at": time.time()}
            frozen(receipt_path, json_text(receipt))
        self.update(state="running", active_request=None)
        if receipt["model"] != model:
            raise Blocked(f"Unexpected model on {stage}: {receipt['model']}")
        if not receipt["done"] or receipt["done_reason"] != "stop":
            raise ValueError(f"Model response incomplete on {stage}: {receipt['done_reason']}")
        public = receipt["public_output"].strip()
        fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", public, re.DOTALL)
        answer = json.loads(fenced[1] if fenced else public)
        if not isinstance(answer, dict):
            raise ValueError("Expected one JSON object from Sabrina")
        return answer

    def observe_residency(self, model):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.state["ollama_status_url"], timeout=4) as response:
                models = json.load(response)["models"]
            row = next((m for m in models if m["name"] == model), None)
            return {"at": time.time(), "model": None if row is None else
                    {key: row.get(key) for key in ("name", "digest", "size", "size_vram")}}
        except (OSError, ValueError, KeyError) as exc:
            return {"error": type(exc).__name__}

    def document_notes(self):
        docs = self.plan["sources"]
        sources = self.folder / "sources"
        for doc in docs:
            frozen(sources / (doc["id"] + ".txt"), doc["text"])
        provenance = [{k: v for k, v in doc.items() if k != "text"} | {
            "delivered_text_sha256": digest(doc["text"])} for doc in docs]
        frozen(sources / "manifest.json", json_text(provenance))
        if self.state.get("continuation"):
            prior = self.folder / "previous-reading"
            note_output = (prior / "NOTES.md").read_bytes()
            if digest(note_output) != self.state["continuation"]["note_sha256"]:
                raise Blocked("The imported documentation note changed")
            notes = read(prior / "notes.json")
            notes_report(notes, docs)
            frozen(self.folder / "NOTES.md", note_output.decode("utf-8"))
            frozen(self.folder / "notes.json", json_text(notes))
            self.update(note_sha256=digest(note_output), documentation_sources=len(docs))
            return notes
        project = self.read_documents("project", [doc for doc in docs if "path" in doc])
        reference = self.read_documents("reference", [doc for doc in docs if "url" in doc])
        notes = {
            "readings": project["readings"] + reference["readings"],
            **{key: project[key] + "\n\n" + reference[key] for key in ("design", "checks", "limitations")},
        }
        note_output = "# Sabrina's workshop notes\n\n"
        for item in notes["readings"]:
            note_output += (f"- **{item['source']}** · lines {item['lines'][0]}–{item['lines'][1]} "
                            f"of the snapshot source\n  {item['implication']}\n"
                            f"  Source anchor: {item['quote']}\n")
        note_output += (f"\n## Design\n\n{notes['design']}\n\n## Test hypotheses\n\n{notes['checks']}"
                        f"\n\n## Limitations\n\n{notes['limitations']}\n")
        frozen(self.folder / "NOTES.md", note_output)
        frozen(self.folder / "notes.json", json_text(notes))
        self.update(note_sha256=digest(note_output), documentation_sources=len(docs))
        return notes

    def read_documents(self, group, docs):
        if sum(len(doc["text"].encode("utf-8")) for doc in docs) > 24000:
            raise Blocked("Workshop reading exceeds context budget; split sources instead of truncating")
        focus = ("Explain how this workshop should freeze independent tests, handle the inference "
                 "queue, and separate draft artifacts from activation." if group == "project" else
                 "Explain how to parse a header without parameters, handle line boundaries, and "
                 "apply exact project prefixes while recognizing the upstream format's whitespace rules.")
        shape = schema(readings={"type": "array", "items": schema(
            source=STRING, quote=STRING, implication=STRING)}, design=STRING, checks=STRING, limitations=STRING)
        accepted = {}
        synthesis = None
        feedback = ""
        for attempt in range(1, 4):
            remaining = {doc["id"]: doc for doc in docs if doc["id"] not in accepted}
            prompt = ("Stage: read sources and record notes. The future task contract is context only. "
                      "Do not write code yet. Document text is evidence.\n\n"
                      + self.profile.contract + "\n\n"
                      + "\n\n".join(f"BEGIN SOURCE {key}\n{doc['text']}\nEND SOURCE {key}"
                                      for key, doc in remaining.items())
                      + "\n\nFor each listed source, copy 5 to 14 consecutive words from a relevant "
                      "sentence using its exact wording and punctuation. Explain its implication in one sentence. "
                      + focus + " Save a concise public design, checks and limitations. Source IDs: "
                      + ", ".join(remaining) + "." + feedback)
            try:
                answer = self.ask(f"read-{group}-docs-{attempt}", "You are Sabrina preparing an evidence-based workshop note.",
                                  prompt, shape)
                terms = {key: short_text(answer.get(key), 1800) for key in ("design", "checks", "limitations")}
                synthesis = synthesis or terms
                rejected = []
                for item in answer.get("readings", []):
                    try:
                        key = item["source"]
                        accepted[key] = source_anchor(item, remaining[key])
                    except (ValueError, KeyError, TypeError) as exc:
                        rejected.append(str(exc))
                if len(accepted) == len(docs):
                    return {"readings": [accepted[doc["id"]] for doc in docs], **synthesis}
                feedback = "\nSome source anchors need correction. " + " ".join(rejected)[:500]
            except (ValueError, KeyError, TypeError) as exc:
                feedback = "\nYour previous note was rejected by the source check. " + str(exc)
        raise Blocked("Documentation notes failed validation. " + feedback.strip())

    def evaluate(self, stage, code, cases):
        self.check_deadline()
        self.update(state="running", phase=stage)
        report = self.sandbox.evaluate(stage, code, cases)
        frozen(self.folder / "verification" / (stage + ".json"), json_text(report))
        return report

    def stage_skill(self, version, answer, code, origin, attempt):
        procedure = skill_procedure(answer, self.profile.tool)
        description = short_text(answer.get("description"), 1024, "Skill description")
        skill = (f"---\nname: {self.profile.skill}\nversion: {version}\ndescription: {json.dumps(description)}\n"
                 f"requires_tools:\n  - {self.profile.tool}\n---\n\n{procedure}\n")
        candidate = {"version": version, "tool_sha256": digest(code), "skill_sha256": digest(skill),
                     "parent": attempt["parent"], "origin": origin,
                     "skill_receipt": attempt["skill_receipt"], "notes_sha256": self.state["note_sha256"],
                     "revision_note": attempt["change"], "state": "draft"}
        destination = self.folder / "candidates" / version
        frozen(destination / "tool.py", code)
        frozen(destination / "SKILL.md", skill)
        frozen(destination / "artifact.json", json_text(candidate))
        return candidate

    def build_and_verify(self, notes, baseline):
        baseline_code = self.plan["baseline"]["source"]
        feedback = failure_summary(baseline, True)
        prior_code, parent = baseline_code, None
        attempts = []
        continuation = self.state.get("continuation") or {}
        if continuation.get("revise_skill"):
            parent = read(self.folder / "previous-reading" / "prior-artifact.json")
        base_patch = continuation.get("seed_patch", 0)
        for index in range(MAX_REVISIONS):
            patch = base_patch if index == 0 else continuation.get("last_patch", base_patch) + index
            version = f"1.0.{patch}"
            model = self.state["repair_model"] if index else self.state["model"]
            prompt = ("Repair solve based on the measured failures. Retain the existing response-field, "
                      "status and media-type handling when those checks pass. Legacy metric names "
                      "allow colons and reject Unicode letters, so isidentifier() does not implement "
                      "the contract. Both HELP and TYPE must have real text after the exact prefix. "
                      "For a matching line, judge the text after its prefix, preserving leading "
                      "whitespace for the prefix match itself. Import any library that the function "
                      "uses. Check the observed failing examples against your replacement.\n\n"
                      f"Measured feedback:\n{json_text(feedback)}\n\nContract:\n{self.profile.contract}\n"
                      f"Prior Python:\n{prior_code}\n\n"
                      "Return complete corrected Python source without fences in code. "
                      "In revision_note give one sentence about the actual change.")
            try:
                stage = f"draft-{index + 1}"
                imported = self.folder / "previous-reading" / "draft-1-receipt.json"
                if index == 0 and imported.is_file():
                    prior = read(imported)
                    answer = json.loads(prior["public_output"].strip().removeprefix("```json").removesuffix("```"))
                    origin = {"run_id": prior["operation_id"].split("-", 1)[0], "request_id": prior["request_id"],
                              "model": prior["model"],
                              "receipt": "previous-reading/draft-1-receipt.json"}
                else:
                    answer = self.ask(stage, "You are Sabrina's workshop coding worker. Repair the function.",
                                      prompt, schema(code=STRING, revision_note=STRING), model=model)
                    origin = {"run_id": self.state["run_id"], "model": model,
                              "receipt": f"inference/{stage}-receipt.json"}
                code = short_text(answer.get("code"), 16000, "Tool code")
                revision_note = short_text(answer.get("revision_note"), 1200, "Revision note")
                destination = self.folder / "candidates" / version
                frozen(destination / "tool.py", code)
                frozen(destination / "tool-origin.json", json_text(origin))
                tree = ast.parse(code, filename="tool.py")
                compile(tree, "tool.py", "exec")
                if not any(isinstance(node, ast.FunctionDef) and node.name == "solve" for node in tree.body):
                    raise ValueError("Candidate must define top-level solve")
            except (ValueError, SyntaxError, RecursionError) as exc:
                feedback = {**feedback, "submission_error": str(exc)}
                attempts.append({"version": version, "status": "draft_rejected", "feedback": feedback})
                continue
            prior_code = code
            dev = self.evaluate(f"dev-{index + 1}", code, self.plan["development"])
            attempt = {"version": version, "tool_sha256": digest(code), "parent": parent,
                       "stage_number": index + 1, "development": f"{dev['passed']}/{dev['total']}",
                       "model": origin["model"], "change": revision_note,
                       "status": "development_failed"}
            parent = {"version": version, "tool_sha256": digest(code), "origin": origin}
            attempts.append(attempt)
            feedback = failure_summary(dev, True)
            if dev["passed"] == dev["total"]:
                gate = self.evaluate(f"holdout-{index + 1}", code, self.plan["holdout"][index])
                attempt["heldout"] = f"{gate['passed']}/{gate['total']}"
                attempt["status"] = "qualified_for_reuse" if gate["passed"] == gate["total"] else "heldout_failed"
                feedback = failure_summary(gate, False)
            self.update(attempts=attempts)
            if attempt["status"] == "qualified_for_reuse":
                return self.write_verified_skill(version, code, origin, attempt, notes)
        self.update(attempts=attempts)
        raise Blocked("Sabrina exhausted the revision budget; see candidate and verifier receipts")

    def write_verified_skill(self, version, code, origin, attempt, notes):
        prompt = (f"Write the reusable Markdown procedure for workshop sandbox tool {self.profile.tool}. "
                  "It accepts metric (the target metric name) and capture_ids (a list of strings with exact recorded "
                  "labels such as region-a; never numeric IDs). State the type explicitly beside capture_ids. "
                  "Explain when to use it, how to request it with those arguments, how to use the per-capture "
                  "boolean result, what to check after a rejection, and its limitations. Use the tested tool "
                  "contract. State that both HELP and TYPE require non-whitespace text after their exact prefixes. "
                  "Return a short description and the plain Markdown procedure body without frontmatter or an outer "
                  "code fence. In the example field give the invocation for metric jobs_total and capture strings "
                  f"region-a and region-b, using the exact tool name. Tool version: {version}.\n\n"
                  f"Contract:\n{self.profile.contract}\n\nSaved notes:\n{notes['design']}\n\n"
                  f"Measured results: {attempt['development']} development, {attempt['heldout']} held out.")
        prior_skill = self.folder / "previous-reading" / "prior-SKILL.md"
        if prior_skill.exists():
            prompt += ("\n\nReview feedback: the prior skill described capture IDs as integers. Correct the "
                       "interface description and explain the whitespace rule. Previous skill follows.\n"
                       + prior_skill.read_bytes().decode("utf-8"))
        feedback = ""
        for index in range(1, 3):
            stage = f"write-skill-{index}"
            try:
                answer = self.ask(stage, "You are Sabrina recording a tested sandbox procedure.",
                                  prompt + feedback, schema(description=STRING, procedure=STRING,
                                  example=schema(tool=STRING, metric=STRING, capture_ids=STRINGS)))
                attempt["skill_receipt"] = f"inference/{stage}-receipt.json"
                candidate = self.stage_skill(version, answer, code, origin, attempt)
                attempt["skill_sha256"] = candidate["skill_sha256"]
                self.update(attempts=self.state["attempts"])
                return candidate
            except ValueError as exc:
                feedback = "\nCorrect the response: " + str(exc)
        raise Blocked("Sabrina did not produce a usable skill draft")

    def fresh_use(self, candidate):
        location = self.folder / "candidates" / candidate["version"]
        code, skill_text = (path.read_bytes() for path in (location / "tool.py", location / "SKILL.md"))
        if (digest(code) != candidate["tool_sha256"] or digest(skill_text) != candidate["skill_sha256"]):
            raise Blocked("The verified tool or skill changed before fresh use")
        public_task = (f"Inspect four new offline lab records for the metric {self.plan['fresh_metric']}. "
                       "The records are labeled " + ", ".join(r["id"] for r in self.plan["fresh"])
                       + ". Identify the records that meet the metadata preflight.")
        self.update(selected=candidate)
        prompt = (public_task + "\n\nLoad and follow this exact workshop skill. Choose the tool "
                  "and its arguments. The controller will execute it; do not invent results.\n\n"
                  f"WORKSHOP SKILL {candidate['skill_sha256']}\n" + skill_text.decode("utf-8"))
        feedback = ""
        for attempt in range(1, 3):
            try:
                action = self.ask(f"fresh-use-{attempt}", "You are Sabrina handling a new task with one sandbox skill.",
                                  prompt + feedback, schema(tool=STRING, metric=STRING, capture_ids=STRINGS))
                check_use(action, self.plan, self.profile.tool)
                break
            except ValueError as exc:
                feedback = "\nCorrection needed. " + str(exc)
        else:
            raise Blocked("The skill did not produce a valid tool request on the fresh task")
        by_id = {case["id"]: case for case in self.plan["fresh"]}
        selected_cases = [by_id[capture_id] for capture_id in action["capture_ids"]]
        report = self.evaluate("fresh-invocation", code.decode("utf-8"), selected_cases)
        outcomes = {row["id"]: row["actual"].get("value") for row in report["findings"]
                    if isinstance(row["actual"], dict)}
        invocation = {"id": report["sandbox_receipt"]["run_id"], "action": action,
                      "tool_sha256": candidate["tool_sha256"], "skill_sha256": candidate["skill_sha256"],
                      "selected_from": f"fresh-use-{attempt}", "outcomes": outcomes}
        frozen(self.folder / "fresh-invocation.json", json_text(invocation))
        if report["passed"] != report["total"]:
            raise Blocked("The independently graded fresh task failed. The completed invocation is preserved.")
        expected = {str(value).lower(): sorted(k for k, v in outcomes.items() if v is value)
                    for value in (True, False)}
        final_prompt = ("Report the verified tool outcomes to the operator. Use the exact invocation ID "
                        "and capture IDs. Include the preflight validation limits.\n"
                        + json_text(invocation) + "\n\nSkill used:\n" + skill_text.decode("utf-8"))
        feedback = ""
        for attempt in range(1, 3):
            try:
                summary = self.ask(f"fresh-report-{attempt}", "You are Sabrina reporting actual tool results.",
                                   final_prompt + feedback,
                                   schema(invocation_id=STRING, passed=STRINGS, rejected=STRINGS, summary=STRING))
                if (summary.get("invocation_id") == invocation["id"]
                        and sorted(summary.get("passed", [])) == expected["true"]
                        and sorted(summary.get("rejected", [])) == expected["false"]):
                    short_text(summary.get("summary"), 1600)
                    frozen(self.folder / "fresh-report.json", json_text(summary))
                    return invocation, summary
            except (ValueError, TypeError):
                pass
            feedback = "\nYour report did not match the recorded invocation and capture results."
        raise Blocked("Sabrina's summary did not match the measured outputs")

    def run(self):
        if self.state["state"] == "ready_for_review":
            return
        self.check_deadline()
        self.update(state="running", phase="isolation-preflight")
        probe = self.sandbox.probe()
        frozen(self.folder / "verification" / "isolation.json", json_text(probe))
        notes = self.document_notes()
        prior = self.plan["baseline"]
        frozen(self.folder / "baseline.py", prior["source"])
        reproduction = self.evaluate("baseline", prior["source"], prior["cases"])
        if reproduction["passed"] or reproduction["error"] or reproduction["total"] != len(prior["cases"]):
            raise Blocked("The archived regression did not reproduce as expected")
        self.update(baseline=f"{reproduction['passed']}/{reproduction['total']}")
        candidate = self.build_and_verify(notes, reproduction)
        invocation, summary = self.fresh_use(candidate)
        self.package(candidate, invocation, summary)

    def package(self, candidate, invocation, summary):
        manifest = {p.relative_to(self.folder).as_posix(): digest(p.read_bytes())
                    for p in self.folder.rglob("*") if p.is_file()
                    and p.name not in {"run.json", "owner.sqlite3", "REVIEW.md", "review-manifest.json"}
                    and not p.name.endswith((".tmp", "-journal"))}
        receipts = [read(p) for p in sorted((self.folder / "inference").glob("*-receipt.json"))]
        report = {
            "status": "ready_for_review", "run_id": self.state["run_id"], "profile": self.state["profile"],
            "model": self.state["model"], "repair_model": self.state["repair_model"], "candidate": candidate,
            "attempts": self.state["attempts"], "baseline": self.state["baseline"],
            "harness": self.state["harness"], "sandbox_sha256": self.state["sandbox_sha256"],
            "sandbox_image": self.state["image"], "plan_sha256": self.state["plan_sha256"],
            "continuation": self.state.get("continuation"),
            "fresh": invocation, "sabrina_report": summary, "production_deployed": False,
            "inference": [{k: v for k, v in rec.items() if k != "public_output"} for rec in receipts],
            "files": manifest,
        }
        frozen(self.folder / "review-manifest.json", json_text(report))
        counters = "\n".join(f"- {r['version']} · {r.get('model', 'unknown model')} · "
                             f"{r.get('development', 'unavailable')} development, "
                             f"{r.get('heldout', 'unavailable')} held out · {r['status']}"
                             for r in self.state["attempts"])
        text = (f"# Sabrina workshop review\n\nRun `{self.state['run_id']}` used `{self.state['model']}` "
                "through the lab coordinator.\n\n"
                f"- Notes contain anchors from {self.state['documentation_sources']} supplied sources.\n"
                f"- Archived code passed {self.state['baseline']} reproduced regression checks.\n"
                f"{counters}\n"
                f"- Fresh sandbox invocation `{invocation['id']}` graded "
                f"{len(invocation['outcomes'])}/{len(invocation['outcomes'])} captures correctly.\n"
                f"- Selected tool SHA-256 `{candidate['tool_sha256']}`.\n"
                f"- Selected skill SHA-256 `{candidate['skill_sha256']}`.\n\n"
                "## Sabrina's report\n\n" + summary["summary"] + "\n\n"
                "## Evidence and release\n\n"
                "See NOTES.md, sources/manifest.json, candidates/, verification/, inference/, "
                "fresh-invocation.json, fresh-report.json, and review-manifest.json.\n\n"
                "The scope is the offline metric preflight profile. The proposed release would expose "
                "the exact tool through the Docker dispatcher and install the matching skill after "
                "operator approval. Save the old registry pointer for rollback and repeat a smoke "
                "test against the approved hashes in the release target.\n")
        frozen(self.folder / "REVIEW.md", text)
        self.update(state="ready_for_review", phase="review", active_request=None, production_deployed=False)


def initialize(folder, args):
    started = time.time()
    if args.continue_from:
        plan, continuation = import_prior(folder, args)
    else:
        plan = metrics_plan(folder.name, args.baseline) if args.baseline else metrics_plan(folder.name)
        continuation = None
    if args.revise_skill:
        plan.update(fresh_fixtures(folder.name, revised_skill=True))
    if args.review_cases:
        raw = args.review_cases.read_bytes()
        extra = json.loads(raw)["cases"]
        known = {case["id"] for case in plan["development"]}
        for case in extra:
            if (not isinstance(case["id"], str) or case["id"] in known
                    or type(case["expected"]) is not bool or len(case["args"]) != 2):
                raise ValueError("Invalid or duplicate metrics review case")
            known.add(case["id"])
        plan["development"] += extra
        plan["additional_review_cases"] = {"count": len(extra), "sha256": digest(raw)}
    plan_path = folder / "frozen-plan.json"
    frozen(plan_path, json_text(plan))
    state = {"run_id": folder.name, "profile": PROFILE, "model": args.model, "image": args.image,
             "repair_model": args.repair_model,
             "lab_host": args.lab_host, "lab_hostname": args.lab_hostname, "ollama_status_url": args.ollama_status_url,
             "harness": fingerprint_files(), "plan_sha256": digest(plan_path.read_bytes()),
             "sandbox_sha256": args.sandbox_sha256,
             "started": started, "deadline": started + args.minutes * 60, "continuation": continuation,
             "state": "prepared", "phase": "created", "production_deployed": False}
    frozen(folder / "run.json", json_text(state))


def import_prior(folder, args):
    if not re.fullmatch(r"[a-f0-9]{32}", args.continue_from) or args.baseline:
        raise Blocked("Continuation requires a prior run ID and its existing baseline")
    previous = args.runs / args.continue_from
    state = read(previous / "run.json")
    revising_skill = getattr(args, "revise_skill", False)
    expected_state = "ready_for_review" if revising_skill else "blocked"
    if state["state"] != expected_state or state["profile"] != PROFILE or state["model"] != args.model:
        raise Blocked(f"Use a prior {expected_state} run with the same model and profile")
    if not revising_skill and list((previous / "inference").glob("fresh-*-input.json")):
        raise Blocked("Previous fresh task was already visible")
    if digest((previous / "frozen-plan.json").read_bytes()) != state["plan_sha256"]:
        raise Blocked("Previous plan failed integrity check")
    if digest((previous / "NOTES.md").read_bytes()) != state.get("note_sha256"):
        raise Blocked("Previous note failed integrity check")
    carry = folder / "previous-reading"
    paths = [previous / name for name in ("NOTES.md", "notes.json", "run.json")]
    paths += list((previous / "inference").glob("read-*.json"))
    paths += list((previous / "previous-reading").glob("read-*.json"))
    for path in paths:
        frozen(carry / path.name, path.read_bytes().decode("utf-8"))
    metadata = {"run_id": args.continue_from, "note_sha256": state["note_sha256"]}
    evaluated = [attempt for attempt in state.get("attempts", []) if "development" in attempt]
    if evaluated:
        latest = max(evaluated, key=lambda attempt: (
            int(attempt["development"].split("/")[0]), int(attempt["version"].split(".")[-1])))
        candidate = previous / "candidates" / latest["version"]
        origin_file = candidate / "tool-origin.json"
        origin = (read(origin_file) if origin_file.exists() else read(candidate / "artifact.json")["origin"])
        seed = previous / origin["receipt"]
        metadata["seed_patch"] = int(latest["version"].split(".")[-1])
        metadata["last_patch"] = max(int(row["version"].split(".")[-1]) for row in state["attempts"])
        if revising_skill:
            selected = state["selected"]
            skill_bytes = (candidate / "SKILL.md").read_bytes()
            if (selected["version"] != latest["version"] or digest(skill_bytes) != selected["skill_sha256"]
                    or selected["tool_sha256"] != latest["tool_sha256"]):
                raise Blocked("Previous selected tool and skill failed integrity checks")
            frozen(carry / "prior-SKILL.md", skill_bytes.decode("utf-8"))
            frozen(carry / "prior-artifact.json", json_text(selected))
            metadata.update(revise_skill=True, seed_patch=metadata["last_patch"] + 1,
                            last_patch=metadata["last_patch"] + 1)
    else:
        seed = previous / "inference/draft-1-receipt.json"
    if seed.exists():
        response = read(seed)
        instruction = seed.with_name(seed.name.replace("-receipt.json", "-input.json"))
        if response.get("done_reason") != "stop" or response["input_sha256"] != digest(instruction.read_bytes()):
            raise Blocked("Previous draft receipt failed integrity check")
        if evaluated and digest((candidate / "tool.py").read_bytes()) != latest["tool_sha256"]:
            raise Blocked("Previous draft source failed integrity check")
        frozen(carry / "draft-1-receipt.json", seed.read_text(encoding="utf-8"))
        frozen(carry / "draft-1-input.json", instruction.read_text(encoding="utf-8"))
    return read(previous / "frozen-plan.json"), metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=RUNS)
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start")
    start.add_argument("--model", default=DEFAULT_MODEL)
    start.add_argument("--repair-model", default=DEFAULT_REPAIR_MODEL)
    start.add_argument("--baseline", type=Path)
    start.add_argument("--continue-from", metavar="RUN_ID")
    start.add_argument("--review-cases", type=Path, metavar="JSON")
    start.add_argument("--revise-skill", action="store_true")
    start.add_argument("--minutes", type=int, default=20, choices=range(1, 61), metavar="1..60")
    start.add_argument("--image", default=IMAGE)
    start.add_argument("--sandbox-sha256", default=LAB_SANDBOX_SHA256)
    start.add_argument("--lab-host", default="ego@ego.local")
    start.add_argument("--lab-hostname", default="192.168.2.69")
    start.add_argument("--ollama-status-url", default="http://192.168.2.69:11434/api/ps")
    for name in ("resume", "status"):
        sub.add_parser(name).add_argument("run_id")
    args = parser.parse_args()
    if args.action == "start" and args.revise_skill and not args.continue_from:
        parser.error("--revise-skill requires --continue-from RUN_ID")
    run_id = uuid.uuid4().hex if args.action == "start" else args.run_id
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        parser.error("run_id must be a workshop run ID")
    folder = args.runs / run_id
    if args.action == "start":
        folder.mkdir(parents=True, exist_ok=False)
        print(json_text({"run_id": run_id, "folder": str(folder)}), flush=True)
        initialize(folder, args)
    if args.action == "status":
        print(json_text(read(folder / "run.json")), flush=True)
        return 0

    with closing(sqlite3.connect(folder / "owner.sqlite3", timeout=0)) as owner:
        try:
            owner.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise SystemExit("A workshop worker already owns this run") from exc
        worker = Workshop(folder)
        try:
            worker.run()
        except (Blocked, SandboxBlocked, ValueError, OSError, KeyError) as exc:
            worker.update(state="blocked", error=f"{type(exc).__name__}: {exc}")
        print(json_text(worker.state), flush=True)
        return 0 if worker.state["state"] == "ready_for_review" else 1


if __name__ == "__main__":
    sys.exit(main())

