#!/bin/sh
# Frame-side supervisor: restart usbip_frame.py whenever it exits. The server enumerates USB
# itself, so a dongle that re-enumerates at a new busid needs no restart.
set -u
cd "$(dirname "$0")"
log() { echo "$(date +%H:%M:%S) supervise: $*" >> server.log; }
while true; do
    log "starting server"
    ~/.pixi/bin/pixi run python usbip_frame.py "$@" >> server.log 2>&1
    log "server exited ($?); restarting"
    sleep 2
done
