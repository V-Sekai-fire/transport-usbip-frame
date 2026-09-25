#!/bin/sh
# Frame-side supervisor: keep one usbip_frame.py running on whatever busid the dongle is at.
# When the dongle re-enumerates (dock power-cycle) the server exits on device-gone; this
# re-resolves the busid and restarts, so a re-attach from the client always finds a live server.
set -u
VID=${VID:-248a}
cd "$(dirname "$0")"
log() { echo "$(date +%H:%M:%S) supervise: $*" >> server.log; }
while true; do
    B=""
    for d in /sys/bus/usb/devices/*; do
        [ -f "$d/idVendor" ] && [ "$(cat "$d/idVendor")" = "$VID" ] && B=$(basename "$d") && break
    done
    if [ -z "$B" ]; then
        sleep 2; continue
    fi
    log "starting server on busid $B"
    ~/.pixi/bin/pixi run python usbip_frame.py --busid "$B" >> server.log 2>&1
    log "server exited; waiting for the dongle to settle"
    sleep 2
done
