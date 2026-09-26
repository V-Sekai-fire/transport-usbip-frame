# usbip-frame

A userspace USB/IP server over libusb that exports every USB device on a Linux headset to a remote host, logging traffic as zstd Parquet.

    pixi run serve [--exclude <busid|vid:pid>]
    pixi run test

Every non-hub device is enumerated from sysfs on each request, so a device that re-enumerates
at a new busid is listed at the new one. A device is opened only while a client imports it and
gets its kernel driver back on release. No `usbip-host` kernel module and no root: usbfs nodes
only need to be readable by the user. A client attaches with `usbip attach -r <host> -b <busid>`
and binds its own class driver.

Traffic lands under `log/session_id=<ns>/<relation>/part-NNNNNN.parquet` in the relations
listed at the top of `etnf_log.py`. `pixi run migrate` rewrites sessions from the first
layout (`session=<ns>/`).

## Self-healing

The Frame's USB dock re-enumerates on power events, and the Frame moves between the LAN and the
direct link of its own Wi-Fi dongle. Nothing about the path is configured by hand:

- `supervise.sh` (Frame) restarts the server if it exits.
- The server drops a client that vanished without closing, through TCP keepalive (about 25 s).
- `attach_watchdog.ps1` (Windows) finds the Frame at its last good address, the gateway of the
  Valve Wi-Fi adapter, or `-Frame`; attaches everything it exports; and detaches any import
  whose device is not `OK`, with backoff while the Frame is down.

      sh supervise.sh &                                   # on the Frame, in ~/usbip
      powershell -File attach_watchdog.ps1                # on Windows
