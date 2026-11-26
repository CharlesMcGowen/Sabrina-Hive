"""Canonical, short-lived messages for the coordinator-to-Ego order channel."""

import hashlib
import hmac
import json
import math
import re
import time
from decimal import Decimal, InvalidOperation

MAX_LIFETIME = 60
MAX_CLOCK_SKEW = 5
ORDER_FIELDS = frozenset({"pair", "side", "purpose", "volume", "ordertype", "oflags", "price"})
_ID = re.compile(r"[0-9a-f]{32}")
_RELEASE = re.compile(r"[0-9a-f]{64}")
_LABEL = re.compile(r"[A-Za-z0-9_.:@-]{1,128}")
_PAIR = re.compile(r"[A-Z0-9]{3,24}")
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]{1,2})?")


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value):
    if not isinstance(value, str) or len(value) > 48 or not _NUMBER.fullmatch(value):
        raise ValueError("Order quantities and prices must be positive decimal strings")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid decimal") from None
    if number <= 0 or number.adjusted() not in range(-18, 18) or number.as_tuple().exponent < -18:
        raise ValueError("Order decimal is outside supported precision or range")
    # No Decimal.normalize(): it can round with the process's current context.
    fixed = format(number, "f")
    return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed


def canonical_order(action, *, runtime_intent=False):
    if not isinstance(action, dict) or (not runtime_intent and set(action) - ORDER_FIELDS):
        raise ValueError("Invalid order fields")
    if action.get("side") != "buy" or action.get("purpose") != "entry":
        raise ValueError("Expected a spot entry buy")
    pair = action.get("pair")
    if not isinstance(pair, str) or not _PAIR.fullmatch(pair):
        raise ValueError("Invalid spot pair")
    volume = _number(action.get("volume"))
    ordertype = action.get("ordertype", "market")
    flags = action.get("oflags", "fciq")
    price = action.get("price")
    if ordertype == "market" and flags == "fciq" and price is None:
        pass
    elif ordertype == "limit" and flags in ("fciq", "post,fciq"):
        price = _number(price)
    else:
        raise ValueError("Invalid order type or flags")
    return dict(pair=pair, side="buy", purpose="entry", volume=volume,
                ordertype=ordertype, oflags=flags, price=price)


def action_hash(action, *, runtime_intent=False):
    order = canonical_order(action, runtime_intent=runtime_intent)
    return hashlib.sha256(canonical_json(order).encode("ascii")).hexdigest()


def _match(pattern, value):
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def _timestamp(value):
    if type(value) not in (int, float) or not 0 <= value <= 10**11 or not math.isfinite(value):
        raise ValueError("Invalid grant clock")
    return float(value)


def validate_scope(account, release, signing_key):
    if not _match(_LABEL, account) or not _match(_RELEASE, release):
        raise ValueError("Invalid account or release scope")
    if not isinstance(signing_key, bytes) or not 32 <= len(signing_key) <= 128:
        raise ValueError("The order channel needs a separate 32-128 byte signing key")


def _signature(fields, signing_key):
    data = canonical_json(fields).encode("ascii")
    return hmac.new(signing_key, b"sabrina.kraken.order-channel.v1\0" + data, hashlib.sha256).hexdigest()


def _base(kind, request_id, account, release, signing_key, now):
    validate_scope(account, release, signing_key)
    if not _match(_ID, request_id):
        raise ValueError("Invalid grant ID")
    issued_at = _timestamp(time.time() if now is None else now)
    return dict(version=1, kind=kind, id=request_id, account=account, release=release,
                issued_at=issued_at, expires=issued_at + MAX_LIFETIME)


def sign_grant(request_id, action, *, account, release, mission, worker, signing_key, now=None):
    if not _match(_LABEL, mission) or not _match(_LABEL, worker):
        raise ValueError("Invalid mission or worker identity")
    fields = _base("grant", request_id, account, release, signing_key, now)
    fields.update(action=canonical_order(action), mission=mission, worker=worker)
    return {**fields, "signature": _signature(fields, signing_key)}


def sign_revocation(grant_id, *, account, release, signing_key, now=None):
    fields = _base("revoke", grant_id, account, release, signing_key, now)
    return {**fields, "signature": _signature(fields, signing_key)}


def verify_message(envelope, *, kind, account, release, signing_key, now=None):
    validate_scope(account, release, signing_key)
    if not isinstance(envelope, dict) or kind not in ("grant", "revoke"):
        raise ValueError("Invalid order channel message")
    fields = {"version", "kind", "id", "account", "release", "issued_at", "expires", "signature"}
    if kind == "grant":
        fields.update(("mission", "worker", "action"))
    if set(envelope) != fields or type(envelope["version"]) is not int or envelope["version"] != 1:
        raise ValueError("Invalid order channel message fields")
    if envelope["kind"] != kind or envelope["account"] != account or envelope["release"] != release:
        raise ValueError("Order channel scope mismatch")
    if not _match(_ID, envelope["id"]) or not _match(_RELEASE, envelope["signature"]):
        raise ValueError("Malformed grant ID or signature")
    if kind == "grant":
        if not _match(_LABEL, envelope["mission"]) or not _match(_LABEL, envelope["worker"]):
            raise ValueError("Invalid mission or worker identity")
        if envelope["action"] != canonical_order(envelope["action"]):
            raise ValueError("Non-canonical order action")
    issued = _timestamp(envelope["issued_at"])
    expires = _timestamp(envelope["expires"])
    clock = _timestamp(time.time() if now is None else now)
    if not 0 < expires - issued <= MAX_LIFETIME or issued > clock + MAX_CLOCK_SKEW or expires <= clock:
        raise ValueError("Expired or invalid grant lifetime")
    unsigned = {name: value for name, value in envelope.items() if name != "signature"}
    if not hmac.compare_digest(envelope["signature"], _signature(unsigned, signing_key)):
        raise ValueError("Order channel signature mismatch")
    return dict(envelope)
