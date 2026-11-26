"""Build a local disclosure draft from a verified BugTrace AuthProbe result."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from tools.engagement_scope import EngagementGate
from tools.findings_queue import render_finding_report


def _parse_endpoint(value):
    if not isinstance(value, str) or not value or any(c.isspace() or c in "\x60<>\\" for c in value):
        raise ValueError("Expected an HTTP endpoint without whitespace or Markdown delimiters")
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username is not None:
        raise ValueError("Expected an HTTP endpoint without embedded credentials")
    if url.query or url.fragment:
        raise ValueError("Use an endpoint without query strings or fragments in this draft")
    return url


def _origin(url):
    return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)


def _local_only(host):
    hostname = host.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified or (
        isinstance(address, ipaddress.IPv6Address)
        and address.ipv4_mapped is not None
        and address.ipv4_mapped.is_loopback
    )


def _text_field(review, name):
    value = review.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing review field {name}")
    return value.strip()


def _channel(value):
    if any(c.isspace() or c in "\x60<>\\" for c in value):
        raise ValueError("Enter the designated security form or mailto address")
    parts = urlsplit(value)
    if (parts.scheme == "mailto" and re.fullmatch(r"[^@/?#]+@[A-Za-z0-9.-]+", parts.path)
            and not (parts.netloc or parts.query or parts.fragment)):
        return value
    if _parse_endpoint(value).scheme != "https":
        raise ValueError("A disclosure form must use HTTPS")
    return value


def _verified_auth_read(scan_bytes, review):
    if review.get("report_sha256") != hashlib.sha256(scan_bytes).hexdigest():
        raise ValueError("Review does not match the scan report SHA-256")
    result = json.loads(scan_bytes)
    status = result["status"]
    scan_id, finding_id = review.get("scan_id"), review.get("finding_id")
    if (status.get("status") != "completed" or status.get("scan_id") != scan_id
            or not isinstance(scan_id, str) or re.fullmatch(r"[a-f0-9-]{8,36}", scan_id) is None):
        raise ValueError("Review does not identify the completed BugTrace scan")
    if not isinstance(finding_id, str) or re.fullmatch(r"AP-\d+", finding_id) is None:
        raise ValueError("Review must identify an AuthProbe finding")
    if not isinstance(result.get("findings"), list):
        raise ValueError("BugTrace result is missing its findings list")
    matches = [finding for finding in result["findings"] if finding.get("id") == finding_id]
    if len(matches) != 1:
        raise ValueError("Review must identify exactly one finding")
    finding = matches[0]
    if (finding.get("classification"), finding.get("validation_status")) != ("confirmed", "confirmed"):
        raise ValueError("Only confirmed and validated findings can become disclosure drafts")
    if "auth_probe" not in finding.get("source_tools", []):
        raise ValueError("This evidence format supports BugTrace AuthProbe findings")

    method, _, endpoint = finding["endpoint"].partition(" ")
    url = _parse_endpoint(endpoint)
    if (method != "GET" or review.get("method") != method or review.get("url") != endpoint
            or _origin(url) != _origin(_parse_endpoint(status["target"]))):
        raise ValueError("Review must match the exact GET and scan target")

    evidence = finding["evidence"]
    digest = evidence.get("unauth_response_sha256")
    if (evidence.get("auth_required_by_spec") is not True or evidence.get("auth_compared") is not True
            or evidence.get("http_method") != method or str(evidence.get("response_code")) != "200"
            or evidence.get("auth_status") != 200
            or not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            or evidence.get("auth_response_sha256") != digest):
        raise ValueError("AuthProbe evidence does not show the same authenticated and unauthenticated HTTP 200 body")

    replay = (finding.get("ai_enrichment") or {}).get("validation") or {}
    replay_id = replay.get("evidence_id")
    if (replay.get("status") != "replayed" or replay.get("method") != method or replay.get("url") != endpoint
            or replay.get("unmodified_request") is not True or replay.get("not_found")
            or replay.get("status_code") != 200 or replay.get("response_sha256") != digest
            or not isinstance(replay_id, str) or re.fullmatch(r"[a-f0-9]{20}", replay_id) is None
            or replay_id not in finding.get("validation_evidence_refs", [])):
        raise ValueError("The credential-free replay does not match the AuthProbe evidence")

    observations = review.get("observations", {})
    for identity in ("without_credentials", "with_test_credentials"):
        observation = observations.get(identity, {})
        if observation.get("status") != 200 or observation.get("body_sha256") != digest:
            raise ValueError(f"Independent observation does not match for {identity}")
    proof = {
        "scan_id": scan_id, "finding_id": finding_id,
        "scan_report_sha256": review["report_sha256"],
        "independent_http_without_credentials": 200, "independent_http_with_test_credentials": 200,
        "matching_response_body_sha256": digest, "credential_free_replay_http": 200,
        "replay_evidence_id": replay_id,
    }
    return endpoint, url, proof


def build_draft(scan_bytes, review, scope=None):
    """Create a draft using an operator-supplied verification bound to the report."""
    endpoint, url, proof = _verified_auth_read(scan_bytes, review)
    verified_by = _text_field(review, "verified_by")
    kind = review.get("kind")
    if kind == "lab":
        program = "Synthetic lab fixture"
        title = "Lab fixture · Missing authentication on a protected API read"
        routing = "Internal lab evidence."
        impact = "The seeded fixture returned the same synthetic record without a bearer token."
        severity = "Lab result. Customer severity requires impact on a verified company target."
    elif kind == "manual":
        gate = EngagementGate(scope)
        if _local_only(url.hostname) or gate.platform in {"lab", "local", "local_lab"}:
            raise ValueError("Lab observations can only generate an internal draft")
        if not gate.in_scope or not gate.platform or gate.program == "unknown":
            raise ValueError("Provide the reviewed engagement scope before drafting a company report")
        if not gate.check_url(endpoint)[0]:
            raise ValueError("Finding is outside the reviewed engagement scope")
        program = gate.program
        title = "Draft · Missing authentication on a protected API read"
        routing = f"Designated reporting channel: {_channel(_text_field(review, 'channel'))}"
        impact = _text_field(review, "demonstrated_impact")
        severity = "Pending reviewer selection under the program's severity taxonomy."
    else:
        raise ValueError("Verification kind must be lab or manual")

    proof["verified_by"] = verified_by
    proof["verification_kind"] = kind
    reproduction = "\n".join((
        f"1. Send GET to {url.geturl()} with the authorized test credential and record the response.",
        "2. Repeat the same GET without authorization headers or session cookies.",
        "3. Compare the responses. Both returned HTTP 200 with identical body hashes.",
    ))
    row = {
        "program": program, "method": "GET", "url": endpoint,
        "vuln_type": "missing authentication", "classifier_verdict": "CONFIRMED",
        "report_details": {
            "title": title, "impact": impact, "reproduction": reproduction, "severity": severity,
            "remediation": "Require authentication before returning the protected resource.",
            "verified_evidence": proof,
        },
    }
    return render_finding_report(row) + "\n## Disclosure review\n\n" + routing + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=Path, required=True)
    parser.add_argument("--verification", type=Path, required=True)
    parser.add_argument("--scope", type=Path, help="Reviewed engagement scope for a company draft")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        scope = json.loads(args.scope.read_text(encoding="utf-8-sig")) if args.scope else None
        review = json.loads(args.verification.read_text(encoding="utf-8-sig"))
        draft = build_draft(args.scan.read_bytes(), review, scope)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8", newline="\n") as output:
            output.write(draft)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        parser.exit(2, f"Draft not created: {exc}\n")
    print(args.out.resolve())


if __name__ == "__main__":
    main()
