import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harnesses.external.collective_pool import KindCollectivePool, DEFAULT_DB_PATH
pool = KindCollectivePool(min_cooldown_sec=0.1, db_path=DEFAULT_DB_PATH)
res = pool.invoke_member("hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api", "TASK: Reply with 'HEALTHCHECK_OK'", "hc_01", False)
print("DEBUG_INVOKE_RES:", res)
