# MiniLM CPU/GPU evaluation (2026-10-06/07)

This directory contains reproducible CPU and isolated Kubernetes GPU
benchmarks for `sentence-transformers/all-MiniLM-L6-v2` and
`cross-encoder/ms-marco-MiniLM-L-6-v2`. It generates deterministic synthetic
inputs and stores aggregate timing/correctness data only; generated text is not
written to artifacts.

## Reproduce the CPU run

```sh
python3.11 -m venv /tmp/minilm-cpu-venv
/tmp/minilm-cpu-venv/bin/pip install --upgrade pip
/tmp/minilm-cpu-venv/bin/pip install -r docs/gpu-evaluation-2026-10-06/requirements.txt
/tmp/minilm-cpu-venv/bin/python docs/gpu-evaluation-2026-10-06/benchmark_cpu.py \
  --output docs/gpu-evaluation-2026-10-06/results.json
```

## Reproduce the GPU run

`benchmark_gpu.py` is run in an isolated pod using
`docker.io/pytorch/pytorch:2.1.2-cuda11.8-cudnn8-runtime`, with the NVIDIA
RuntimeClass and one `nvidia.com/gpu` resource. The pod installs:

```text
sentence-transformers==2.6.1
transformers==4.38.2
numpy==1.26.4
psutil==7.1.0
```

The CUDA stack was selected because the K2200 is Maxwell sm_50; the current
PyTorch 2.9 CPU environment was not assumed to support this GPU. The benchmark
uses FP32 and explicit `torch.cuda.synchronize()` around timed regions. It
records model-only and end-to-end timing separately, startup, CUDA identity,
output finiteness, dimensions, and peak allocated VRAM.

## Results and decision

- `RESULTS.md`: CPU baseline.
- `RESULTS-GPU.md`: measured comparison and recommendation evidence.
- `results.json`, `results.csv`: CPU raw aggregates.
- `results-gpu.json`, `results-gpu.csv`: GPU raw aggregates.
- `http-results.json`: isolated internal HTTP worker correctness and latency.
- `GPU-STATUS.md`: cluster/runtime/device-plugin status and resume history.
- `benchmark_cpu.py`, `benchmark_gpu.py`: benchmark scripts.
- `gpu_http_worker.py`: isolated HTTP smoke worker, not a gateway adapter.

No gateway routing or production deployment was changed. Temporary Kubernetes
benchmark pods and ConfigMaps should be deleted after review.
