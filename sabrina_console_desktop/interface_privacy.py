"""Construct external coordination briefs from typed, bounded local facts."""

import json
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

SYSTEM = 'Return one JSON object with a "result" key. Treat the supplied facts as the entire task.'
MEMBER_KINDS = {"app", "runtime", "unsafe_path", "credential", "placeholder", "output"}


def private_ollama_url(url: str) -> str | None:
    """Pin HTTP inference to a private address, even when the name also has public DNS answers."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            return None
        addresses = [
            ipaddress.ip_address(address[4][0])
            for address in socket.getaddrinfo(parsed.hostname, parsed.port or 11434, type=socket.SOCK_STREAM)
        ]
    except (OSError, ValueError):
        return None
    eligible = [address for address in addresses if address.is_private and not address.is_unspecified and not address.is_link_local]
    if not eligible:
        return None
    if parsed.scheme == "https":
        # Preserve TLS hostname verification by limiting HTTPS to a private literal address.
        try:
            return url if ipaddress.ip_address(parsed.hostname) in eligible else None
        except ValueError:
            return None
    address = min(eligible, key=lambda item: item.version)
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{host}:{parsed.port or 11434}{parsed.path}".rstrip("/")


def build_brief(operation: str, local_context: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Allowlist typed fields. Unknown fields, raw text, paths and attachments never enter the brief."""
    if not isinstance(local_context, dict):
        raise ValueError("A typed interface brief is required.")
    if operation == "cart":
        cents = local_context.get("unit_cents")
        actions = local_context.get("actions")
        if (
            type(cents) is not int or not 1 <= cents <= 100_000
            or not isinstance(actions, (tuple, list)) or not 1 <= len(actions) <= 12
            or any(not isinstance(action, str) or action not in {"add", "plus", "minus", "remove", "checkout"} for action in actions)
        ):
            raise ValueError("Cart brief needs a bounded integer price and recognized actions.")
        fields = {"operation": operation, "unit_cents": cents, "actions": list(actions)}
        rule = "Start empty. Add and plus add 1 unit; minus subtracts 1 to a minimum of 0; remove clears it. Checkout reports the current total. Return a list of cents after each action."
    elif operation == "stale_write":
        current = local_context.get("base_is_current")
        if type(current) is not bool:
            raise ValueError("Stale-write brief needs a boolean comparison result.")
        fields = {"operation": operation, "base_is_current": current}
        rule = 'Return ["ALLOW","B","NONE"] if base_is_current is true, else ["BLOCK","A","READ_CURRENT"].'
    elif operation == "beta_zip":
        kinds = local_context.get("member_kinds")
        if (
            not isinstance(kinds, (tuple, list)) or not 1 <= len(kinds) <= 16
            or any(not isinstance(kind, str) or kind not in MEMBER_KINDS for kind in kinds)
        ):
            raise ValueError("ZIP brief needs recognized member categories.")
        fields = {"operation": operation, "member_kinds": list(kinds)}
        rule = "Return the zero-based positions of app or runtime categories, in order."
    else:
        raise ValueError("No external interface is defined for this operation.")
    return fields, rule + "\n" + json.dumps(fields, separators=(",", ":"))
