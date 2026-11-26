import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.collective_router_and_shadow_daemon import CollectiveRouter

router = CollectiveRouter()
res = router.route(
    "Explain how atomic compare-and-swap (CAS) eliminates race conditions in concurrent data structures in two concise sentences.",
    tier="deep_reasoning"
)
print("TIER 2 ROUTED TO:", res.get("portal_id"), "| ACTUAL MODEL:", res.get("actual_model"))
print("LATENCY:", res.get("latency_ms"), "ms | CACHE:", res.get("cache_match_type"))
print("REPLY:", res.get("reply")[:200])
