#!/usr/bin/env python3
"""Reproducible CPU benchmark for MiniLM embedding and cross-encoder models.

Outputs aggregate JSON and CSV only; generated input text is never written.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RERANK_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
SEED = 20261006


def percentile(values: list[float], p: float) -> float:
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    index = (len(values) - 1) * p / 100
    low, high = int(index), min(int(index) + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (index - low)


def stats(samples: list[float]) -> dict[str, float | int]:
    return {
        "iterations": len(samples),
        "median_ms": statistics.median(samples) * 1000,
        "p95_ms": percentile(samples, 95) * 1000,
        "min_ms": min(samples) * 1000,
        "max_ms": max(samples) * 1000,
    }


def timed(fn, warmups: int, iterations: int) -> dict[str, float | int]:
    for _ in range(warmups):
        fn()
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return stats(samples)


def generated_texts(count: int, words: int, seed: int = SEED) -> list[str]:
    rng = random.Random(seed + count + words)
    vocabulary = [
        "amber", "vector", "harbor", "cobalt", "signal", "meadow", "kernel",
        "steady", "orbit", "parcel", "frost", "maple", "raster", "circuit",
        "lattice", "quiet", "granite", "thread", "silver", "archive", "north",
        "canvas", "juniper", "metric", "tuning", "stable", "query", "window",
    ]
    return [" ".join(rng.choice(vocabulary) for _ in range(words)) for _ in range(count)]


def process_rss_mb() -> float | None:
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
    except (ImportError, OSError):
        return None


def package_versions() -> dict[str, str]:
    import importlib.metadata
    names = ["torch", "sentence-transformers", "transformers", "tokenizers", "numpy"]
    result = {}
    for name in names:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = "unavailable"
    return result


def finite_embedding(embeddings) -> bool:
    import torch
    if isinstance(embeddings, torch.Tensor):
        tensor = embeddings
    elif isinstance(embeddings, (list, tuple)) and embeddings and isinstance(embeddings[0], torch.Tensor):
        tensor = torch.stack(list(embeddings))
    else:
        tensor = torch.as_tensor(embeddings)
    return bool(torch.isfinite(tensor).all().item())


def run_embedding(model, tokenizer, cases, warmups, iterations, torch) -> tuple[list[dict], dict]:
    results = []
    with torch.inference_mode():
        for batch_size, length in cases:
            texts = generated_texts(batch_size, length)
            def tokenize():
                return tokenizer(texts, padding=True, truncation=True, max_length=length, return_tensors="pt")
            tokenized = tokenize()
            def model_only():
                return model(tokenized)
            def end_to_end():
                return model.encode(texts, batch_size=batch_size, convert_to_numpy=False,
                                    normalize_embeddings=True, show_progress_bar=False)
            token_stats = timed(tokenize, warmups, iterations)
            model_stats = timed(model_only, warmups, iterations)
            e2e_stats = timed(end_to_end, warmups, iterations)
            with torch.inference_mode():
                output = end_to_end()
            vector = output[0] if hasattr(output, "__getitem__") else output
            results.append({
                "model": EMBEDDING_MODEL, "revision": EMBEDDING_REVISION,
                "operation": "embedding", "batch_size": batch_size, "length": length,
                "dimension": int(vector.shape[-1]), "finite_output": finite_embedding(output),
                "tokenization": token_stats, "model_only": model_stats,
                "end_to_end": e2e_stats,
            })
    return results, {"dimension": results[0]["dimension"], "finite_output": all(r["finite_output"] for r in results)}


def run_rerank(model, tokenizer, cases, warmups, iterations, torch) -> list[dict]:
    results = []
    with torch.inference_mode():
        for candidates, words, batch_size in cases:
            queries = generated_texts(1, words, SEED + candidates)
            docs = generated_texts(candidates, words, SEED + candidates + 1)
            pairs = [(queries[0], doc) for doc in docs]
            def tokenize():
                return [tokenizer(pairs[start:start + batch_size], padding=True, truncation=True, return_tensors="pt")
                        for start in range(0, len(pairs), batch_size)]
            tokenized = tokenize()
            def model_only():
                return torch.cat([model(**batch).logits for batch in tokenized])
            def end_to_end():
                return torch.cat([model(**batch).logits for batch in tokenize()])
            token_stats = timed(tokenize, warmups, iterations)
            model_stats = timed(model_only, warmups, iterations)
            e2e_stats = timed(end_to_end, warmups, iterations)
            with torch.inference_mode():
                scores = end_to_end()
            results.append({
                "model": RERANK_MODEL, "revision": RERANK_REVISION,
                "operation": "rerank", "candidate_count": candidates,
                "pair_words": words, "inference_batch_size": batch_size,
                "finite_output": bool(torch.isfinite(scores).all().item()),
                "tokenization": token_stats, "model_only": model_stats,
                "end_to_end": e2e_stats,
            })
    return results


def flatten_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in results:
        row = {k: v for k, v in item.items() if not isinstance(v, dict)}
        for phase in ("tokenization", "model_only", "end_to_end"):
            for key, value in item.get(phase, {}).items():
                row[f"{phase}_{key}"] = value
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("results.json"))
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--threads", type=int, default=min(os.cpu_count() or 1, 4))
    args = parser.parse_args()
    if args.iterations < 20:
        parser.error("--iterations must be at least 20")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    import torch
    from sentence_transformers import CrossEncoder, SentenceTransformer
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    started = time.perf_counter()
    embedding = SentenceTransformer(EMBEDDING_MODEL, revision=EMBEDDING_REVISION, device="cpu")
    reranker = CrossEncoder(RERANK_MODEL, revision=RERANK_REVISION, device="cpu", activation_fn=None)
    startup_s = time.perf_counter() - started
    embedding_model = embedding
    rerank_model = reranker.model
    embedding_tokenizer = embedding.tokenizer
    rerank_tokenizer = reranker.tokenizer
    embedding_cases = [(b, l) for l in (32, 128, 256) for b in (1, 8, 32)]
    rerank_cases = [(c, w, b) for c in (10, 50, 100) for w in (16, 64) for b in (1, 8)]
    embedding_results, embedding_summary = run_embedding(
        embedding_model, embedding_tokenizer, embedding_cases, args.warmups, args.iterations, torch)
    rerank_results = run_rerank(
        rerank_model, rerank_tokenizer, rerank_cases, args.warmups, args.iterations, torch)
    payload = {
        "schema": "minilm-cpu-benchmark/v1",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seed": SEED, "python": sys.version,
        "platform": {"system": platform.platform(), "machine": platform.machine(), "processor": platform.processor()},
        "versions": package_versions(), "models": {
            "embedding": {"name": EMBEDDING_MODEL, "revision": EMBEDDING_REVISION},
            "reranker": {"name": RERANK_MODEL, "revision": RERANK_REVISION},
        },
        "resources": {"cpu_count": os.cpu_count(), "torch_num_threads": torch.get_num_threads(),
                      "torch_num_interop_threads": torch.get_num_interop_threads(), "max_rss_mb": process_rss_mb()},
        "settings": {"warmups": args.warmups, "iterations": args.iterations, "device": "cpu", "dtype": "float32",
                      "tokenizers_parallelism": os.environ["TOKENIZERS_PARALLELISM"]},
        "startup": {"seconds": startup_s, "milliseconds": startup_s * 1000},
        "summary": {"embedding_dimension": embedding_summary["dimension"],
                     "all_outputs_finite": embedding_summary["finite_output"] and all(x["finite_output"] for x in rerank_results)},
        "results": embedding_results + rerank_results,
        "errors": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    csv_path = args.output.with_suffix(".csv")
    rows = flatten_rows(payload["results"])
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted({key for row in rows for key in row}))
        writer.writeheader(); writer.writerows(rows)
    print(json.dumps({"json": str(args.output), "csv": str(csv_path), "startup_ms": payload["startup"]["milliseconds"],
                      "cases": len(rows), "all_outputs_finite": payload["summary"]["all_outputs_finite"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
