"""Operator-owned workshop profiles. Expected results stay outside model prompts."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.request

from tools.tool_development_context import ROOT, required_documents

PROFILE = "metrics-response-v1"
TOOL = "check_metrics_captures"
SKILL = "metrics-response-preflight"
BASELINE = ROOT / "artifacts/wsp-recovery2-edit-20261115/early-metrics-sources.jsonl"
BASELINE_SHA256 = "3d5fe84cd55c98a19c457a32db3ccb743247a3a5264fea6fce6197305c380846"

CONTRACT = """Implement solve(response, metric), a pure Python function that returns a bool.
Use only the standard library. Do not perform I/O or mutate any data.
The input is a captured HTTP response. Require a dict with status of type int
equal to 200 and strings content_type and body. Missing or wrong types return
False. The metric must be a string matching [A-Za-z_:][A-Za-z0-9_:]* in full.
Check that the media type before the first semicolon (or the whole header if
there is no semicolon), trimmed and compared case-insensitively, is text/plain.
Use Python string line boundaries, including LF and CRLF. Require BOTH a line
starting exactly with '# HELP <metric> ' and a line starting exactly with
'# TYPE <metric> ', each followed by some non-whitespace content. Do not strip
leading whitespace from metadata lines. Values may have extra whitespace
after the required prefix. The two lines can occur anywhere in the capture.
Reject a similarly named metric, HELP/TYPE lookalikes, and a leading space or
tab before '#'. This stricter project preflight checks metadata presence only.
It does not validate sample syntax, ordering, type names or whole exposition.
"""


@dataclass(frozen=True)
class WorkshopProfile:
    tool: str
    skill: str
    contract: str


PROFILES = {PROFILE: WorkshopProfile(TOOL, SKILL, CONTRACT)}


def _document_section(raw, start, stop):
    text = raw.decode("utf-8")
    opening = re.search("^" + re.escape(start) + r".*$", text, re.MULTILINE)
    if opening is None:
        raise ValueError(f"Required document heading missing: {start}")
    next_heading = re.search(stop, text[opening.end():], re.MULTILINE)
    if next_heading is None:
        raise ValueError(f"Could not verify the end of document section: {start}")
    end = opening.end() + next_heading.start()
    section = text[opening.start():end].rstrip()
    begin_line = text.count("\n", 0, opening.start()) + 1
    return section, [begin_line, begin_line + len(section.splitlines()) - 1]


def documents(root=ROOT):
    """Snapshot full project docs and the complete selected upstream sections."""
    docs = []
    for key, doc in zip(("sandbox-skill", "lifecycle", "coordinator"), required_documents(root=root), strict=True):
        docs.append({**doc, "id": key, "scope": "complete document", "line_range": [1, doc["lines"]]})
    upstream = (
        ("https://raw.githubusercontent.com/prometheus/docs/main/docs/instrumenting/exposition_formats.md",
         "https://prometheus.io/docs/instrumenting/exposition_formats/#prometheus-text-format",
         (("exposition", "## Prometheus Text Format", r"^## "),)),
        ("https://raw.githubusercontent.com/python/cpython/v3.11.14/Doc/library/stdtypes.rst",
         "https://docs.python.org/3.11/library/stdtypes.html",
         (("python-header", ".. method:: str.partition(", r"^\.\. (?:method|staticmethod)::"),
          ("python-lines", ".. method:: str.splitlines(", r"^\.\. (?:method|staticmethod)::"),
          ("python-prefix", ".. method:: str.startswith(", r"^\.\. (?:method|staticmethod)::"))),
    )
    for url, reference, sections in upstream:
        with urllib.request.urlopen(url, timeout=15) as response:
            raw = response.read(512001)
            if len(raw) > 512000:
                raise ValueError("Upstream document exceeds the snapshot budget")
            retrieved = time.time()
            etag = response.headers.get("ETag")
        source_hash = hashlib.sha256(raw).hexdigest()
        for key, start, stop in sections:
            text, lines = _document_section(raw, start, stop)
            docs.append({"id": key, "url": url, "reference": reference, "sha256": source_hash,
                         "etag": etag, "bytes": len(raw), "retrieved_at": retrieved,
                         "scope": "complete selected section", "line_range": lines, "text": text})
    return docs


def _fixture(id_, response, metric, expected):
    return {"id": id_, "args": [response, metric], "expected": expected}


def _response(metric, media="text/plain", **fields):
    return {"status": 200, "content_type": media,
            "body": f"# HELP {metric} Total jobs\n# TYPE {metric} counter\n{metric} 7\n", **fields}


def _cases(metric, suffix):
    baseline = _response(metric)
    body = baseline["body"]
    return [
        _fixture("bare-type-" + suffix, baseline, metric, True),
        _fixture("mixed-header-" + suffix, _response(metric, " TEXT/PLAIN ; version=0.0.4"), metric, True),
        _fixture("crlf-" + suffix, {**baseline, "body": body.replace("\n", "\r\n")}, metric, True),
        _fixture("body-order-" + suffix, {**baseline, "body": "\n".join(reversed(body.splitlines()))}, metric, True),
        _fixture("other-media-" + suffix, _response(metric, "text/html; text/plain"), metric, False),
        _fixture("leading-space-" + suffix, {**baseline, "body": " " + body}, metric, False),
        _fixture("leading-tab-" + suffix, {**baseline, "body": body.replace("# TYPE", "\t# TYPE")}, metric, False),
        _fixture("misnamed-" + suffix, _response(metric + "_extra"), metric, False),
        _fixture("marker-" + suffix, {**baseline, "body": body.replace("# HELP ", "# HELPER ")}, metric, False),
        _fixture("empty-metadata-" + suffix, {**baseline, "body": body.replace("Total jobs", " ")}, metric, False),
        _fixture("missing-body-" + suffix, {"status": 200, "content_type": "text/plain"}, metric, False),
        _fixture("not-text-" + suffix, {**baseline, "body": [body]}, metric, False),
        _fixture("wrong-status-" + suffix, {**baseline, "status": 503}, metric, False),
        _fixture("float-status-" + suffix, {**baseline, "status": 200.0}, metric, False),
        _fixture("bad-metric-" + suffix, _response("3jobs"), "3jobs", False),
        _fixture("bad-record-" + suffix, [baseline], metric, False),
    ]


def fresh_fixtures(run_id, revised_skill=False):
    reuse_metric = f"workshop_completed_{run_id[-10:]}_total"
    ready = _response(reuse_metric)
    if revised_skill:
        captures = [
            _fixture("north-cache", _response(reuse_metric, " TEXT/PLAIN ; version=0.0.4"), reuse_metric, True),
            _fixture("blue-probe", _response(reuse_metric + "_extra"), reuse_metric, False),
            _fixture("west-mirror", {**ready, "body": ready["body"].replace("\n", "\r\n")}, reuse_metric, True),
            _fixture("amber-probe", {**ready, "status": 503}, reuse_metric, False),
        ]
    else:
        captures = [
            _fixture("capture-A", ready, reuse_metric, True),
            _fixture("capture-B", _response(reuse_metric + "_duplicate"), reuse_metric, False),
            _fixture("capture-C", {**ready, "body": ready["body"].replace("\n", "\r\n")}, reuse_metric, True),
            _fixture("capture-D", {**ready, "body": " " + ready["body"]}, reuse_metric, False),
        ]
    return {"fresh": captures, "fresh_metric": reuse_metric}


def metrics_plan(run_id, baseline_path=BASELINE, root=ROOT):
    """Freeze development, qualification, and unseen reuse cases before inference."""
    rows = [json.loads(line) for line in Path(baseline_path).read_text(encoding="utf-8").splitlines()]
    prior = next(row for row in rows if row["side"] == "candidate" and row["sha256"] == BASELINE_SHA256)
    if hashlib.sha256(prior["source"].encode()).hexdigest() != BASELINE_SHA256:
        raise ValueError("Archived baseline source changed")
    previous_cases = [{"id": f"recorded-regression-{failure['test']}",
                       "args": failure["args"], "expected": failure["expected"]}
                      for failure in prior["failures"]]
    if [case["id"] for case in previous_cases] != [
            "recorded-regression-1", "recorded-regression-2", "recorded-regression-19"]:
        raise ValueError("Archived regression fixture set changed")
    # The original behavioral regressions and new contract checks are separate.
    development = [
        {**case, "args": [case["args"][0], "scrapes_total"]} for case in previous_cases
    ] + _cases("queue_depth", "dev")
    holdout = [_cases(f"lab_{run_id[:10]}_{i}_total", f"gate-{i}") for i in range(1, 4)]
    return {"sources": documents(root), "baseline": {**prior, "cases": previous_cases,
             "archive": str(baseline_path)}, "development": development, "holdout": holdout,
            **fresh_fixtures(run_id)}

