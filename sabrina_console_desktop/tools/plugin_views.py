"""Schema v1 for declarative plugin views.

A plugin's execute() may return {"ok": true, "view": {"version": 1, "blocks": [...]}}.
validate_view() checks the whole document before anything is rendered. It never
returns HTML or script: every block is data that the console draws with fixed
components. Limits keep a single view small enough to render and to audit.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import re
from typing import Any

VIEW_VERSION = 1
MAX_BLOCKS = 40
MAX_VIEW_JSON_BYTES = 2_000_000
MAX_TEXT = 4000
MAX_LABEL = 200
MAX_TABLE_ROWS = 500
MAX_TABLE_COLUMNS = 20
MAX_CHART_POINTS = 200
MAX_CHART_SERIES = 6
MAX_IMAGE_BYTES = 2_000_000
MAX_ANIMATION_BYTES = 8_000_000
MAX_ANIMATION_FRAMES = 60
MAX_URL_CHARS = 2048
MAX_GRAPH_NODES = 100
MAX_GRAPH_EDGES = 300
MAX_FORM_FIELDS = 20
MAX_SELECT_OPTIONS = 50

BLOCK_TYPES = frozenset({"text", "table", "chart", "image", "animation", "graph", "form"})
CHART_KINDS = frozenset({"line", "bar", "pie"})
NODE_KINDS = frozenset({"model", "agent", "tool", "data", "decision"})
FIELD_KINDS = frozenset({"text", "number", "select", "checkbox"})
IDENT = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
NODE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
DATA_URI = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/]+={0,2})$")
MAGIC = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),
    "gif": (b"GIF87a", b"GIF89a"),
}


class ViewError(ValueError):
    """Raised when a plugin view does not match schema v1."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ViewError(message)


def _text(value: Any, limit: int, what: str) -> str:
    _require(isinstance(value, str) and len(value) <= limit, f"{what} must be a string up to {limit} characters")
    return value


def _number(value: Any, what: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
             f"{what} must be a finite number")
    return float(value)


def _scalar(value: Any, what: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        _require(len(value) <= 500, f"{what} text is too long")
        return value
    return _number(value, what)


def _check_image(src: Any, limit: int, what: str) -> str:
    _require(isinstance(src, str) and len(src) <= limit * 2, f"{what} source is missing or too large")
    if src.startswith("https://"):
        _require(len(src) <= MAX_URL_CHARS and re.fullmatch(r"https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", src),
                 f"{what} URL is not a plain https address")
        return src
    match = DATA_URI.fullmatch(src)
    _require(match is not None, f"{what} must be a png, jpeg, webp or gif data URI, or an https URL")
    kind, payload = match.group(1), match.group(2)
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ViewError(f"{what} data URI is not valid base64") from exc
    _require(len(raw) <= limit, f"{what} is larger than {limit} bytes")
    _require(any(raw.startswith(magic) for magic in MAGIC[kind]), f"{what} bytes do not match its declared type")
    return src


def _block_text(block: dict[str, Any]) -> dict[str, Any]:
    return {"type": "text", "text": _text(block.get("text"), MAX_TEXT, "text block")}


def _block_table(block: dict[str, Any]) -> dict[str, Any]:
    columns = block.get("columns")
    _require(isinstance(columns, list) and 1 <= len(columns) <= MAX_TABLE_COLUMNS, "table needs 1-20 columns")
    columns = [_text(c, 80, "table column") for c in columns]
    rows = block.get("rows", [])
    _require(isinstance(rows, list) and len(rows) <= MAX_TABLE_ROWS, f"table allows up to {MAX_TABLE_ROWS} rows")
    clean_rows = []
    for row in rows:
        _require(isinstance(row, list) and len(row) == len(columns), "every table row must match the column count")
        clean_rows.append([_scalar(v, "table cell") for v in row])
    return {"type": "table", "columns": columns, "rows": clean_rows}


def _block_chart(block: dict[str, Any]) -> dict[str, Any]:
    kind = block.get("kind")
    _require(kind in CHART_KINDS, "chart kind must be line, bar or pie")
    labels = block.get("labels")
    _require(isinstance(labels, list) and 1 <= len(labels) <= MAX_CHART_POINTS, "chart needs 1-200 labels")
    labels = [_text(label, 80, "chart label") for label in labels]
    series = block.get("series")
    _require(isinstance(series, list) and 1 <= len(series) <= MAX_CHART_SERIES, "chart needs 1-6 series")
    if kind == "pie":
        _require(len(series) == 1, "pie charts take exactly one series")
    clean_series = []
    for s in series:
        _require(isinstance(s, dict) and set(s) <= {"name", "values"}, "series needs name and values")
        values = s.get("values")
        _require(isinstance(values, list) and len(values) == len(labels), "each series needs one value per label")
        clean_series.append({
            "name": _text(s.get("name"), 80, "series name"),
            "values": [_number(v, "chart value") for v in values],
        })
    return {"type": "chart", "kind": kind, "title": _text(block.get("title", ""), 120, "chart title"),
            "labels": labels, "series": clean_series}


def _block_image(block: dict[str, Any]) -> dict[str, Any]:
    return {"type": "image", "src": _check_image(block.get("src"), MAX_IMAGE_BYTES, "image"),
            "alt": _text(block.get("alt", ""), MAX_LABEL, "image alt")}


def _block_animation(block: dict[str, Any]) -> dict[str, Any]:
    frames = block.get("frames")
    _require(isinstance(frames, list) and 2 <= len(frames) <= MAX_ANIMATION_FRAMES, "animation needs 2-60 frames")
    fps = block.get("fps", 2)
    _require(type(fps) is int and 1 <= fps <= 10, "animation fps must be an integer from 1 to 10")
    total = 0
    clean = []
    for frame in frames:
        src = _check_image(frame, MAX_IMAGE_BYTES, "animation frame")
        total += len(src)
        clean.append(src)
    _require(total <= MAX_ANIMATION_BYTES * 2, "animation is too large")
    return {"type": "animation", "frames": clean, "fps": fps,
            "alt": _text(block.get("alt", ""), MAX_LABEL, "animation alt")}


def _block_graph(block: dict[str, Any]) -> dict[str, Any]:
    nodes = block.get("nodes")
    _require(isinstance(nodes, list) and 1 <= len(nodes) <= MAX_GRAPH_NODES, "graph needs 1-100 nodes")
    seen: set[str] = set()
    clean_nodes = []
    for node in nodes:
        _require(isinstance(node, dict), "graph node must be an object")
        node_id = node.get("id")
        _require(isinstance(node_id, str) and NODE_ID.fullmatch(node_id) and node_id not in seen,
                 "graph node ids must be unique lowercase identifiers")
        seen.add(node_id)
        kind = node.get("kind", "tool")
        _require(kind in NODE_KINDS, "graph node kind is not recognised")
        clean_nodes.append({"id": node_id, "label": _text(node.get("label", node_id), 80, "node label"), "kind": kind})
    edges = block.get("edges", [])
    _require(isinstance(edges, list) and len(edges) <= MAX_GRAPH_EDGES, "graph allows up to 300 edges")
    clean_edges = []
    for edge in edges:
        _require(isinstance(edge, dict), "graph edge must be an object")
        src, dst = edge.get("from"), edge.get("to")
        _require(src in seen and dst in seen, "graph edge points at an unknown node")
        clean_edges.append({"from": src, "to": dst, "label": _text(edge.get("label", ""), 80, "edge label")})
    return {"type": "graph", "nodes": clean_nodes, "edges": clean_edges}


def _block_form(block: dict[str, Any]) -> tuple[dict[str, Any], str]:
    form_id = block.get("id")
    _require(isinstance(form_id, str) and IDENT.fullmatch(form_id), "form id must be a lowercase identifier")
    submit = block.get("submit")
    _require(isinstance(submit, dict) and set(submit) == {"label", "operation"}, "form submit needs label and operation")
    operation = submit["operation"]
    _require(isinstance(operation, str) and IDENT.fullmatch(operation), "form operation must be a lowercase identifier")
    fields = block.get("fields")
    _require(isinstance(fields, list) and 1 <= len(fields) <= MAX_FORM_FIELDS, "form needs 1-20 fields")
    names: set[str] = set()
    clean_fields = []
    for field in fields:
        _require(isinstance(field, dict), "form field must be an object")
        name, kind = field.get("name"), field.get("kind")
        _require(isinstance(name, str) and IDENT.fullmatch(name) and name not in names, "field names must be unique identifiers")
        names.add(name)
        _require(kind in FIELD_KINDS, "field kind must be text, number, select or checkbox")
        clean = {"name": name, "label": _text(field.get("label", name), 80, "field label"), "kind": kind,
                 "required": bool(field.get("required", False))}
        if kind == "number":
            for bound in ("min", "max", "default"):
                if bound in field:
                    clean[bound] = _number(field[bound], f"field {bound}")
            _require(clean.get("min", -math.inf) <= clean.get("max", math.inf), "number min must not exceed max")
        elif kind == "select":
            options = field.get("options")
            _require(isinstance(options, list) and 1 <= len(options) <= MAX_SELECT_OPTIONS, "select needs 1-50 options")
            clean["options"] = [_text(o, 80, "select option") for o in options]
            if "default" in field:
                _require(field["default"] in clean["options"], "select default must be one of its options")
                clean["default"] = field["default"]
        elif kind == "text":
            clean["max_length"] = int(field.get("max_length", 200))
            _require(1 <= clean["max_length"] <= 2000, "text max_length must be 1-2000")
            if "default" in field:
                clean["default"] = _text(field["default"], clean["max_length"], "text default")
        elif kind == "checkbox":
            clean["default"] = bool(field.get("default", False))
        clean_fields.append(clean)
    block_out = {"type": "form", "id": form_id, "submit": {"label": _text(submit["label"], 40, "submit label"),
                                                           "operation": operation}, "fields": clean_fields}
    return block_out, operation


_BUILDERS = {
    "text": _block_text,
    "table": _block_table,
    "chart": _block_chart,
    "image": _block_image,
    "animation": _block_animation,
    "graph": _block_graph,
}


def validate_view(view: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate a schema v1 view. Returns (normalized_view, form_operations).

    Raises ViewError with a short message on the first problem found.
    """
    _require(isinstance(view, dict) and view.get("version") == VIEW_VERSION, "view must declare version 1")
    _require(set(view) <= {"version", "blocks"}, "view has unknown keys")
    blocks = view.get("blocks")
    _require(isinstance(blocks, list) and 1 <= len(blocks) <= MAX_BLOCKS, "view needs 1-40 blocks")
    try:
        encoded = json.dumps(view, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ViewError("view is not JSON-compatible") from exc
    _require(len(encoded) <= MAX_VIEW_JSON_BYTES, "view is larger than 2 MB")
    out_blocks: list[dict[str, Any]] = []
    operations: list[str] = []
    for block in blocks:
        _require(isinstance(block, dict), "each block must be an object")
        kind = block.get("type")
        _require(kind in BLOCK_TYPES, f"unknown block type: {kind!r}")
        if kind == "form":
            clean, operation = _block_form(block)
            out_blocks.append(clean)
            if operation not in operations:
                operations.append(operation)
        else:
            out_blocks.append(_BUILDERS[kind](block))
    return {"version": VIEW_VERSION, "blocks": out_blocks}, operations


def validate_form_inputs(form: dict[str, Any], inputs: Any) -> dict[str, Any]:
    """Check submitted values against one validated form block. Returns the cleaned inputs."""
    _require(isinstance(inputs, dict), "form inputs must be an object")
    fields = {f["name"]: f for f in form["fields"]}
    _require(set(inputs) <= set(fields), "form received an unknown input")
    clean: dict[str, Any] = {}
    for name, field in fields.items():
        if name not in inputs or inputs[name] is None:
            _require(not field["required"], f"input {name} is required")
            continue
        value = inputs[name]
        kind = field["kind"]
        if kind == "text":
            _require(isinstance(value, str) and len(value) <= field["max_length"], f"input {name} is not valid text")
        elif kind == "number":
            value = _number(value, f"input {name}")
            _require(field.get("min", -math.inf) <= value <= field.get("max", math.inf), f"input {name} is out of range")
        elif kind == "select":
            _require(value in field["options"], f"input {name} is not one of its options")
        elif kind == "checkbox":
            _require(isinstance(value, bool), f"input {name} must be true or false")
        clean[name] = value
    return clean
