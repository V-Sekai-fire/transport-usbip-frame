# usbip-frame

A userspace USB/IP server over libusb that exports one USB device from a Linux headset to a remote host, logging traffic as zstd Parquet.

    pixi run serve --vid 248a --pid 8002 --busid 1-1.4

No `usbip-host` kernel module and no root: the device's usbfs node only needs to be readable
by the user. A client attaches it with `usbip attach -r <host> -b <busid>` and binds its own
class driver.

Traffic lands under `log/session_id=<ns>/<relation>/part-NNNNNN.parquet` in the relations
listed at the top of `etnf_log.py`. `pixi run migrate` rewrites sessions from the first
layout (`session=<ns>/`).

## Self-healing

The Frame's USB dock re-enumerates on power events, which drops the dongle and leaves the
client's attach stale. Two supervisors keep the path up without hand-holding:

- `supervise.sh` (Frame) restarts the server on the live busid whenever it exits on device-gone.
- `attach_watchdog.ps1` (Windows) re-attaches whenever the COM device is missing or not `OK`,
  with backoff so a Frame-down window does not thrash.

      sh supervise.sh &                                   # on the Frame, in ~/usbip
      powershell -File attach_watchdog.ps1                # on Windows
