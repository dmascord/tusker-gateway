#!/usr/bin/env bash
# USB-flap monitor for visor.
#
# Background
# ----------
# On 2026-08-26 the Samsung Portable SSD T5 attached to visor's USB bus
# flapped three times in four seconds — almost certainly from a physical
# bump of the cable/drive (the drive is otherwise stable when left alone).
# ext4 aborted its journal on both LVs (containerd-data, longhorn-ssd),
# the kernel remounted them read-only, containerd crashed, kubelet went
# NotReady, and the only Longhorn replica for tusker-home became
# unattachable. Every /v1/chat/completions request returned 500 for ~1
# hour until we rebooted visor.
#
# Post-incident the runtime state was migrated off the USB drive (see
# docs/incidents/2026-08-26-usb-ssd-flap.md), so even another flap won't
# take down the gateway. This script is now a **smoke detector** — it
# catches disturbances early so an operator can verify the node is still
# healthy (kubelet reporting Ready, pods running) without waiting for a
# user-visible 500.
#
# What it does
# ------------
# - Runs every minute (via systemd timer; see the unit files in this dir).
# - Greps the last 60 s of dmesg for "USB disconnect" / "USB ... new ...
#   device" / "rejected I/O to offline device" entries.
# - If a flap is observed in the window, prints
#   'ALERT usb-flap-detected ...' on stdout and exits 0. The caller (the
#   systemd unit) is expected to forward stdout to a notification channel
#   (e.g. a cron job piped into a webhook).
#
# This script is INTENTIONALLY dependency-free: it uses only bash, dmesg,
# awk, grep. It runs on visor only.
#
# Exit codes
# ----------
# 0 — flap detected (alert raised) OR no flap (silent success)
# 2 — dmesg buffer unavailable (logs warning, silent otherwise)
#
# Install (manual, on visor):
#   1. Copy this script to /usr/local/bin/usb-flap-monitor.sh
#   2. Copy the systemd timer + service from this directory:
#        /etc/systemd/system/usb-flap-monitor.service
#        /etc/systemd/system/usb-flap-monitor.timer
#   3. systemctl daemon-reload
#   4. systemctl enable --now usb-flap-monitor.timer
#
# Tuning
# ------
# The drive is currently unused for critical state — see follow-ups in
# docs/incidents/2026-08-26-usb-ssd-flap.md. Once it's physically
# unplugged, this monitor is no longer useful and can be removed with:
#   systemctl disable --now usb-flap-monitor.timer
#   rm /etc/systemd/system/usb-flap-monitor.{service,timer}
set -euo pipefail

# Window to look back: 1 minute. systemd timer fires every minute so this
# gives a fresh window each tick with one minute of overlap.
WINDOW_SECONDS=60

# Recognised flap patterns. We match on the USB bus 1 family because that's
# where visor's USB Samsung T5 attaches (visible from `lsusb`).
#
# "USB disconnect, device number N" — kernel saw the cable unplug
# "new high-speed USB device" — kernel saw the cable reconnect
# "rejected I/O to offline device" — too late, the damage is done
EVENT_REGEX='(USB disconnect, device number|new high-speed USB device number|rejected I/O to offline device)'

# dmesg output since the boot has form "[  123.456] message". The kernel
# stores elapsed-since-boot timestamps, so we can read current uptime and
# filter lines newer than (uptime - window).
if ! command -v dmesg >/dev/null 2>&1; then
    echo "WARN: dmesg not available" >&2
    exit 2
fi

NOW_UP=$(awk '{print $1}' /proc/uptime)
THRESHOLD=$(awk -v now="$NOW_UP" -v win="$WINDOW_SECONDS" 'BEGIN { printf "%.3f", now - win }')

# dmesg with -T is human readable but slow; without -T we get raw [sec] form
# which we can filter. dmesg --since="60 sec ago" works on systemd-journald-backed
# systems; we fall back to manual timestamp filter if --since is unsupported.
RECENT=$(dmesg --since="-${WINDOW_SECONDS} sec" 2>/dev/null || true)

if [[ -z "${RECENT:-}" ]]; then
    # Fallback: read the full ring buffer and filter by elapsed timestamp.
    # dmesg output is "[  123.456] message" — awk extracts 1st field, strips [ ].
    RECENT=$(dmesg | awk -v thr="$THRESHOLD" '
        match($0, /^\[[ \t]*([0-9]+\.[0-9]+)\]/) {
            ts = substr($0, RSTART, RLENGTH); gsub(/[\[\]]/, "", ts); ts += 0;
            if (ts+0 >= thr+0) print $0
        }')
fi

# Count flap events. Excluding "new high-speed" cuts duplicate noise (each
# disconnect is followed by a re-attach; we only need to see both for a
# real flap). We require at least one disconnect OR a rejected I/O.
if [[ -z "$RECENT" ]]; then
    exit 0
fi

DISCONNECTS=$(echo "$RECENT" | grep -E 'USB disconnect, device number' | wc -l || true)
RECONNECTS=$(echo "$RECENT" | grep -E 'new high-speed USB device number' | wc -l || true)
IO_REJECTS=$(echo "$RECENT" | grep -E 'rejected I/O to offline device' | wc -l || true)

# Alert if we saw at least one disconnect in the window OR any I/O rejection.
# (A single disconnect + reconnect is a flap. A lone I/O reject means the
# disk is already gone and we need an operator now.)
if (( DISCONNECTS > 0 || IO_REJECTS > 0 )); then
    printf 'ALERT usb-flap-detected disconnects=%d reconnects=%d io_rejects=%d window=%ds\n' \
        "$DISCONNECTS" "$RECONNECTS" "$IO_REJECTS" "$WINDOW_SECONDS"
    echo "Recent dmesg entries:"
    echo "$RECENT" | grep -E "$EVENT_REGEX" | tail -10
    exit 0
fi

# Silent success: no flap in the last minute.
exit 0
