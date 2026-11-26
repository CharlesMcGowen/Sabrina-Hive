import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.oracle_providers import UnifiedOracleHub

hub = UnifiedOracleHub()
print("enable_live_collective:", hub.enable_live_collective)
rep, lbl = hub.query(prompt="Echo ACK_SWARM_ONLINE", preferred="collective")
print("ORACLE_QUERY_SUCCESS:", lbl, "length:", len(rep))
