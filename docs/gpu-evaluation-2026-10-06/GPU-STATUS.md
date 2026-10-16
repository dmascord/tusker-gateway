# Quadro K2200 evaluation status

Date: 2026-10-07

## Live cluster access

Use the working path when direct private-node access is unavailable:

```sh
ssh -p 8022 [REDACTED-EMAIL]
```

The repository deployment documentation records this fallback in `docs/deployment-k8s.md`.

## Selected host

`warden` is the selected test host. It is a Kubernetes worker, not a control-plane node.

- Debian GNU/Linux 13 (trixie)
- Kernel `6.12.111+deb13-amd64`
- Current IP `10.0.0.112` (changed from `10.0.0.113` after reboot)
- 4 logical CPUs
- 15 GiB RAM
- K2200 PCI ID `10de:13ba`

## Completed host and Kubernetes setup

1. Node was cordoned and drained successfully.
2. Debian APT sources were corrected to include `contrib` and `non-free`; the previous file is backed up as `/etc/apt/sources.list.gpu-backup`.
3. Proprietary Debian NVIDIA driver installed: `nvidia-driver`, `nvidia-kernel-dkms`, and `nvidia-smi` version `550.163.01-2`; DKMS is `3.2.2-1~deb13u1`.
4. Nouveau was blacklisted in `/etc/modprobe.d/blacklist-nouveau.conf` and initramfs rebuilt.
5. `warden` was rebooted and returned Ready.
6. NVIDIA Container Toolkit packages version `1.20.1-1` were installed.
7. Named containerd runtime added in `/etc/containerd/conf.d/90-nvidia.toml`, using `/usr/bin/nvidia-container-runtime`; `runc` remains the default.
8. Node-constrained `nvidia-device-plugin-warden` DaemonSet installed in `kube-system`, using NVIDIA device-plugin `v0.17.1` and RuntimeClass `nvidia`.

No production gateway route or deployment manifest was changed.

## Verified runtime state

`nvidia-smi` reports:

```text
GPU: Quadro K2200
PCI ID: 10de:13ba
VRAM: 4096 MiB
Driver: 550.163.01
Reported CUDA: 12.4
Compute capability: 5.0
Idle power: approximately 1.43 W
Power limit: 39.50 W
```

A direct `ctr` CUDA smoke test succeeded with `nvidia/cuda:11.8.0-base-ubuntu22.04` and printed the GPU from inside a container. Kubernetes reports one healthy resource:

```text
capacity:   nvidia.com/gpu = 1
allocatable: nvidia.com/gpu = 1
resource ID: GPU-a5d26692-e9b7-c5c8-6509-4b9ca0bbfe47
```

A Kubernetes pod requesting one GPU succeeded. A BusyBox probe confirmed `/dev/nvidia0`, `/dev/nvidiactl`, `/dev/nvidia-uvm`, and `/dev/nvidia-modeset` injection.

## Benchmark state

Complete artifacts:

- `benchmark_cpu.py`, `results.json`, `results.csv`, `RESULTS.md`
- `benchmark_gpu.py`, `results-gpu.json`, `results-gpu.csv`, `RESULTS-GPU.md`
- `gpu_http_worker.py`, `http-results.json`
- `benchmark_frigate_decode.sh`, `FRIGATE-DECODE-RESULTS.md`

The GPU run used the same pinned model revisions and deterministic seed as CPU, FP32, explicit CUDA synchronization, three warmups, and 20 timed iterations. It proved CUDA execution, output finiteness, CPU/GPU numerical agreement, and isolated internal HTTP behavior.

Measured GPU model startup was 16,028.9 ms. Peak allocated VRAM was approximately 183–470 MiB for embedding cases and 183–209 MiB for reranking. During the run, `nvidia-smi` observed approximately 94–100% utilization, 618–620 MiB total reported memory, and 28–35 W board power. These are partial GPU telemetry, not whole-system electricity measurements.

The isolated Frigate decode benchmark used the same FFmpeg input and output for CPU and NVDEC. For a 20-second 1280x720 H.264 file, CPU software decode completed in 0.175 s (1,714 fps) versus 0.509 s (589 fps) for K2200 NVDEC. Both paths decoded all 300 frames without errors. NVDEC lowered FFmpeg user CPU time but increased wall latency and memory use for this short stream. The input SHA-256 and limitations are recorded in `FRIGATE-DECODE-RESULTS.md`.

## Known operational issue

The first Kubernetes CUDA `nvidia-smi` pod was killed by a node SystemOOM while starting `nvidia-smi`; Kubernetes recorded `nvidia-smi` as the victim. Warden recovered to Ready, and the later model benchmark and HTTP smoke completed successfully. Retain this as a stability risk.

## Recommendation

See `RESULTS-GPU.md`: do not deploy these K2200s for interactive embedding or reranking gateway traffic. The measured GPU is faster only for some small embedding batches and effectively tied or slower for representative reranking shapes. Background-only use is not justified without a separate larger-batch workload study.
