"""
tools/cve_feed.py
==================
Fetches recently-published CVEs from NVD's public API for safe public research
and compares them only with supplied evidence. It does not test a target,
attempt a patch bypass, or turn an unverified CVE lead into live traffic.

Deliberately does NOT touch any target itself -- fetch_recent_cves() only
calls NVD's public API. Matching against a specific target's fingerprint
happens via match_cves_to_fingerprint(), which takes fingerprint text you
already collected (e.g. via the existing scan_target_surface tool, or
api_request) as a plain argument -- it doesn't make its own request to
anything, so it carries no scope-gate concerns of its own. Use results to
explain an already-published fix or compare versions in an authorized local
repository or intentionally vulnerable training app.
"""

import json
import re
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

NVD_API_BASE = "https://services.nvd.nist.gov/rest/json/cves/2.0"
SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
_CVE_ID = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)


def public_cve_review(cve: Dict[str, Any], published_fix_summary: str = "") -> Dict[str, Any]:
    """Create a target-free review record for an already-public advisory.

    The result intentionally contains no request recipe, exploit steps, or target
    action. It can accompany an educational explanation or an authorized source
    review, but cannot authorize validation against a deployed service.
    """
    cve_id = str(cve.get("id", "")).upper().strip()
    if not _CVE_ID.fullmatch(cve_id):
        raise ValueError("public CVE review requires a canonical CVE identifier")
    description = str(cve.get("description", "")).strip()
    if not description:
        raise ValueError("public CVE review requires advisory description text")
    return {
        "cve_id": cve_id,
        "classification": "public_advisory_review",
        "published_fix_summary": published_fix_summary.strip() or "No published-fix summary supplied.",
        "safe_next_steps": [
            "Explain the vendor's published remediation and affected versions.",
            "Compare versions or source code only in an authorized local repository or training app.",
            "Add a regression test in an isolated authorized workspace before proposing a patch.",
        ],
        "prohibited_next_steps": [
            "Do not send a bypass or proof-of-concept attempt to a live target from this review.",
            "Do not infer that a target is vulnerable from product fingerprinting alone.",
        ],
        "live_target_authorization": False,
    }


def fetch_recent_cves(days: int = 7, min_severity: str = "HIGH", keyword: Optional[str] = None, max_results: int = 50) -> List[Dict[str, Any]]:
    """
    Fetch CVEs published in the last `days` days, at or above `min_severity`.
    `keyword` (optional) is passed to NVD's own keywordSearch param (e.g. a
    product name) to narrow results server-side instead of fetching everything.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    params = {
        "pubStartDate": start.strftime("%Y-%m-%dT%H:%M:%S.000"),
        "pubEndDate": end.strftime("%Y-%m-%dT%H:%M:%S.000"),
        "resultsPerPage": str(min(max_results, 2000)),
    }
    if keyword:
        params["keywordSearch"] = keyword

    url = f"{NVD_API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Sabrina-CVE-Feed/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return [{"error": f"NVD API request failed: {e}"}]

    min_rank = SEVERITY_ORDER.get(min_severity.upper(), 2)
    results = []
    for item in data.get("vulnerabilities", []):
        cve = item.get("cve", {})
        cve_id = cve.get("id", "unknown")
        descriptions = cve.get("descriptions", [])
        desc = next((d["value"] for d in descriptions if d.get("lang") == "en"), "")

        # CVSS metrics come in versioned buckets (v40/v31/v30/v2) -- take whichever's present.
        severity, score = None, None
        metrics = cve.get("metrics", {})
        for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
            if key in metrics and metrics[key]:
                cvss = metrics[key][0].get("cvssData", {})
                severity = cvss.get("baseSeverity") or metrics[key][0].get("baseSeverity")
                score = cvss.get("baseScore")
                break

        if severity and SEVERITY_ORDER.get(severity.upper(), -1) < min_rank:
            continue

        vendors_products = set()
        for affected in cve.get("configurations", []) or []:
            for node in affected.get("nodes", []):
                for cpe_match in node.get("cpeMatch", []):
                    cpe = cpe_match.get("criteria", "")
                    parts = cpe.split(":")
                    if len(parts) > 4:
                        vendors_products.add(f"{parts[3]}:{parts[4]}")  # vendor:product
        # Fallback: also pull vendor/product directly from `affected` blocks if present
        for aff in cve.get("affected", []) or []:
            for ad in aff.get("affectedData", []) or []:
                v, p = ad.get("vendor"), ad.get("product")
                if v and p:
                    vendors_products.add(f"{v}:{p}")

        results.append({
            "id": cve_id,
            "published": cve.get("published"),
            "severity": severity,
            "cvss_score": score,
            "description": desc[:400],
            "vendors_products": sorted(vendors_products),
        })

    results.sort(key=lambda r: (SEVERITY_ORDER.get((r.get("severity") or "").upper(), -1), r.get("cvss_score") or 0), reverse=True)
    return results


def match_cves_to_fingerprint(cves: List[Dict[str, Any]], fingerprint_text: str) -> List[Dict[str, Any]]:
    """
    Given CVEs from fetch_recent_cves() and free-text fingerprint info you
    already collected about a target (server headers, X-Powered-By, detected
    framework/CMS names, version strings), return CVEs whose vendor/product
    name or description text plausibly matches something in the fingerprint.

    This is a coarse, high-recall keyword match meant to produce candidates
    for a human/agent to actually verify -- not a confirmed finding. Matching
    a product name is not the same as confirming the exact vulnerable version
    is running or that the vulnerable code path is reachable.
    """
    fp_lower = fingerprint_text.lower()
    fp_tokens = set(re.findall(r"[a-z0-9][a-z0-9._-]{2,}", fp_lower))

    matches = []
    for cve in cves:
        if "error" in cve:
            continue
        hit_terms = set()
        for vp in cve.get("vendors_products", []):
            vendor, _, product = vp.partition(":")
            for term in (vendor, product):
                term_norm = term.replace("_", "-").lower()
                if len(term_norm) >= 3 and (term_norm in fp_lower or term_norm.replace("-", "") in fp_lower.replace("-", "")):
                    hit_terms.add(term)
        # Also check description text for any fingerprint token appearing verbatim
        desc_lower = (cve.get("description") or "").lower()
        for token in fp_tokens:
            if len(token) >= 4 and token in desc_lower:
                hit_terms.add(token)

        if hit_terms:
            match = dict(cve)
            match["matched_terms"] = sorted(hit_terms)
            matches.append(match)

    return matches


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    cves = fetch_recent_cves(days=days, min_severity="HIGH")
    print(json.dumps(cves[:10], indent=2))
