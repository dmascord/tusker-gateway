# Observed CPU run

Executed on the local Apple arm64 host with Python 3.11.16, torch 2.9.0, sentence-transformers 5.1.2, transformers 4.57.1, and 4 torch threads (interop 1). Both pinned model revisions loaded successfully. Startup for both models was 10,251.4 ms; peak RSS was 648.2 MiB. The embedding output dimension was 384 and every embedding/reranker output was finite.

The run used 3 warmups and 20 timed iterations per case, producing 21 aggregate cases (9 embedding and 12 reranking). Timing distributions, including median and p95 for tokenization, model-only, and end-to-end phases, are in `results.json` and flattened in `results.csv`. As an example of the largest reranking case (100 candidates, 64-word pairs, inference batch 8), model-only median/p95 were 522.53/562.60 ms and end-to-end median/p95 were 552.26/590.70 ms.

No GPU result is claimed. ONNX Runtime was not attempted because the torch + sentence-transformers path completed without custom compilation; the README records this choice. The benchmark's first attempt encountered a transient Hugging Face read timeout and a fixed output-check bug; the completed run after the fix is the artifact cited above, with `errors: []`.
