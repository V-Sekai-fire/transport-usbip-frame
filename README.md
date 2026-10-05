# transport-usbip-frame

A userspace USB/IP server that exports one USB device from a standalone headset to another host and logs its traffic as Parquet.

## What it is for

It lets a host use a device plugged into the headset with no kernel module and no root on the headset, and records every transfer as normalised relations for later reading. A supervisor on each end restarts the server and re-attaches the client when the device re-enumerates.

## Build and run

```sh
pixi run serve
```

`pixi run serve --help` lists the device and logging options.

## Licence

MIT; see `LICENSE`.
