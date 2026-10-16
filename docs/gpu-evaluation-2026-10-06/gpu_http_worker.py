#!/usr/bin/env python3
"""Internal-only MiniLM HTTP worker and smoke benchmark for the GPU pod."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

EMBED = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_REV = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
RERANK = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RERANK_REV = "233902d25c440f23af6f7d6e94d2946bac0bee0a"


def make_server(gpu_embed, gpu_rerank, torch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def do_GET(self):
            if self.path == "/health":
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path not in ("/v1/embeddings", "/v1/rerank"):
                self.send_error(404); return
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))))
            with torch.inference_mode():
                if self.path == "/v1/embeddings":
                    texts = body["input"]
                    vectors = gpu_embed.encode(texts, convert_to_numpy=True, normalize_embeddings=True,
                                               show_progress_bar=False, device="cuda").tolist()
                    result = {"object": "list", "data": [{"object": "embedding", "index": i, "embedding": v}
                                                              for i, v in enumerate(vectors)], "model": EMBED}
                else:
                    query = body["query"]
                    docs = body["documents"]
                    scores = gpu_rerank.predict([(query, d) for d in docs], convert_to_numpy=True).tolist()
                    result = {"results": [{"index": i, "relevance_score": s} for i, s in enumerate(scores)], "model": RERANK}
            encoded = json.dumps(result).encode()
            self.send_response(200); self.send_header("content-type", "application/json");
            self.send_header("content-length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
    return ThreadingHTTPServer(("127.0.0.1", 8080), Handler)


def request(path, body):
    encoded = json.dumps(body).encode()
    if path == "/health":
        req = Request("http://127.0.0.1:8080" + path, method="GET")
    else:
        req = Request("http://127.0.0.1:8080" + path, data=encoded,
                      headers={"content-type": "application/json"})
    start = time.perf_counter()
    with urlopen(req, timeout=120) as response:
        payload = json.loads(response.read())
    return payload, (time.perf_counter() - start) * 1000


def main(output: str) -> None:
    import numpy as np
    import torch
    from sentence_transformers import CrossEncoder, SentenceTransformer

    gpu_embed = SentenceTransformer(EMBED, revision=EMBED_REV, device="cuda")
    gpu_rerank = CrossEncoder(RERANK, revision=RERANK_REV, device="cuda")
    gpu_rerank.model.to("cuda")
    cpu_embed = SentenceTransformer(EMBED, revision=EMBED_REV, device="cpu")
    cpu_rerank = CrossEncoder(RERANK, revision=RERANK_REV, device="cpu")
    server = make_server(gpu_embed, gpu_rerank, torch)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    time.sleep(0.1)

    texts = ["amber vector harbor signal", "quiet granite archive north"]
    query = "stable kernel signal"
    docs = ["irrelevant frost parcel", "stable kernel signal archive", "cobalt meadow window"]
    with torch.inference_mode():
        gpu_vec = gpu_embed.encode(texts, convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False)
        cpu_vec = cpu_embed.encode(texts, convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False)
        gpu_scores = gpu_rerank.predict([(query, d) for d in docs], convert_to_numpy=True)
        cpu_scores = cpu_rerank.predict([(query, d) for d in docs], convert_to_numpy=True)
    api_embed, embed_ms = request("/v1/embeddings", {"input": texts})
    api_rerank, rerank_ms = request("/v1/rerank", {"query": query, "documents": docs})
    api_vec = np.asarray([x["embedding"] for x in api_embed["data"]])
    api_scores = np.asarray([x["relevance_score"] for x in api_rerank["results"]])
    payload = {
        "model_revisions": {"embedding": EMBED_REV, "reranker": RERANK_REV},
        "cuda": {"device": torch.cuda.get_device_name(0), "capability": list(torch.cuda.get_device_capability(0))},
        "correctness": {"embedding_dimension": int(gpu_vec.shape[1]),
                        "embedding_finite": bool(np.isfinite(gpu_vec).all()),
                        "embedding_cpu_gpu_min_cosine": float(np.min(np.sum(gpu_vec * cpu_vec, axis=1))),
                        "embedding_api_gpu_min_cosine": float(np.min(np.sum(gpu_vec * api_vec, axis=1))),
                        "rerank_finite": bool(np.isfinite(gpu_scores).all()),
                        "rerank_max_abs_cpu_gpu": float(np.max(np.abs(gpu_scores - cpu_scores))),
                        "rerank_max_abs_direct_api": float(np.max(np.abs(gpu_scores - api_scores))),
                        "rerank_cpu_gpu_same_order": bool(np.array_equal(np.argsort(-gpu_scores), np.argsort(-cpu_scores)))},
        "http": {"health": request("/health", {})[0]["status"],
                 "embedding_requests": 1, "embedding_batch": len(texts), "embedding_ms": embed_ms,
                 "rerank_requests": 1, "rerank_candidates": len(docs), "rerank_ms": rerank_ms},
    }
    Path(output).write_text(json.dumps(payload, indent=2) + "\n")
    server.shutdown()
    print(json.dumps(payload))


if __name__ == "__main__":
    import sys
    main(sys.argv[1])
