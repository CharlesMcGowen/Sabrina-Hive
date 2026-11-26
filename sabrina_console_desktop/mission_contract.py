"""Validate the bounded JSON contract accepted by a read-only live mission."""

from __future__ import annotations

import json
import math
import re
from typing import Any


MAX_REVISIONS = 4
MAX_RESPONSE_CHARS = 4_000
_TYPES = {"string", "integer", "number", "boolean", "null"}
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,39}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _finite_number(number: str) -> float:
    value = float(number)
    if not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    return value


def _load_object(raw: str, *, limit: int, label: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw) > limit:
        raise ValueError(f"{label} must be a JSON object of at most {limit} characters.")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_float=_finite_number, parse_constant=_finite_number)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"{label} must be valid JSON with unique property names and finite numbers.") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return value


def _clean_text(value: Any, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must be text of 1–{limit} characters.")
    if re.search(r"[\x00\ud800-\udfff]", value):
        raise ValueError(f"{label} contains invalid text.")
    return value.strip()


def _matches_type(value: Any, kind: str) -> bool:
    if kind == "integer":
        return type(value) is int or (type(value) is float and value.is_integer())
    return type(value) in {"number": (int, float), "string": (str,), "boolean": (bool,), "null": (type(None),)}[kind]


def parse_contract(raw: str) -> dict[str, Any]:
    spec = _load_object(raw, limit=4_000, label="Mission contract")
    if spec.keys() != {"objective", "constraints", "response_schema"}:
        raise ValueError("Provide objective, constraints, and response_schema.")
    spec["objective"] = _clean_text(spec["objective"], 600, "Objective")
    constraints = spec["constraints"]
    if not isinstance(constraints, list) or len(constraints) > 6:
        raise ValueError("Provide up to six text constraints in a JSON array.")
    spec["constraints"] = [_clean_text(value, 240, "Constraint") for value in constraints]

    schema = spec["response_schema"]
    if (not isinstance(schema, dict) or schema.keys() != {"type", "properties", "required", "additionalProperties"}
        or schema["type"] != "object" or schema["additionalProperties"] is not False):
        raise ValueError("Use an object response schema with properties, required, and additionalProperties set to false.")
    properties = schema["properties"]
    if not isinstance(properties, dict) or not 1 <= len(properties) <= 12:
        raise ValueError("The response schema must define 1–12 properties.")
    for name, rule in properties.items():
        if not _FIELD_NAME.fullmatch(name):
            raise ValueError("Response field names must be identifiers of up to 40 characters.")
        if not isinstance(rule, dict) or rule.keys() - {"type", "enum"}:
            raise ValueError(f"Field {name} supports scalar type and optional enum.")
        kind = rule.get("type")
        if not isinstance(kind, str) or kind not in _TYPES:
            raise ValueError(f"Field {name} requires a scalar JSON type.")
        if "enum" in rule:
            values = rule["enum"]
            if not isinstance(values, list) or not 1 <= len(values) <= 12 or any(
                not _matches_type(value, kind) for value in values
            ):
                raise ValueError(f"Field {name} requires 1–12 enum values of its declared type.")
    required = schema["required"]
    if (not isinstance(required, list) or not required or any(
        not isinstance(name, str) or name not in properties for name in required
    ) or len(set(required)) != len(required)):
        raise ValueError("List at least one distinct required field from properties.")
    return spec


def parse_response(schema: dict[str, Any], raw: str) -> dict[str, Any]:
    if len(raw) > MAX_RESPONSE_CHARS + 32:
        raise ValueError(f"Deliverable must be a JSON object of at most {MAX_RESPONSE_CHARS} characters.")
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", raw.strip(), flags=re.I | re.S)
    result = _load_object(fenced[1] if fenced else raw, limit=MAX_RESPONSE_CHARS, label="Deliverable")
    missing = sorted(set(schema["required"]) - result.keys())
    if missing:
        raise ValueError(f"Required deliverable fields are missing ({', '.join(missing)}).")
    if result.keys() - schema["properties"].keys():
        raise ValueError("Deliverable contains unexpected fields.")
    for name, value in result.items():
        rule = schema["properties"][name]
        if not _matches_type(value, rule["type"]):
            raise ValueError(f"Deliverable field {name} must be {rule['type']}.")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"Deliverable field {name} must match an allowed value.")
    return result


def require_critic_pass(raw: str) -> None:
    if not raw.lstrip().upper().startswith("PASS:"):
        raise ValueError("Critic did not explicitly pass the current mission contract.")
