# usbip-frame

A userspace USB/IP server over libusb that exports one USB device from a Linux headset to a remote host, logging traffic as zstd Parquet.

    pixi run serve --vid 248a --pid 8002 --busid 1-1.4

No `usbip-host` kernel module and no root: the device's usbfs node only needs to be readable
by the user. A client attaches it with `usbip attach -r <host> -b <busid>` and binds its own
class driver.

Traffic lands under `log/session_id=<ns>/<relation>/part-NNNNNN.parquet` in the relations
listed at the top of `etnf_log.py`. `pixi run migrate` rewrites sessions from the first
layout (`session=<ns>/`).
