
## Recommendation

### Embeddings: not worth deploying for interactive gateway traffic

The K2200 is only faster for batch-1 and some batch-8 short/long inputs. It is
slower for every batch-32 case and for batch 8 at 128 and 256 words. The
practical worker-pod CPU baseline is already competitive, avoids GPU runtime
complexity, and has no single-GPU scheduling bottleneck. Keep embeddings on the
existing CPU/provider path. The GPU could be useful only for a deliberately
batch-oriented background workload after measuring a larger sustained queue;
that is not enough evidence to justify gateway deployment.

### Reranking: not worth deploying for interactive gateway traffic

The GPU is effectively tied at the smallest measured 10-candidate, 64-word,
batch-8 case and is 8–9% slower for 50 and 100 candidates. Batch 8 is clearly
better than batch 1, but not enough to beat the four-core CPU baseline at the
representative longer pairs. Keep reranking on CPU or an existing provider.
The K2200 is not recommended even as a background reranking accelerator unless
future workloads have much larger batches and tolerate the extra node/runtime
failure surface.

### Four-card estimate

A naive upper-bound estimate from four independent cards would multiply measured
single-card throughput by four, but the observed GPU does not beat CPU on the
representative gateway shapes, and scaling is not established. Therefore no
four-card deployment capacity is recommended or claimed. VRAM is not pooled;
each card has its own approximately 4 GiB address space.
