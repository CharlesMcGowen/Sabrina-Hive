"""Read-only inventory; no inference of failure cause from a zero score."""
import json
from tools.portal_cli import audit
from tools.portal_policy import DEFAULT_DB
if __name__ == '__main__':
    print(json.dumps(audit(DEFAULT_DB), indent=2))
