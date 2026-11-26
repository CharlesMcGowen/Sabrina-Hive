#!/usr/bin/env python3
"""Minimal stdlib-only OpenTelemetry OTLP/HTTP trace helper for Arize Phoenix.

Allows Living Archive / Sabrina agents to emit spans to self-hosted Arize Phoenix
at http://192.168.1.133:6006/v1/traces without requiring heavy OpenTelemetry SDKs.

Includes both:
- OTLP/HTTP JSON payload builder (`build_otlp_payload`) for debugging or JSON collectors.
- Zero-dependency OTLP/HTTP Protobuf encoder (`encode_otlp_protobuf`), used by default
  because Arize Phoenix's `/v1/traces` endpoint expects `application/x-protobuf`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import struct
import sys
import time
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

DEFAULT_TRACES_ENDPOINT = "http://192.168.1.133:6006/v1/traces"
DEFAULT_SERVICE_NAME = "livingarchive-agent"
DEFAULT_PROJECT_NAME = "living-archive"

# OTLP SpanKind enum values
SPAN_KIND_INTERNAL = 1
SPAN_KIND_SERVER = 2
SPAN_KIND_CLIENT = 3
SPAN_KIND_PRODUCER = 4
SPAN_KIND_CONSUMER = 5

# OTLP StatusCode enum values
STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2


def resolve_endpoint(endpoint: Optional[str] = None) -> str:
    """Resolve the OTLP/HTTP traces endpoint from argument or environment."""
    if endpoint:
        url = endpoint.strip()
    elif os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        url = os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"].strip()
    elif os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        url = os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"].strip().rstrip("/") + "/v1/traces"
    elif os.environ.get("PHOENIX_COLLECTOR_ENDPOINT"):
        base = os.environ["PHOENIX_COLLECTOR_ENDPOINT"].strip().rstrip("/")
        url = base if base.endswith("/v1/traces") else f"{base}/v1/traces"
    else:
        url = DEFAULT_TRACES_ENDPOINT
    return url


def _encode_json_value(value: Any) -> Dict[str, Any]:
    """Encode a Python primitive or sequence into an OTLP JSON AnyValue dict."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_encode_json_value(v) for v in value]}}
    if isinstance(value, dict):
        return {"stringValue": json.dumps(value, default=str)}
    return {"stringValue": str(value)}


def _encode_json_attributes(attrs: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not attrs:
        return []
    return [{"key": str(k), "value": _encode_json_value(v)} for k, v in attrs.items() if v is not None]


# --- Minimal stdlib Protobuf wire encoder for OTLP ExportTraceServiceRequest ---


def _pb_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def _pb_tag(field_number: int, wire_type: int) -> bytes:
    return _pb_varint((field_number << 3) | wire_type)


def _pb_bytes(field_number: int, data: bytes) -> bytes:
    return _pb_tag(field_number, 2) + _pb_varint(len(data)) + data


def _pb_string(field_number: int, text: str) -> bytes:
    return _pb_bytes(field_number, text.encode("utf-8"))


def _pb_varint_field(field_number: int, value: int) -> bytes:
    return _pb_tag(field_number, 0) + _pb_varint(value)


def _pb_fixed64(field_number: int, value: int) -> bytes:
    return _pb_tag(field_number, 1) + struct.pack("<Q", value)


def _pb_double(field_number: int, value: float) -> bytes:
    return _pb_tag(field_number, 1) + struct.pack("<d", value)


def _pb_any_value(value: Any) -> bytes:
    if isinstance(value, bool):
        return _pb_varint_field(2, 1 if value else 0)
    if isinstance(value, int):
        return _pb_varint_field(3, value)
    if isinstance(value, float):
        return _pb_double(4, value)
    if isinstance(value, (list, tuple)):
        arr = b"".join(_pb_bytes(1, _pb_any_value(v)) for v in value)
        return _pb_bytes(5, arr)
    if isinstance(value, dict):
        return _pb_string(1, json.dumps(value, default=str))
    return _pb_string(1, str(value))


def _pb_kv(key: str, value: Any) -> bytes:
    return _pb_string(1, key) + _pb_bytes(2, _pb_any_value(value))


def encode_otlp_protobuf(payload: Dict[str, Any]) -> bytes:
    """Encode a `build_otlp_payload()` dict into OTLP ExportTraceServiceRequest protobuf bytes."""
    out = bytearray()
    for rs in payload.get("resourceSpans", []):
        rs_buf = bytearray()
        res_attrs = rs.get("resource", {}).get("attributes", [])
        if res_attrs:
            res_buf = bytearray()
            for attr in res_attrs:
                res_buf.extend(_pb_bytes(1, _pb_kv_from_json(attr)))
            rs_buf.extend(_pb_bytes(1, bytes(res_buf)))

        for ss in rs.get("scopeSpans", []):
            ss_buf = bytearray()
            scope = ss.get("scope", {})
            if scope:
                sc_buf = bytearray()
                if scope.get("name"):
                    sc_buf.extend(_pb_string(1, scope["name"]))
                if scope.get("version"):
                    sc_buf.extend(_pb_string(2, scope["version"]))
                ss_buf.extend(_pb_bytes(1, bytes(sc_buf)))

            for sp in ss.get("spans", []):
                sp_buf = bytearray()
                sp_buf.extend(_pb_bytes(1, bytes.fromhex(sp["traceId"])))
                sp_buf.extend(_pb_bytes(2, bytes.fromhex(sp["spanId"])))
                if sp.get("parentSpanId"):
                    sp_buf.extend(_pb_bytes(4, bytes.fromhex(sp["parentSpanId"])))
                sp_buf.extend(_pb_string(5, sp["name"]))
                if sp.get("kind"):
                    sp_buf.extend(_pb_varint_field(6, int(sp["kind"])))
                sp_buf.extend(_pb_fixed64(7, int(sp["startTimeUnixNano"])))
                sp_buf.extend(_pb_fixed64(8, int(sp["endTimeUnixNano"])))
                for attr in sp.get("attributes", []):
                    sp_buf.extend(_pb_bytes(9, _pb_kv_from_json(attr)))
                status = sp.get("status")
                if status:
                    st_buf = bytearray()
                    if status.get("message"):
                        st_buf.extend(_pb_string(2, status["message"]))
                    if status.get("code"):
                        st_buf.extend(_pb_varint_field(3, int(status["code"])))
                    sp_buf.extend(_pb_bytes(15, bytes(st_buf)))
                ss_buf.extend(_pb_bytes(2, bytes(sp_buf)))
            rs_buf.extend(_pb_bytes(2, bytes(ss_buf)))
        out.extend(_pb_bytes(1, bytes(rs_buf)))
    return bytes(out)


def _pb_any_from_json(val: Dict[str, Any]) -> bytes:
    if "stringValue" in val:
        return _pb_string(1, str(val["stringValue"]))
    if "boolValue" in val:
        return _pb_varint_field(2, 1 if val["boolValue"] else 0)
    if "intValue" in val:
        return _pb_varint_field(3, int(val["intValue"]))
    if "doubleValue" in val:
        return _pb_double(4, float(val["doubleValue"]))
    if "arrayValue" in val:
        arr = b"".join(_pb_bytes(1, _pb_any_from_json(v)) for v in val["arrayValue"].get("values", []))
        return _pb_bytes(5, arr)
    return _pb_string(1, json.dumps(val, default=str))


def _pb_kv_from_json(attr: Dict[str, Any]) -> bytes:
    return _pb_string(1, str(attr["key"])) + _pb_bytes(2, _pb_any_from_json(attr["value"]))


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def build_otlp_payload(
    *,
    name: str,
    start_time_ns: int,
    end_time_ns: int,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    kind: int = SPAN_KIND_INTERNAL,
    attributes: Optional[Dict[str, Any]] = None,
    status_code: int = STATUS_OK,
    status_message: str = "",
    service_name: Optional[str] = None,
    project_name: Optional[str] = None,
    scope_name: str = "livingarchive.otel_trace_helper",
    scope_version: str = "1.0.0",
) -> Dict[str, Any]:
    """Build an OTLP/HTTP JSON ExportTraceServiceRequest payload for a single span."""
    svc = service_name or os.environ.get("OTEL_SERVICE_NAME", DEFAULT_SERVICE_NAME)
    proj = project_name or os.environ.get("PHOENIX_PROJECT_NAME", DEFAULT_PROJECT_NAME)

    span: Dict[str, Any] = {
        "traceId": trace_id or new_trace_id(),
        "spanId": span_id or new_span_id(),
        "name": name,
        "kind": kind,
        "startTimeUnixNano": str(start_time_ns),
        "endTimeUnixNano": str(end_time_ns),
        "attributes": _encode_json_attributes(attributes),
        "status": {"code": status_code},
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    if status_message:
        span["status"]["message"] = status_message

    resource_attrs = {
        "service.name": svc,
        "openinference.project.name": proj,
    }

    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _encode_json_attributes(resource_attrs)},
                "scopeSpans": [
                    {
                        "scope": {"name": scope_name, "version": scope_version},
                        "spans": [span],
                    }
                ],
            }
        ]
    }


def post_otlp_payload(
    payload: Dict[str, Any],
    *,
    endpoint: Optional[str] = None,
    timeout: float = 5.0,
    headers: Optional[Dict[str, str]] = None,
    use_protobuf: bool = True,
) -> int:
    """POST an OTLP/HTTP payload (protobuf by default, or JSON) to Phoenix."""
    url = resolve_endpoint(endpoint)
    if use_protobuf:
        body = encode_otlp_protobuf(payload)
        req_headers = {"Content-Type": "application/x-protobuf"}
    else:
        body = json.dumps(payload).encode("utf-8")
        req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()
        return int(resp.status)


def send_span(
    name: str,
    *,
    attributes: Optional[Dict[str, Any]] = None,
    input_value: Optional[Any] = None,
    output_value: Optional[Any] = None,
    span_kind: Optional[str] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    start_time_ns: Optional[int] = None,
    end_time_ns: Optional[int] = None,
    status_code: int = STATUS_OK,
    status_message: str = "",
    service_name: Optional[str] = None,
    project_name: Optional[str] = None,
    endpoint: Optional[str] = None,
    timeout: float = 5.0,
    use_protobuf: bool = True,
) -> Dict[str, Any]:
    """Emit a single span to Arize Phoenix via OTLP/HTTP."""
    now_ns = time.time_ns()
    start_ns = start_time_ns if start_time_ns is not None else now_ns
    end_ns = end_time_ns if end_time_ns is not None else max(now_ns, start_ns + 1_000_000)

    merged_attrs: Dict[str, Any] = dict(attributes or {})
    if span_kind:
        merged_attrs.setdefault("openinference.span.kind", span_kind)
    if input_value is not None:
        merged_attrs.setdefault("input.value", input_value)
    if output_value is not None:
        merged_attrs.setdefault("output.value", output_value)

    tid = trace_id or new_trace_id()
    sid = span_id or new_span_id()

    payload = build_otlp_payload(
        name=name,
        start_time_ns=start_ns,
        end_time_ns=end_ns,
        trace_id=tid,
        span_id=sid,
        parent_span_id=parent_span_id,
        attributes=merged_attrs,
        status_code=status_code,
        status_message=status_message,
        service_name=service_name,
        project_name=project_name,
    )
    http_status = post_otlp_payload(
        payload,
        endpoint=endpoint,
        timeout=timeout,
        use_protobuf=use_protobuf,
    )
    return {"http_status": http_status, "trace_id": tid, "span_id": sid}


class SpanContext:
    """Mutable span handle yielded by trace_span()."""

    def __init__(self, trace_id: str, span_id: str) -> None:
        self.trace_id = trace_id
        self.span_id = span_id
        self.attributes: Dict[str, Any] = {}
        self.input_value: Optional[Any] = None
        self.output_value: Optional[Any] = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


@contextlib.contextmanager
def trace_span(
    name: str,
    *,
    attributes: Optional[Dict[str, Any]] = None,
    span_kind: str = "CHAIN",
    trace_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    service_name: Optional[str] = None,
    project_name: Optional[str] = None,
    endpoint: Optional[str] = None,
    timeout: float = 5.0,
    suppress_export_errors: bool = True,
) -> Iterator[SpanContext]:
    """Context manager that times a block and exports an OTLP/HTTP span on exit."""
    ctx = SpanContext(trace_id=trace_id or new_trace_id(), span_id=new_span_id())
    if attributes:
        ctx.attributes.update(attributes)
    start_ns = time.time_ns()
    status_code = STATUS_OK
    status_message = ""
    try:
        yield ctx
    except Exception as exc:
        status_code = STATUS_ERROR
        status_message = f"{type(exc).__name__}: {exc}"
        ctx.attributes.setdefault("exception.type", type(exc).__name__)
        ctx.attributes.setdefault("exception.message", str(exc))
        raise
    finally:
        end_ns = time.time_ns()
        try:
            send_span(
                name=name,
                attributes=ctx.attributes,
                input_value=ctx.input_value,
                output_value=ctx.output_value,
                span_kind=span_kind,
                trace_id=ctx.trace_id,
                span_id=ctx.span_id,
                parent_span_id=parent_span_id,
                start_time_ns=start_ns,
                end_time_ns=end_ns,
                status_code=status_code,
                status_message=status_message,
                service_name=service_name,
                project_name=project_name,
                endpoint=endpoint,
                timeout=timeout,
            )
        except Exception as export_exc:
            if not suppress_export_errors:
                raise
            print(f"[otel_trace_helper] span export failed: {export_exc}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Send a test OTLP/HTTP span to Arize Phoenix.")
    parser.add_argument("--endpoint", default=None, help=f"OTLP traces URL (default: {DEFAULT_TRACES_ENDPOINT})")
    parser.add_argument("--name", default="phoenix.smoke_test", help="Span name")
    parser.add_argument("--project", default=DEFAULT_PROJECT_NAME, help="Phoenix project name")
    parser.add_argument("--service", default=DEFAULT_SERVICE_NAME, help="Service name")
    parser.add_argument("--json", action="store_true", help="Send application/json instead of protobuf")
    args = parser.parse_args(argv)

    result = send_span(
        args.name,
        span_kind="AGENT",
        input_value="ping",
        output_value="pong",
        attributes={"smoke_test": True, "source": "otel_trace_helper.py"},
        service_name=args.service,
        project_name=args.project,
        endpoint=args.endpoint,
        use_protobuf=not args.json,
    )
    print(json.dumps({"endpoint": resolve_endpoint(args.endpoint), **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
