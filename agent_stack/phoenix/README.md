# Arize Phoenix (Self-Hosted LLM & Agent Tracing)

Self-hosted [Arize Phoenix](https://phoenix.arize.com/) instance running on `ego` (`192.168.1.133`) for Living Archive and Sabrina agent observability.

## Endpoints (LAN)

| Service | URL / Address |
| --- | --- |
| Web UI | `http://192.168.1.133:6006` |
| OTLP / HTTP Traces | `http://192.168.1.133:6006/v1/traces` |
| OTLP / gRPC Traces | `192.168.1.133:4317` |

## Deployment on `ego`

- **Container name:** `livingarchive-phoenix`
- **Image:** `arizephoenix/phoenix:latest`
- **Persistent storage:** `/mnt/webapps-nvme/agent_stack/phoenix/data` mounted to `/phoenix-data` (`PHOENIX_WORKING_DIR=/phoenix-data`)
- **Log rotation:** `json-file` driver with `max-size=10m`, `max-file=3`

Because `docker compose` is not installed on `ego`, the container is started directly with `docker run` (see `docker-compose.yml` for the equivalent portable reference):

```bash
mkdir -p /mnt/webapps-nvme/agent_stack/phoenix/data

docker run -d \
  --name livingarchive-phoenix \
  --restart unless-stopped \
  -p 6006:6006 \
  -p 4317:4317 \
  -e PHOENIX_WORKING_DIR=/phoenix-data \
  -e PHOENIX_ENABLE_AUTH=false \
  -v /mnt/webapps-nvme/agent_stack/phoenix/data:/phoenix-data \
  --log-driver json-file \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  arizephoenix/phoenix:latest
```

## Environment Variables (Clients / Agents)

```bash
export PHOENIX_COLLECTOR_ENDPOINT="http://192.168.1.133:6006"
export OTEL_EXPORTER_OTLP_ENDPOINT="http://192.168.1.133:6006"
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://192.168.1.133:6006/v1/traces"
export PHOENIX_PROJECT_NAME="living-archive"
export OTEL_SERVICE_NAME="livingarchive-agent"
```

## Quick Usage (`otel_trace_helper.py`)

`otel_trace_helper.py` uses only the Python standard library to send OTLP/HTTP JSON spans to Phoenix:

```bash
python3 otel_trace_helper.py --name "sabrina.healthcheck"
```

In Python code:

```python
from otel_trace_helper import trace_span

with trace_span("sabrina.retrieve", span_kind="RETRIEVER") as span:
    span.input_value = "user query"
    # ... do work ...
    span.output_value = "retrieved docs"
    span.set_attribute("doc.count", 5)
```
