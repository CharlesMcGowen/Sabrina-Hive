import dataclasses
from datetime import datetime
from typing import Dict, Optional

import urllib.parse
import urllib.request
import http.client
import time

@dataclasses.dataclass
class TelemetryProbeResult:
    url: str
    method: str
    status_code: int
    headers: Dict[str, str]
    body_snippet: str
    body_size: int
    elapsed_ms: float
    error: str = ""

from urllib.parse import urljoin

class EndpointNormalizer:
    @staticmethod
    def normalize_target(base_url: str, endpoint_path: str) -> str:
        return urljoin(base_url, endpoint_path)

class TelemetryProber:
    @staticmethod
    def send_probe(url: str, method: str = "GET", headers: Optional[Dict[str, str]] = None, body: Optional[str] = None, timeout: float = 10.0) -> TelemetryProbeResult:
        from urllib.parse import urlencode

        # Ensure method is string
        if not isinstance(method, str):
            raise ValueError("Method must be a string")

        headers = headers if headers is not None else {}
        
        if method != 'GET' and body is not None:
            data = body.encode('utf-8') if isinstance(body, str) else body
        else:
            data = None

        start_time = time.time()
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status_code = response.status
                headers_dict = dict(response.headers)
                body_snippet = response.read().decode('utf-8', errors='replace')[:2000]
                body_size = len(body_snippet.encode('utf-8'))
        except http.client.IncompleteRead as e:
            status_code = 200
            headers_dict = {}
            body_snippet = e.partial.decode('utf-8', errors='replace')[:2000]
            body_size = len(e.partial)
        except urllib.error.HTTPError as e:
            status_code = e.code
            headers_dict = dict(e.headers)
            body_snippet = e.read().decode('utf-8', errors='replace')[:2000]
            body_size = 0
        except (urllib.error.URLError, Exception) as e:
            status_code = 0
            headers_dict = {}
            body_snippet = ""
            body_size = 0

        elapsed_ms = (time.time() - start_time) * 1000
        return TelemetryProbeResult(url, method.upper(), status_code, headers_dict, body_snippet, body_size, elapsed_ms)

if __name__ == '__main__':
    assert EndpointNormalizer.normalize_target("http://localhost:3000/rest/country-mapping", "/rest/country-mapping") == "http://localhost:3000/rest/country-mapping"
    assert EndpointNormalizer.normalize_target("http://localhost:3000", "/rest/admin") == "http://localhost:3000/rest/admin"
    assert EndpointNormalizer.normalize_target("http://localhost:3000/", "api/BasketItems") == "http://localhost:3000/api/BasketItems"

    sample = TelemetryProbeResult("http://test", "GET", 200, {}, "ok", 2, 10.5)
    assert sample.status_code == 200 and sample.body_snippet == "ok"

    print("[SELF-TEST] ALL TESTS PASSED")