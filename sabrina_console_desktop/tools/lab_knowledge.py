"""
tools/lab_knowledge.py
=======================
Redis-backed knowledge store hosted on the lab GPU host (192.168.2.69:6379 by
default, matching SABRINA_REDIS_URL in docker-compose.dev.yml) — this is the
canonical, network-reachable home for Sabrina's knowledge base, so it works
the same way regardless of which machine sabrina_gemini_loop.py runs on.

No RediSearch module is assumed to be installed, so "search" is done client
side: pull every stored chunk (expected to stay small — dozens to low
hundreds of wordlist/payload entries) and score by keyword overlap. That's
plenty for exact-match wordlist/payload retrieval, which is what this is for.

tools/rag_knowledge.py (local SQLite+FTS5) remains as an offline fallback for
when the lab network isn't reachable.
"""

import os
import re

try:
    import redis
except ImportError:
    redis = None

from tools.rag_knowledge import SEED_KNOWLEDGE  # reuse the same curated content

REDIS_URL = os.environ.get("SABRINA_REDIS_URL", "redis://192.168.2.69:6379/0")
IDS_KEY = "sabrina:knowledge:ids"
NEXT_ID_KEY = "sabrina:knowledge:next_id"
CHUNK_KEY_PREFIX = "sabrina:knowledge:chunk:"

_CONNECT_TIMEOUT = 3  # seconds — fail fast rather than stalling the main loop


def _client(url=None):
    if redis is None:
        raise RuntimeError("redis-py is not installed")
    return redis.from_url(
        url or REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=_CONNECT_TIMEOUT,
        socket_timeout=_CONNECT_TIMEOUT,
    )


def _score(query_text: str, topic: str, content: str) -> int:
    terms = set(re.findall(r"[a-z0-9_]{3,}", (query_text or "").lower()))
    if not terms:
        return 0
    haystack = f"{topic}\n{content}".lower()
    return sum(haystack.count(t) for t in terms)


def add_knowledge(topic: str, content: str, source_type: str = "seed", url=None) -> str:
    c = _client(url)
    chunk_id = str(c.incr(NEXT_ID_KEY))
    c.hset(
        f"{CHUNK_KEY_PREFIX}{chunk_id}",
        mapping={"topic": topic, "content": content, "source_type": source_type},
    )
    c.sadd(IDS_KEY, chunk_id)
    return chunk_id


def upsert_knowledge(topic: str, content: str, source_type: str = "seed", url=None) -> str:
    """
    Like add_knowledge, but replaces any existing chunk(s) with the same topic first.
    Used for Sabrina's write-back pipeline: refining the same script/methodology
    across rounds should update one entry, not pile up near-duplicates.
    """
    c = _client(url)
    for chunk_id in list(c.smembers(IDS_KEY)):
        key = f"{CHUNK_KEY_PREFIX}{chunk_id}"
        if c.hget(key, "topic") == topic:
            c.delete(key)
            c.srem(IDS_KEY, chunk_id)
    return add_knowledge(topic, content, source_type=source_type, url=url)


def query_knowledge(query_text: str, top_k: int = 5, url=None) -> list:
    """
    Returns [] on any connection failure (unreachable lab network, redis-py
    missing, etc.) — this is a non-fatal augmentation, never a hard dependency.
    """
    try:
        c = _client(url)
        ids = c.smembers(IDS_KEY)
        if not ids:
            return []
        scored = []
        for chunk_id in ids:
            data = c.hgetall(f"{CHUNK_KEY_PREFIX}{chunk_id}")
            if not data:
                continue
            score = _score(query_text, data.get("topic", ""), data.get("content", ""))
            if score > 0:
                scored.append((score, data))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            {"topic": d.get("topic", ""), "content": d.get("content", ""), "score": s}
            for s, d in scored[:top_k]
        ]
    except Exception:
        return []


def seed_default_knowledge(url=None, skip_if_populated: bool = True) -> int:
    try:
        c = _client(url)
        if skip_if_populated and c.scard(IDS_KEY) > 0:
            return 0
        count = 0
        for topic, content in SEED_KNOWLEDGE:
            add_knowledge(topic, content, source_type="pentest_seed", url=url)
            count += 1
        return count
    except Exception as e:
        print(f"[lab_knowledge] Seeding failed (lab network unreachable?): {e}")
        return 0


def is_reachable(url=None) -> bool:
    try:
        _client(url).ping()
        return True
    except Exception:
        return False


def fetch_all(url=None) -> list:
    """Every stored chunk as {topic, content, source_type} — used to mirror into the local offline cache."""
    try:
        c = _client(url)
        ids = c.smembers(IDS_KEY)
        out = []
        for chunk_id in ids:
            data = c.hgetall(f"{CHUNK_KEY_PREFIX}{chunk_id}")
            if data:
                out.append(data)
        return out
    except Exception:
        return []


def clear_all(url=None):
    """Wipes every entry from the lab store. Only meant for a deliberate reseed."""
    c = _client(url)
    ids = c.smembers(IDS_KEY)
    for chunk_id in ids:
        c.delete(f"{CHUNK_KEY_PREFIX}{chunk_id}")
    c.delete(IDS_KEY)
    c.delete(NEXT_ID_KEY)


if __name__ == "__main__":
    print("Lab Redis reachable:", is_reachable())
    n = seed_default_knowledge()
    print(f"Seeded {n} knowledge chunks on the lab server.")
    print(query_knowledge("xss payload script alert", top_k=3))
