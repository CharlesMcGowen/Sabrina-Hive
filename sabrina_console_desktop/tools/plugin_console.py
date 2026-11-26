"""Operator commands for Sabrina's existing dynamic plugin contract.

Plugins live in tools/custom/*_plugin.py or tools/engines/*_engine.py and expose
TOOL_NAME, TOOL_PROMPT and execute(action, loop=None). Discovery inspects source
without importing code. A plugin is imported and invoked only on an explicit
operator command. Each plugin owns its own action validation and permissions.
"""
from __future__ import annotations

import ast
import hashlib
from html import escape as _html_escape
from html.parser import HTMLParser
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (("custom", "*_plugin.py"), ("engines", "*_engine.py"))
NAME = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
OPERATION = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
ACCESS_VALUES = ("read", "write")
TAB_KEYS = {"title", "icon", "operation", "display", "refresh_seconds"}
TAB_DISPLAYS = ("json", "text", "html", "dashboard")

_VOID_HTML_TAGS = frozenset({"br", "hr", "col", "input"})
_DROP_SUBTREE_TAGS = frozenset({
    "script", "iframe", "frame", "frameset", "object", "embed",
    "applet", "meta", "base", "link", "noscript", "template", "form",
})
_SVG_CASE_MAP = {
    "lineargradient": "linearGradient",
    "radialgradient": "radialGradient",
    "clippath": "clipPath",
    "foreignobject": "",  # disallowed inside SVG
}
_SVG_ATTR_CASE_MAP = {
    "viewbox": "viewBox",
    "preserveaspectratio": "preserveAspectRatio",
    "gradientunits": "gradientUnits",
    "gradienttransform": "gradientTransform",
    "patternunits": "patternUnits",
    "clippathunits": "clipPathUnits",
}
_ALLOWED_TAGS = frozenset({
    "a", "div", "span", "section", "article", "header", "footer", "nav", "aside", "main",
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "code", "blockquote",
    "strong", "b", "em", "i", "small", "sub", "sup", "mark", "del", "ins",
    "hr", "br", "ul", "ol", "li", "dl", "dt", "dd", "details", "summary",
    "figure", "figcaption", "time", "table", "thead", "tbody", "tfoot",
    "tr", "th", "td", "caption", "colgroup", "col",
    "button", "select", "option", "input", "label", "progress", "meter", "canvas", "style",
    "svg", "g", "path", "polyline", "polygon", "line", "rect", "circle", "ellipse",
    "text", "tspan", "defs", "lineargradient", "radialgradient", "stop", "clippath",
    "mask", "pattern", "title", "desc",
})
_ALLOWED_ATTRS = frozenset({
    "class", "id", "title", "role", "style", "type", "value", "placeholder",
    "checked", "selected", "disabled", "readonly", "min", "max", "step",
    "colspan", "rowspan", "width", "height", "open", "href", "for", "name",
    "xmlns", "viewbox", "preserveaspectratio", "d", "points", "x", "y",
    "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "dx", "dy",
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
    "stroke-dasharray", "stroke-dashoffset", "stroke-linecap", "stroke-linejoin",
    "stroke-opacity", "opacity", "transform", "text-anchor", "dominant-baseline",
    "font-size", "font-family", "font-weight", "letter-spacing",
    "offset", "stop-color", "stop-opacity", "gradientunits", "gradienttransform",
    "patternunits", "clippathunits", "clip-path", "mask",
})
_DANGEROUS_CSS_RE = re.compile(
    r"(@import|expression\s*\(|javascript\s*:|vbscript\s*:|-moz-binding|behavior\s*:|</style)",
    re.IGNORECASE,
)


def _clean_css(css_text: str) -> str:
    return _DANGEROUS_CSS_RE.sub("/*blocked*/", str(css_text or ""))


class _PluginHtmlSanitizer(HTMLParser):
    """Allowlist HTML5/SVG sanitizer stripping executable scripts, inline JS handlers, and unsafe URLs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._drop_depth = 0
        self._in_style = False
        self._open_stack: list[str] = []

    def _sanitize_attrs(self, tag_low: str, attrs: list[tuple[str, str | None]]) -> str:
        rendered: list[str] = []
        has_btn_type = False
        for raw_k, raw_v in attrs:
            if not raw_k:
                continue
            k_low = raw_k.strip().lower()
            if k_low.startswith("on") or k_low in {"srcdoc", "formaction", "xlink:href"}:
                continue
            allowed = (
                k_low in _ALLOWED_ATTRS
                or (k_low.startswith("data-") and re.fullmatch(r"data-[a-z0-9_-]{1,48}", k_low) is not None)
                or (k_low.startswith("aria-") and re.fullmatch(r"aria-[a-z0-9_-]{1,48}", k_low) is not None)
            )
            if not allowed:
                continue
            val = "" if raw_v is None else str(raw_v)
            if k_low == "href":
                v_strip = val.strip()
                if not v_strip.startswith("#"):
                    continue
            elif k_low == "style":
                val = _clean_css(val)
            elif k_low == "type":
                v_low = val.strip().lower()
                if tag_low == "button":
                    val = "button"
                    has_btn_type = True
                elif v_low not in {"button", "text", "search", "checkbox", "radio", "range", "number"}:
                    val = "text"
            out_k = _SVG_ATTR_CASE_MAP.get(k_low, k_low)
            if raw_v is None and k_low in {"checked", "selected", "disabled", "readonly", "open"}:
                rendered.append(f" {out_k}")
            else:
                rendered.append(f' {out_k}="{_html_escape(val, quote=True)}"')
        if tag_low == "button" and not has_btn_type:
            rendered.append(' type="button"')
        return "".join(rendered)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t_low = tag.lower()
        if self._drop_depth > 0:
            if t_low in _DROP_SUBTREE_TAGS and t_low not in _VOID_HTML_TAGS:
                self._drop_depth += 1
            return
        if t_low in _DROP_SUBTREE_TAGS or _SVG_CASE_MAP.get(t_low) == "":
            if t_low not in _VOID_HTML_TAGS:
                self._drop_depth = 1
            return
        if t_low not in _ALLOWED_TAGS:
            return
        out_tag = _SVG_CASE_MAP.get(t_low, t_low)
        attr_str = self._sanitize_attrs(t_low, attrs)
        if t_low in _VOID_HTML_TAGS:
            self._out.append(f"<{out_tag}{attr_str} />")
            return
        self._out.append(f"<{out_tag}{attr_str}>")
        self._open_stack.append(out_tag)
        if t_low == "style":
            self._in_style = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t_low = tag.lower()
        if self._drop_depth > 0 or t_low in _DROP_SUBTREE_TAGS or t_low not in _ALLOWED_TAGS:
            return
        out_tag = _SVG_CASE_MAP.get(t_low, t_low)
        attr_str = self._sanitize_attrs(t_low, attrs)
        self._out.append(f"<{out_tag}{attr_str} />")

    def handle_endtag(self, tag: str) -> None:
        t_low = tag.lower()
        if self._drop_depth > 0:
            if t_low in _DROP_SUBTREE_TAGS and t_low not in _VOID_HTML_TAGS:
                self._drop_depth = max(0, self._drop_depth - 1)
            return
        if t_low not in _ALLOWED_TAGS or t_low in _VOID_HTML_TAGS:
            return
        out_tag = _SVG_CASE_MAP.get(t_low, t_low)
        if t_low == "style":
            self._in_style = False
        if out_tag in self._open_stack:
            while self._open_stack:
                popped = self._open_stack.pop()
                self._out.append(f"</{popped}>")
                if popped == out_tag:
                    break

    def handle_data(self, data: str) -> None:
        if self._drop_depth > 0:
            return
        if self._in_style:
            self._out.append(_clean_css(data))
        else:
            self._out.append(_html_escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if self._drop_depth == 0 and not self._in_style:
            if re.fullmatch(r"[A-Za-z0-9]{1,24}", name):
                self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._drop_depth == 0 and not self._in_style:
            if re.fullmatch(r"(?:[0-9]{1,7}|x[0-9A-Fa-f]{1,6})", name):
                self._out.append(f"&#{name};")

    def get_html(self) -> str:
        while self._open_stack:
            self._out.append(f"</{self._open_stack.pop()}>")
        return "".join(self._out)


def sanitize_plugin_html(raw_html: str) -> str:
    """Sanitize plugin-authored HTML/SVG markup so custom plugin UIs render safely without script execution."""
    if not isinstance(raw_html, str) or not raw_html.strip():
        return ""
    parser = _PluginHtmlSanitizer()
    parser.feed(raw_html[:500_000])
    parser.close()
    return parser.get_html()
HELP = (
    "Plugin commands: /plugin list; /plugin info NAME; "
    "/plugin run NAME {\"operation\":\"status\"}. "
    "Plugins follow tools/custom/*_plugin.py with TOOL_NAME, TOOL_PROMPT, "
    "PARALLEL_SAFE and execute(action, loop=None). "
    "Running a plugin executes its code with the console user's permissions."
)


def validate_tab(tab: Any) -> dict[str, Any]:
    """Validate a literal TOOL_TAB declaration and return its normalized form."""
    if not isinstance(tab, dict):
        raise ValueError("TOOL_TAB must be a dict")
    unknown = set(tab) - TAB_KEYS
    if unknown:
        raise ValueError(f"TOOL_TAB has unknown keys: {sorted(unknown)}")
    for required in ("title", "operation", "display"):
        if required not in tab:
            raise ValueError(f"TOOL_TAB is missing {required}")
    title = tab["title"]
    if not isinstance(title, str) or not 1 <= len(title) <= 40:
        raise ValueError("TOOL_TAB title must be 1-40 characters")
    icon = tab.get("icon", "🧩")
    if not isinstance(icon, str) or not 1 <= len(icon) <= 4:
        raise ValueError("TOOL_TAB icon must be 1-4 characters")
    operation = tab["operation"]
    if not isinstance(operation, str) or not OPERATION.fullmatch(operation):
        raise ValueError("TOOL_TAB operation must be a lowercase identifier")
    display = tab["display"]
    if display not in TAB_DISPLAYS:
        raise ValueError(f"TOOL_TAB display must be one of {list(TAB_DISPLAYS)}")
    refresh = tab.get("refresh_seconds", 0)
    if isinstance(refresh, bool) or not isinstance(refresh, int):
        raise ValueError("TOOL_TAB refresh_seconds must be an integer")
    if refresh != 0 and not 5 <= refresh <= 300:
        raise ValueError("TOOL_TAB refresh_seconds must be 0 or 5-300")
    return {"title": title, "icon": icon, "operation": operation,
            "display": display, "refresh_seconds": refresh}


def validate_view_declaration(view: Any) -> dict[str, Any]:
    """Validate a literal TOOL_VIEW: the operations that may be called from a rendered view."""
    if not isinstance(view, dict) or set(view) != {"operations"}:
        raise ValueError("TOOL_VIEW must contain exactly operations")
    operations = view["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= 10:
        raise ValueError("TOOL_VIEW needs 1-10 operations")
    seen: list[str] = []
    for op in operations:
        if not isinstance(op, str) or not OPERATION.fullmatch(op) or op in seen:
            raise ValueError("TOOL_VIEW operations must be unique lowercase identifiers")
        seen.append(op)
    return {"operations": seen}


def run_view(name: str, operation: str, inputs: Any, *, root: Path = ROOT) -> dict[str, Any]:
    """Run one operation that a plugin declared in TOOL_VIEW, and validate any view it returns.

    The console calls only declared operations. Inputs are limited to plain scalar
    values; the plugin still validates its own inputs strictly.
    """
    from tools import plugin_views

    plugins, errors = discover(root)
    plugin = plugins.get(name)
    if plugin is None:
        return {"ok": False, "error": "Plugin not found or has a duplicate name"}
    declared = (plugin.get("view") or {}).get("operations", [])
    if operation not in declared:
        return {"ok": False, "error": "operation_not_declared"}
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict) or len(inputs) > 20:
        return {"ok": False, "error": "inputs must be an object with at most 20 fields"}
    for key, value in inputs.items():
        if not isinstance(key, str) or not OPERATION.fullmatch(key):
            return {"ok": False, "error": "input names must be lowercase identifiers"}
        if value is not None and not isinstance(value, (str, int, float, bool)):
            return {"ok": False, "error": "input values must be scalars"}
        if isinstance(value, str) and len(value) > 2000:
            return {"ok": False, "error": "input text is too long"}
    action = {"operation": operation, "inputs": inputs}
    result = execute_command("/plugin run " + name + " " + json.dumps(action, allow_nan=False), root=root)
    if not result.get("ok"):
        return result
    body = result["data"]["result"]
    if isinstance(body, dict) and "view" in body and body.get("ok") is not False:
        try:
            normalized, _ops = plugin_views.validate_view(body["view"])
        except plugin_views.ViewError as exc:
            return {"ok": False, "error": f"Plugin view rejected: {exc}"}
        body = dict(body)
        body["view"] = normalized
        result["data"]["result"] = body
        result["reply"] = json.dumps(result["data"], indent=2, ensure_ascii=False, allow_nan=False)
    return result


def validate_connection(connection: Any) -> dict[str, Any]:
    """Validate user-facing settings metadata without importing plugin code."""
    if not isinstance(connection, dict) or set(connection) - {"title", "description", "fields"}:
        raise ValueError("TOOL_CONNECTION must contain title, optional description, and fields")
    title = connection.get("title")
    description = connection.get("description", "")
    fields = connection.get("fields")
    if not isinstance(title, str) or not 1 <= len(title) <= 60:
        raise ValueError("TOOL_CONNECTION title must be 1-60 characters")
    if not isinstance(description, str) or len(description) > 300:
        raise ValueError("TOOL_CONNECTION description is too long")
    if not isinstance(fields, list) or not 1 <= len(fields) <= 12:
        raise ValueError("TOOL_CONNECTION must declare 1-12 fields")
    result = []
    seen = set()
    for field in fields:
        if not isinstance(field, dict) or set(field) - {"key", "label", "secret", "required", "kind", "placeholder", "default"}:
            raise ValueError("Invalid TOOL_CONNECTION field")
        key, label = field.get("key"), field.get("label")
        if not isinstance(key, str) or not OPERATION.fullmatch(key) or key in seen:
            raise ValueError("Connection field keys must be unique lowercase identifiers")
        if not isinstance(label, str) or not 1 <= len(label) <= 80:
            raise ValueError("Connection field label must be 1-80 characters")
        secret, required = field.get("secret", False), field.get("required", True)
        if type(secret) is not bool or type(required) is not bool:
            raise ValueError("Connection secret and required flags must be boolean")
        if any(part in key for part in ("api_key", "secret", "password", "token")) and not secret:
            raise ValueError("Credential fields must be secret")
        kind = field.get("kind", "text")
        if kind not in ("text", "url") or (secret and kind != "text"):
            raise ValueError("Connection kind must be text or url")
        placeholder, default = field.get("placeholder", ""), field.get("default", "")
        if (not isinstance(placeholder, str) or len(placeholder) > 120
                or not isinstance(default, str) or len(default) > 2048 or (secret and default)):
            raise ValueError("Invalid connection placeholder or default")
        seen.add(key)
        result.append({"key": key, "label": label, "secret": secret, "required": required,
                       "kind": kind, "placeholder": placeholder, "default": default})
    return {"title": title, "description": description, "fields": result}


def discover(root: Path = ROOT) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    root = Path(root).resolve()
    plugins: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    duplicates: set[str] = set()
    for folder, pattern in SOURCES:
        directory = (root / "tools" / folder).resolve()
        if not directory.is_relative_to(root):
            errors.append({"file": folder, "error": "Plugin directory resolves outside console root"})
            continue
        for path in sorted(directory.glob(pattern)):
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(directory):
                    raise ValueError("Plugin resolves outside its plugin directory")
                source = resolved.read_bytes()
                tree = ast.parse(source, filename=str(path))
                metadata = {}
                for node in tree.body:
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id in {"TOOL_NAME", "TOOL_PROMPT", "PARALLEL_SAFE", "TOOL_ACCESS", "TOOL_TAB", "TOOL_CONNECTION", "TOOL_VIEW"}:
                                try:
                                    metadata[target.id] = ast.literal_eval(node.value)
                                except (ValueError, TypeError):
                                    pass
                if "TOOL_NAME" not in metadata:
                    # Some *_engine.py files are standalone scripts, not plugins.
                    if folder == "engines":
                        continue
                    raise ValueError("Missing literal TOOL_NAME")
                name = metadata["TOOL_NAME"]
                if not isinstance(name, str) or not NAME.fullmatch(name):
                    raise ValueError("TOOL_NAME must be a lowercase identifier")
                if name in plugins or name in duplicates:
                    plugins.pop(name, None)
                    duplicates.add(name)
                    raise ValueError("Duplicate TOOL_NAME")
                access = metadata.get("TOOL_ACCESS")
                if access is not None and access not in ACCESS_VALUES:
                    raise ValueError("TOOL_ACCESS must be 'read' or 'write'")
                tab = None
                if "TOOL_TAB" in metadata:
                    if access != "read":
                        raise ValueError("Plugins with a TOOL_TAB must declare TOOL_ACCESS = 'read'")
                    tab = validate_tab(metadata["TOOL_TAB"])
                connection = validate_connection(metadata["TOOL_CONNECTION"]) if "TOOL_CONNECTION" in metadata else None
                view_decl = validate_view_declaration(metadata["TOOL_VIEW"]) if "TOOL_VIEW" in metadata else None
                plugins[name] = {
                    "view": view_decl,
                    "name": name,
                    "description": str(metadata.get("TOOL_PROMPT", "")),
                    "parallel_safe": metadata.get("PARALLEL_SAFE"),
                    "access": access,
                    "tab": tab,
                    "connection": connection,
                    "path": str(resolved),
                    "sha256": hashlib.sha256(source).hexdigest(),
                }
            except (OSError, SyntaxError, UnicodeError, ValueError) as exc:
                errors.append({"file": str(path), "error": str(exc)})
    return plugins, errors


def list_tabs(root: Path = ROOT) -> dict[str, Any]:
    """Return valid plugin tab declarations for the console nav, without running any plugin."""
    plugins, errors = discover(root)
    tabs = [{"plugin": name, **plugin["tab"]} for name, plugin in sorted(plugins.items()) if plugin["tab"]]
    return {"ok": True, "tabs": tabs, "discovery_errors": errors}


def invoke(plugin: dict[str, Any], action: dict[str, Any]) -> Any:
    path = Path(plugin["path"])
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != plugin["sha256"]:
        raise RuntimeError("Plugin changed since discovery; retry the command")
    # Compile the inspected bytes directly so invocation cannot use a stale pyc.
    module_name = "tools." + path.parent.name + "._operator_" + plugin["sha256"][:16]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None:
        raise RuntimeError("Could not prepare plugin module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
        if getattr(module, "TOOL_NAME", None) != plugin["name"]:
            raise ValueError("Plugin name changed at import")
        execute = getattr(module, "execute", None)
        if not callable(execute):
            raise ValueError("Plugin does not expose callable execute")
        return execute(action)
    finally:
        sys.modules.pop(module_name, None)


def execute_command(command: str, *, root: Path = ROOT) -> dict[str, Any]:
    parts = command.strip().split(None, 3)
    if len(parts) < 2 or parts[1].lower() == "help":
        return {"ok": True, "reply": HELP}
    plugins, errors = discover(root)
    operation = parts[1].lower()
    if operation == "list" and len(parts) == 2:
        data = {"plugins": list(plugins.values()), "discovery_errors": errors}
    elif operation == "info" and len(parts) == 3:
        if parts[2] not in plugins:
            return {"ok": False, "error": "Plugin not found or has a duplicate name"}
        data = plugins[parts[2]]
    elif operation == "run" and len(parts) == 4:
        if parts[2] not in plugins:
            return {"ok": False, "error": "Plugin not found or has a duplicate name"}
        try:
            action = json.loads(parts[3])
            if not isinstance(action, dict):
                raise ValueError("Action must be a JSON object")
            result = invoke(plugins[parts[2]], action)
            if isinstance(result, str):
                try:
                    decoded = json.loads(result)
                except ValueError:
                    pass
                else:
                    if isinstance(decoded, (dict, list)):
                        result = decoded
            if isinstance(result, dict) and isinstance(result.get("html"), str):
                result = dict(result)
                result["html"] = sanitize_plugin_html(result["html"])
            data = {"plugin": parts[2], "source_sha256": plugins[parts[2]]["sha256"], "result": result}
            json.dumps(data, allow_nan=False)
        except (Exception, SystemExit) as exc:
            return {"ok": False, "error": f"Plugin execution failed: {type(exc).__name__}: {exc}"}
    else:
        return {"ok": False, "error": HELP}
    plugin_ok = not (operation == "run" and isinstance(data["result"], dict)
                     and data["result"].get("ok") is False)
    return {"ok": plugin_ok, "reply": json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False), "data": data}
