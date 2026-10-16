# Frigate video decode benchmark

Date: 2026-10-07

## Scope

This is an isolated FFmpeg benchmark. It did not change the live Frigate
container, camera configuration, or gateway routing.

The live Frigate deployment is external to Kubernetes on host `wyzard`
(`10.0.0.218`), image `ghcr.io/blakeblackshear/frigate:0.17.1`. Its current
configuration uses `preset-vaapi`, and the configured active camera inputs
include H.265 paths. The K2200 test host is the separate Kubernetes worker
`warden` (`10.0.0.112`).

The synthetic input is a reproducible 20-second, 1280x720, 15 fps, H.264
YUV420P MP4 generated with FFmpeg/libx264. SHA-256:

```text
41fa9645578ac70368673f2a42466888d4c6052db28a80b906f8650e45db64ef
```

It contains 300 frames. The test image was
`docker.io/jrottenberg/ffmpeg:8-nvidia`, which exposes both `h264_cuvid` and
CUDA hardware acceleration. Software and NVDEC runs used the same input,
output (`-f null -`), resolution, frame count, and FFmpeg build.

## Measured runs

| Path | FFmpeg options | Frames | Wall time | Effective rate | FFmpeg max RSS |
|---|---|---:|---:|---:|---:|
| CPU software decode | `-i input -f null -` | 300 | 0.175 s | 1,714 fps | 56 MiB |
| K2200 NVDEC, frames retained on GPU | `-hwaccel cuda -hwaccel_output_format cuda` | 300 | 0.509 s | 589 fps | 113 MiB |
| K2200 NVDEC, frames downloaded to system memory | plus `-vf hwdownload,format=nv12` | 300 | 0.510 s | 588 fps | 116 MiB |

The CPU and GPU numbers above are representative completed runs. Additional
single-run measurements varied because the very short synthetic workload is
sensitive to startup and scheduling overhead:

- CPU user/system time: `0.417/0.035 s`; wall time `0.175 s` in one run.
- NVDEC user/system time: `0.184/0.040 s`; wall time `0.509 s` in one run.
- An earlier CPU run measured `0.461/0.006 s`, wall time `0.180 s`.
- An earlier NVDEC run measured `0.121/0.086 s`, wall time `0.761 s`.

All completed decode runs emitted 300 frames and no FFmpeg errors. CUDA
initialization was proven separately: without the NVIDIA runtime injection,
FFmpeg failed with `Cannot load libcuda.so.1`; with
`/usr/bin/nvidia-container-runtime`, NVDEC completed and the output stream was
reported as `cuda`.

## Resource evidence

At idle after the benchmark:

```text
Quadro K2200, GPU utilization 0%, memory 2 MiB, power 1.53 W, temperature 43 C
```

The benchmark was too short for a reliable sampled peak telemetry series; the
measured wall-time result is still valid for this workload, but GPU utilization
and power should be collected over a sustained real camera stream before any
capacity claim.

## Interpretation

For this 1280x720 H.264 file, NVDEC was approximately 2.9x slower in wall-clock
throughput than the same FFmpeg process using software decode. That is not a
contradiction: the host has four logical CPUs, and this short file has substantial
process/container/GPU initialization overhead. NVDEC reduced FFmpeg user CPU
time in the representative run, but it increased total latency and memory use.

This benchmark therefore does **not** justify moving Frigate H.264 decode to
the K2200 for performance. It may still be useful if CPU saturation is the
primary constraint and real camera streams are long-lived, concurrent, and
numerous enough to amortize GPU initialization. That requires a sustained
multi-stream test using actual camera profiles.

The live Frigate inputs include H.265 paths. The K2200 is not suitable for
those: NVIDIA's codec matrix identifies this first-generation Maxwell card as
supporting H.264 decode but not HEVC/H.265 decode. Do not replace the current
VAAPI path with `preset-nvidia` for those cameras.

## Recommendation

- **H.264 decode:** retain the current path for now. A K2200 NVDEC pilot is
  only worth pursuing to reduce CPU load under measured multi-camera
  saturation, not to increase single-stream throughput.
- **H.265 decode:** do not use the K2200; it lacks the required hardware decode
  support.
- **Frigate detection:** this benchmark covers video decode only. It provides
  no evidence for TensorRT/object-detection deployment on the K2200.
- **Production change:** none recommended from this result. Keep Frigate's
  existing `preset-vaapi` configuration unchanged.

The reproducible runner is `benchmark_frigate_decode.sh`. The K2200 runtime
setup and cleanup status remain in `GPU-STATUS.md`.
