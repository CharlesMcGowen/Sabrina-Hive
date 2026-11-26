"""
tools/bounty_discovery.py
==========================
Discovery + extraction helpers for bug-bounty platforms.

Sabrina can use these (via delegate_to_engineer or direct api_request) to:
1. Query a bounty platform's API or recon URL to list enrolled programs.
2. Parse the program list into structured format (name, URL, estimated scope).
3. Surface candidates for human scope-file authoring (via ask_human).

This is scaffolding for the "seed engagements, then get human authorization per
program" workflow described in tools/engagement_scope.py.

Example: Bugcrowd API at https://www.bugcrowd.com/api/v1/researcher/engagements
returns a JSON list of enrolled programs with their public-facing info.
"""

import json
import re
from typing import List, Dict, Any, Optional


class BugBountyDiscovery:
    """Base class for bounty platform discovery. Subclass for each platform."""

    def parse_program_list(self, response_body: str) -> List[Dict[str, Any]]:
        """
        Parse a platform's program list response into structured format.
        Returns list of dicts with 'name', 'slug', 'url', 'notes' keys (or platform-specific variants).
        """
        raise NotImplementedError

    def format_for_scope_file(self, program: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert a discovered program dict into a skeleton scope file.
        Still requires human to fill in actual in_scope/out_of_scope from the program's policy page.
        """
        return {
            "platform": self.platform_name,
            "program": program.get("slug") or program.get("name", "unknown").lower().replace(" ", "_"),
            "display_name": program.get("name", ""),
            "program_url": program.get("url", ""),
            "in_scope": [program.get("target_domain") or ""],  # Placeholder; human fills from policy
            "out_of_scope": [],
            "automated_testing_allowed": False,
            "rate_limit_rps": None,
            "focus": [],
            "notes": f"Discovered from {self.platform_name}. Human: fill in real in_scope/out_of_scope from the program's policy page."
        }


class BugcrowdDiscovery(BugBountyDiscovery):
    """Bugcrowd engagement discovery."""
    platform_name = "bugcrowd"

    def parse_program_list(self, response_body: str) -> List[Dict[str, Any]]:
        """
        Parse Bugcrowd's /api/v1/researcher/engagements JSON response.
        Returns list of programs with name, uuid, url, target_domain (if available).
        """
        try:
            data = json.loads(response_body)
            programs = []

            # Bugcrowd's API returns { "engagements": [...] } or similar
            engagements = data.get("engagements", data.get("data", []))
            if not isinstance(engagements, list):
                engagements = []

            for eng in engagements:
                program = {
                    "name": eng.get("name") or eng.get("program_name") or "Unknown",
                    "slug": eng.get("uuid") or eng.get("slug") or eng.get("name", "").lower().replace(" ", "_"),
                    "url": eng.get("program_url") or f"https://bugcrowd.com/engagements/{eng.get('uuid', '')}",
                    "target_domain": eng.get("target") or eng.get("target_domain") or "",
                    "status": eng.get("status", ""),
                    "raw": eng  # Keep full record for debugging
                }
                programs.append(program)

            return programs
        except json.JSONDecodeError:
            return []

    # UI-chrome link text that shares an href with the real program name (Bugcrowd
    # renders a badge link and a title link pointing at the same /engagements/<slug>,
    # e.g. {"text": "Updated", "href": "/engagements/hostgator-latam-bb"} followed by
    # {"text": "HostGator LATAM Bug Bounty", "href": "/engagements/hostgator-latam-bb"}).
    # Verified against a real authenticated session on 2026-09-13.
    _NAV_LABELS = {"updated", "expedited triage", "bug bounty", "vulnerability disclosure", "pen tests", "self-managed"}

    def parse_program_list_from_links(self, links: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """
        Parse the structured {"text", "href"} link list already returned by
        ai_systems.browser_utils.PlaywrightBrowserManager.inspect_page() -- this is
        what Sabrina's browser_inspect tool actually gives her, so this avoids
        regex-scraping raw HTML for something already available as structured data.

        Replaces an earlier version of this method that matched
        href="/engagements/UUID" -- verified against a real Bugcrowd session that
        engagement URLs are lowercase-hyphenated SLUGS (e.g. "hostgator-latam-bb",
        "webdotcom", "launchdarkly-mbb-og"), not UUIDs. That earlier pattern would
        have matched zero real engagements.
        """
        by_slug: Dict[str, List[str]] = {}
        for link in links:
            href = (link.get("href") or "").strip()
            text = (link.get("text") or "").strip()
            m = re.match(r"^/engagements/([a-z0-9][a-z0-9\-]*)$", href, re.IGNORECASE)
            if not m:
                continue
            slug = m.group(1)
            if slug == "invites":  # nav link, not a program
                continue
            by_slug.setdefault(slug, []).append(text)

        programs = []
        for slug, texts in by_slug.items():
            real_names = [t for t in texts if t and t.lower() not in self._NAV_LABELS]
            name = max(real_names, key=len) if real_names else slug
            programs.append({
                "name": name,
                "slug": slug,
                "url": f"https://bugcrowd.com/engagements/{slug}",
                "target_domain": "",
                "status": "discovered_from_dashboard"
            })
        return programs


class HackerOneDiscovery(BugBountyDiscovery):
    """HackerOne program discovery."""
    platform_name = "hackerone"

    def parse_program_list(self, response_body: str) -> List[Dict[str, Any]]:
        """
        Parse HackerOne's program list JSON (typically from /graphql or /api/v1/programs).
        """
        try:
            data = json.loads(response_body)
            programs = []

            # HackerOne's schema varies; try common keys
            program_list = data.get("data", data.get("programs", []))
            if not isinstance(program_list, list):
                program_list = []

            for prog in program_list:
                program = {
                    "name": prog.get("name") or prog.get("title") or "Unknown",
                    "slug": prog.get("handle") or prog.get("id") or prog.get("name", "").lower().replace(" ", "_"),
                    "url": prog.get("url") or f"https://hackerone.com/{prog.get('handle', '')}",
                    "target_domain": prog.get("target") or "",
                    "status": prog.get("state", ""),
                    "raw": prog
                }
                programs.append(program)

            return programs
        except json.JSONDecodeError:
            return []


def discover_bugcrowd_programs(api_response_body: str) -> List[Dict[str, Any]]:
    """
    Convenience function: parse Bugcrowd discovery response into scope-file skeletons.
    """
    disco = BugcrowdDiscovery()
    programs = disco.parse_program_list(api_response_body)
    return [disco.format_for_scope_file(p) for p in programs]


def discover_bugcrowd_programs_from_links(links: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """
    Convenience function: parse the structured links list from browser_inspect
    (dashboard/engagements page) into scope-file skeletons. This is the verified,
    working path -- see BugcrowdDiscovery.parse_program_list_from_links.
    """
    disco = BugcrowdDiscovery()
    programs = disco.parse_program_list_from_links(links)
    return [disco.format_for_scope_file(p) for p in programs]


def discover_hackerone_programs(api_response_body: str) -> List[Dict[str, Any]]:
    """
    Convenience function: parse HackerOne discovery response into scope-file skeletons.
    """
    disco = HackerOneDiscovery()
    programs = disco.parse_program_list(api_response_body)
    return [disco.format_for_scope_file(p) for p in programs]
