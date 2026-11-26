#!/usr/bin/env python3
"""
bb_semantic_link.py
===================
Semantic linking over the bug-bounty report corpus -- the DATA layer of the self-training flywheel,
designed to run on idle CPU while the GPUs train.

For each report it computes a text embedding, links each report to its top-k semantically nearest
neighbours (a kNN similarity graph), and MEASURES the link quality as neighbour-label-agreement@k:
for every report that carries a ground-truth weakness class, the fraction of its k neighbours that
share that class. That scalar is the RSI objective -- an improvement loop can later raise it by
improving the embedding, the text preprocessing, k, or a reranker (never-worse gated).

Embeddings: sentence-transformers (all-MiniLM-L6-v2) on CPU if available; otherwise a dependency-light
TF-IDF fallback (scikit-learn), so it always produces links even with no model/network. English text.

Outputs:
    <out-graph>  nodes + kNN edges (the semantic link graph, for RAG / dedup / curation)
    <out-score>  {link_quality, per_class, n, k, method}  (append-only history)

CLI:
    python bb_semantic_link.py --corpus /workspace/rl/bugbounty_vuln_tasks.jsonl --k 5
"""
import argparse
import json
import os
from collections import Counter, defaultdict


def load_corpus(path):
    recs = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        title = str(r.get("title") or "")
        text = str(r.get("text") or r.get("vulnerability_information") or r.get("prompt") or "")
        label = r.get("gold_label") or r.get("weakness") or r.get("ground_truth_weakness")
        if isinstance(label, dict):
            label = label.get("name") or label.get("id")
        rid = r.get("name") or r.get("id") or f"rec{len(recs)}"
        blob = (title + "\n" + text).strip()
        if len(blob) >= 30:
            recs.append({"id": str(rid), "label": (str(label) if label else None),
                         "text": blob[:4000]})
    return recs


def embed(texts):
    """Return (matrix [n,d] L2-normalized, method_name). Try sentence-transformers, else TF-IDF."""
    try:
        from sentence_transformers import SentenceTransformer
        import numpy as np
        model = SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
        emb = model.encode(texts, batch_size=64, show_progress_bar=False,
                           normalize_embeddings=True)
        return np.asarray(emb, dtype="float32"), "sentence-transformers/all-MiniLM-L6-v2"
    except Exception as e:
        print(f"[bb_link] sentence-transformers unavailable ({e}); TF-IDF fallback", flush=True)
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.preprocessing import normalize
        import numpy as np
        X = TfidfVectorizer(max_features=4096, stop_words="english",
                            ngram_range=(1, 2)).fit_transform(texts)
        return normalize(X).astype("float32").toarray(), "tfidf"


def build_graph(recs, k):
    import numpy as np
    mat, method = embed([r["text"] for r in recs])
    sims = mat @ mat.T                      # cosine (rows are L2-normalized)
    np.fill_diagonal(sims, -1.0)            # exclude self
    n = len(recs)
    edges, neighbours = [], []
    for i in range(n):
        top = np.argsort(-sims[i])[:k]
        neigh = [{"id": recs[j]["id"], "sim": round(float(sims[i][j]), 4)} for j in top]
        neighbours.append(neigh)
        for j in top:
            edges.append({"src": recs[i]["id"], "dst": recs[j]["id"],
                          "sim": round(float(sims[i][j]), 4)})
    # link-quality metric: neighbour label agreement@k over labelled reports
    agree, counted, per_class = 0.0, 0, defaultdict(lambda: [0.0, 0])
    for i, r in enumerate(recs):
        if not r["label"]:
            continue
        same = sum(1 for j in np.argsort(-sims[i])[:k] if recs[j]["label"] == r["label"])
        frac = same / k
        agree += frac
        counted += 1
        per_class[r["label"]][0] += frac
        per_class[r["label"]][1] += 1
    link_quality = (agree / counted) if counted else 0.0
    per_class_out = {c: round(v[0] / v[1], 3) for c, v in sorted(per_class.items())}
    graph = {"method": method, "k": k, "n": n,
             "nodes": [{"id": r["id"], "label": r["label"]} for r in recs],
             "neighbours": {recs[i]["id"]: neighbours[i] for i in range(n)},
             "edges": edges}
    score = {"link_quality": round(link_quality, 4), "labelled": counted, "n": n, "k": k,
             "method": method, "per_class": per_class_out}
    return graph, score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out-graph", default="/workspace/rl/orchestrator/bb_semantic_graph.json")
    ap.add_argument("--out-score", default="/workspace/rl/orchestrator/bb_link_scores.json")
    a = ap.parse_args()

    recs = load_corpus(a.corpus)
    print(f"[bb_link] loaded {len(recs)} reports "
          f"({sum(1 for r in recs if r['label'])} labelled)", flush=True)
    graph, score = build_graph(recs, a.k)

    os.makedirs(os.path.dirname(a.out_graph), exist_ok=True)
    json.dump(graph, open(a.out_graph, "w"), indent=2)
    try:
        hist = json.load(open(a.out_score))
    except Exception:
        hist = []
    import datetime
    hist.append({"ts": datetime.datetime.now().isoformat(timespec="seconds"), **score})
    json.dump(hist, open(a.out_score, "w"), indent=2)

    print(f"[bb_link] link_quality@{a.k} = {score['link_quality']} "
          f"(neighbour label agreement over {score['labelled']} labelled reports, method={score['method']})",
          flush=True)
    print(f"[bb_link] wrote graph -> {a.out_graph} ({len(graph['edges'])} edges)", flush=True)
    top = sorted(score["per_class"].items(), key=lambda kv: -kv[1])[:5]
    print(f"[bb_link] best-linked classes: {top}", flush=True)


if __name__ == "__main__":
    main()
