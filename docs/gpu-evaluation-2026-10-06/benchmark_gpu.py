#!/usr/bin/env python3
"""Reproducible FP32 CUDA benchmark matching benchmark_cpu.py semantics."""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import random
import statistics
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
    index = (len(values) - 1) * p / 100
    low, high = int(index), min(int(index) + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (index - low)


def stats(samples: list[float]) -> dict[str, float | int]:
    return {"iterations": len(samples), "median_ms": statistics.median(samples) * 1000,
            "p95_ms": percentile(samples, 95) * 1000, "min_ms": min(samples) * 1000,
            "max_ms": max(samples) * 1000}


def generated_texts(count: int, words: int, seed: int = SEED) -> list[str]:
    rng = random.Random(seed + count + words)
    vocabulary = ["amber", "vector", "harbor", "cobalt", "signal", "meadow", "kernel",
                  "steady", "orbit", "parcel", "frost", "maple", "raster", "circuit",
                  "lattice", "quiet", "granite", "thread", "silver", "archive", "north",
                  "canvas", "juniper", "metric", "tuning", "stable", "query", "window"]
    return [" ".join(rng.choice(vocabulary) for _ in range(words)) for _ in range(count)]


def timed(fn, torch, warmups: int, iterations: int) -> dict[str, float | int]:
    for _ in range(warmups):
        fn()
    torch.cuda.synchronize()
    samples = []
    for _ in range(iterations):
        torch.cuda.synchronize()
        start = time.perf_counter()
        fn()
        torch.cuda.synchronize()
        samples.append(time.perf_counter() - start)
    return stats(samples)


def package_versions() -> dict[str, str]:
    import importlib.metadata
    result = {}
    for name in ("torch", "sentence-transformers", "transformers", "tokenizers", "numpy"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = "unavailable"
    return result


def finite(value, torch) -> bool:
    if not isinstance(value, torch.Tensor):
        value = torch.as_tensor(value)
    return bool(torch.isfinite(value).all().item())


def run_embedding(model, tokenizer, cases, torch, device, warmups, iterations):
    results = []
    with torch.inference_mode():
        for batch_size, length in cases:
            texts = generated_texts(batch_size, length)
            def tokenize():
                return tokenizer(texts, padding=True, truncation=True, max_length=length, return_tensors="pt").to(device)
            tokenized = tokenize()
            def model_only():
                return model(tokenized)
            def end_to_end():
                return model.encode(texts, batch_size=batch_size, convert_to_tensor=True,
                                    normalize_embeddings=True, show_progress_bar=False, device=device)
            token_stats = timed(tokenize, torch, warmups, iterations)
            model_stats = timed(model_only, torch, warmups, iterations)
            e2e_stats = timed(end_to_end, torch, warmups, iterations)
            output = end_to_end()
            results.append({"model": EMBEDDING_MODEL, "revision": EMBEDDING_REVISION,
                            "operation": "embedding", "batch_size": batch_size, "length": length,
                            "dimension": int(output.shape[-1]), "finite_output": finite(output, torch),
                            "tokenization": token_stats, "model_only": model_stats,
                            "end_to_end": e2e_stats,
                            "peak_allocated_mb": torch.cuda.max_memory_allocated() / 1048576})
            torch.cuda.reset_peak_memory_stats()
    return results


def run_rerank(model, tokenizer, cases, torch, device, warmups, iterations):
    results = []
    with torch.inference_mode():
        for candidates, words, batch_size in cases:
            query = generated_texts(1, words, SEED + candidates)[0]
            docs = generated_texts(candidates, words, SEED + candidates + 1)
            pairs = [(query, doc) for doc in docs]
            def tokenize():
                return [tokenizer(pairs[start:start + batch_size], padding=True, truncation=True, return_tensors="pt").to(device)
                        for start in range(0, len(pairs), batch_size)]
            tokenized = tokenize()
            def model_only():
                return torch.cat([model(**batch).logits for batch in tokenized])
            def end_to_end():
                return torch.cat([model(**batch).logits for batch in tokenize()])
            token_stats = timed(tokenize, torch, warmups, iterations)
            model_stats = timed(model_only, torch, warmups, iterations)
            e2e_stats = timed(end_to_end, torch, warmups, iterations)
            scores = end_to_end()
            results.append({"model": RERANK_MODEL, "revision": RERANK_REVISION,
                            "operation": "rerank", "candidate_count": candidates,
                            "pair_words": words, "inference_batch_size": batch_size,
                            "finite_output": finite(scores, torch),
                            "tokenization": token_stats, "model_only": model_stats,
                            "end_to_end": e2e_stats, "pairs_per_second_median": candidates / (e2e_stats["median_ms"] / 1000),
                            "peak_allocated_mb": torch.cuda.max_memory_allocated() / 1048576})
            torch.cuda.reset_peak_memory_stats()
    return results


def flatten_rows(results: list[dict[str, Any]]):
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
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=3)
    args = parser.parse_args()
    if args.iterations < 20:
        parser.error("--iterations must be at least 20")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    import torch
    from sentence_transformers import CrossEncoder, SentenceTransformer
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable")
    device = "cuda"
    torch.set_num_threads(min(os.cpu_count() or 1, 4))
    torch.set_num_interop_threads(1)
    started = time.perf_counter()
    embedding = SentenceTransformer(EMBEDDING_MODEL, revision=EMBEDDING_REVISION, device=device)
    reranker = CrossEncoder(RERANK_MODEL, revision=RERANK_REVISION, device=device)
    reranker.model.to(device)
    startup_s = time.perf_counter() - started
    embedding_cases = [(b, l) for l in (32, 128, 256) for b in (1, 8, 32)]
    rerank_cases = [(c, w, b) for c in (10, 50, 100) for w in (16, 64) for b in (1, 8)]
    embedding_results = run_embedding(embedding, embedding.tokenizer, embedding_cases, torch, device, args.warmups, args.iterations)
    rerank_results = run_rerank(reranker.model, reranker.tokenizer, rerank_cases, torch, device, args.warmups, args.iterations)
    payload = {"schema": "minilm-gpu-benchmark/v1", "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "seed": SEED, "python": sys.version, "platform": {"system": platform.platform(), "machine": platform.machine()},
               "versions": package_versions(), "models": {"embedding": {"name": EMBEDDING_MODEL, "revision": EMBEDDING_REVISION},
               "reranker": {"name": RERANK_MODEL, "revision": RERANK_REVISION}},
               "cuda": {"device": torch.cuda.get_device_name(0), "device_index": 0, "capability": list(torch.cuda.get_device_capability(0)),
                        "torch_cuda": torch.version.cuda, "cudnn": torch.backends.cudnn.version()},
               "settings": {"warmups": args.warmups, "iterations": args.iterations, "device": device, "dtype": "float32",
                            "tokenizers_parallelism": os.environ["TOKENIZERS_PARALLELISM"]},
               "startup": {"seconds": startup_s, "milliseconds": startup_s * 1000},
               "summary": {"embedding_dimension": embedding_results[0]["dimension"],
                           "all_outputs_finite": all(x["finite_output"] for x in embedding_results + rerank_results)},
               "results": embedding_results + rerank_results, "errors": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    rows = flatten_rows(payload["results"])
    with args.output.with_suffix(".csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted({key for row in rows for key in row}))
        writer.writeheader(); writer.writerows(rows)
    print(json.dumps({"json": str(args.output), "csv": str(args.output.with_suffix('.csv')), "startup_ms": payload["startup"]["milliseconds"],
                      "cases": len(rows), "all_outputs_finite": payload["summary"]["all_outputs_finite"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
