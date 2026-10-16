#!/usr/bin/env bash
set -euo pipefail

IMAGE=${IMAGE:-docker.io/jrottenberg/ffmpeg:8-nvidia}
SOURCE=${SOURCE:-/tmp/workdir/frigate-bench-h264-20s.mp4}
OUTDIR=${OUTDIR:-/tmp/frigate-decode-results}
RUNS=${RUNS:-5}
mkdir -p "$OUTDIR"

run_ffmpeg() {
  local mode="$1" run="$2"
  local output="$OUTDIR/${mode}-${run}.log"
  local input_args output_args runtime_args=()
  if [[ "$mode" == cpu ]]; then
    input_args=(-i /workdir/$(basename "$SOURCE"))
    output_args=(-f null -)
  elif [[ "$mode" == nvdec || "$mode" == nvdec-download ]]; then
    runtime_args=(--runc-binary /usr/bin/nvidia-container-runtime)
    input_args=(-hwaccel cuda -hwaccel_output_format cuda -i /workdir/$(basename "$SOURCE"))
    output_args=(-f null -)
    [[ "$mode" == nvdec-download ]] && output_args=(-vf hwdownload,format=nv12 -f null -)
  else
    echo "unknown mode: $mode" >&2
    return 2
  fi
  sudo ctr run --rm "${runtime_args[@]}" \
    --mount "type=bind,src=$(dirname "$SOURCE"),dst=/workdir,options=rbind:ro" \
    "$IMAGE" "frigate-decode-${mode}-${run}" \
    ffmpeg -hide_banner -loglevel info -benchmark -vsync 0 "${input_args[@]}" "${output_args[@]}" \
    >"$output" 2>&1
}

for mode in cpu nvdec nvdec-download; do
  for run in $(seq 1 "$RUNS"); do
    echo "running mode=$mode run=$run"
    run_ffmpeg "$mode" "$run"
  done
done

printf 'mode,run,frames,fps,utime,stime,rtime,maxrss_kb\n' >"$OUTDIR/summary.csv"
for mode in cpu nvdec nvdec-download; do
  for run in $(seq 1 "$RUNS"); do
    log="$OUTDIR/${mode}-${run}.log"
    frames=$(grep -Eo 'frame= *[0-9]+' "$log" | tail -1 | grep -Eo '[0-9]+' || echo 0)
    fps=$(grep -Eo 'fps= *[0-9.]+ ' "$log" | tail -1 | grep -Eo '[0-9.]+' || echo 0)
    bench=$(grep '^bench:' "$log" | tail -1)
    utime=$(printf '%s\n' "$bench" | grep -Eo 'utime=[0-9.]+s' | cut -d= -f2 || echo '')
    stime=$(printf '%s\n' "$bench" | grep -Eo 'stime=[0-9.]+s' | cut -d= -f2 || echo '')
    rtime=$(printf '%s\n' "$bench" | grep -Eo 'rtime=[0-9.]+s' | cut -d= -f2 || echo '')
    maxrss=$(grep -Eo 'maxrss=[0-9]+KiB' "$log" | tail -1 | grep -Eo '[0-9]+' || echo '')
    printf '%s,%s,%s,%s,%s,%s,%s,%s\n' "$mode" "$run" "$frames" "$fps" "$utime" "$stime" "$rtime" "$maxrss" >>"$OUTDIR/summary.csv"
  done
done
cat "$OUTDIR/summary.csv"
