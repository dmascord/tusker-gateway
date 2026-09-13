# Frigate retention tightening — 2026-09-13

## Background

`/var/lib/docker` on wyzard was at 99% used, with 168 GB of recordings in
the `frigate_storage-volume` (almost the entire 172 GiB used on the docker
LV). Frigate was set up with `motion.days: 14` but the cameras had
`roles: [record]` on their second H265 inputs, which forced continuous
recording regardless of motion. At ~30 GB/day/camera × 3 cameras × 4 days,
this had filled the docker LV and threatened future image pulls.

## Diagnosis

- `frigate_storage-volume/_data` was 168 GB, almost all under
  `recordings/`.
- Per-camera config had:
  ```yaml
  camera_dome_1:
      ffmpeg:
        inputs:
          - path: rtsp://...ch1/sub/av_stream
            roles: [detect]
          - path: rtsp://...ch2/main/av_stream    # H265 main stream
            roles: [record]                        # forces continuous
  ```
- `record.continuous.days: 0` was set globally, but the per-camera
  `roles: [record]` on the main H265 stream overrode that — frigate
  recorded every second of every camera, split into ~331 10-second
  segments per camera per hour.
- Daily growth: 90 GB. With 14-day retention the steady-state was 1.2 TB.

## Fix

Three changes to `/home/tusker/docker/frigate/config/config.yml` on
wyzard:

1. **Remove the second H265 main-stream input** from each of the 3 main
   cameras. The sub-stream is sufficient for detection at 3-4 fps; the
   main H265 stream was only used for recording.
2. **Reduce `motion.days`, `alerts.retain.days`, and `detections.retain.days`
   from 14 to 7.**
3. **Add `record.enabled: false` per camera** to override the global
   `record.enabled: true` (which had been causing frigate to auto-infer
   a `record` role on the remaining sub-stream input).

A backup of the original config was saved to
`config.yml.bak.20260913_disable_continuous` in the same directory.

After editing, the container was restarted (`docker restart frigate`) so
the config reload would take effect — frigate does not hot-reload role
changes reliably, only retention values.

## Verification

After the restart:

- **`/tmp/cache/` (frigate's continuous-segment staging area) is empty**
  for the 3 main cameras. The cache is only populated when an ffmpeg
  process is doing `-f segment` writes — no such process exists now for
  the main cameras.
- **Recording activity dropped from continuous to motion-only.**
  Per-camera motion file count over 5 min:
  - camera_carport: ~15-18 files
  - camera_dome_1: ~31-35 files
  - camera_dome_128: ~15-17 files
  - yard_doorbell: ~23-26 files (pre-existing config, not touched)

  (Compare to before: ~3,300 files per 5 min per camera when continuous.)
- **`record.<camera>.enabled: false`** confirmed by frigate's API.
- **Frigate started in normal mode** (not safe mode). No validation errors.
- **Docker LV usage: stable at 169 GB used, 8.7 GB free.** No longer
  growing.
- **Existing 145 GB of pre-cutover recordings (2026-09-09 to 2026-09-12)
  remain on disk** and will roll off naturally after 7 days (2026-09-13 + 7
  = 2026-09-20 onwards). The `motion.days: 7` retention sweep is active
  and will keep the steady-state recordings dir to roughly 1-3 GB per
  camera (residential motion only).
- **Frigate's "Less than 1 hour of recording space left" trigger has
  fired** and proactively cleaned up 3.27 GB in a single sweep on
  2026-09-13 19:05:36 AEST. This sweep ran automatically before the
  container restart.

## Out of scope

- **yard_doorbell** was not changed. Its config has pre-existing issues
  (duplicate `roles: [detect, detect]`) that date back to before this
  session. The `record.enabled` flag was left as-is. It currently records
  continuously at low rate (the rtsp source is a sub-stream preview), but
  its retention behavior is left intact.
- **front_doorbell** is `enabled: false` in the config (a separate state).
- **Old pre-cutover recordings** were not deleted manually; they will
  expire on the rolling 7-day window. Per operator direction (see session
  log), no destructive cleanup was performed beyond the config change.

## Files

- Backup of original config: `/home/tusker/docker/frigate/config/config.yml.bak.20260913_disable_continuous`
- Native backup cron: frigate already does `config.yml.bak.YYYYMMDD-HHMMSS`
  backups on each `docker compose run`, so the rollback path is well
  preserved.
