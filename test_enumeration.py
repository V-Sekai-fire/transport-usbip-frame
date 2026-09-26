"""Enumeration against a fake sysfs tree, with the negative controls it must reject."""
import os
import struct
import tempfile

import usbip_frame as uf


def dev(root, busid, vid, pid, cls="00", ifaces=((3, 0, 0),), speed="12"):
    d = os.path.join(root, busid)
    os.makedirs(d)
    for k, v in dict(idVendor=vid, idProduct=pid, bDeviceClass=cls, bDeviceSubClass="00", bDeviceProtocol="00",
                     bcdDevice="0100", busnum="1", devnum="3", speed=speed, bConfigurationValue="1",
                     bNumConfigurations="1").items():
        open(os.path.join(d, k), "w").write(v + "\n")
    for n, (c, s, p) in enumerate(ifaces):
        i = os.path.join(root, "%s:1.%d" % (busid, n))
        os.makedirs(i)
        for k, v in dict(bInterfaceClass=c, bInterfaceSubClass=s, bInterfaceProtocol=p).items():
            open(os.path.join(i, k), "w").write("%02x\n" % v)
        os.symlink(i, os.path.join(d, "%s:1.%d" % (busid, n)))


def main():
    root = tempfile.mkdtemp()
    dev(root, "1-1", "1d5c", "5801", cls="09")
    dev(root, "1-1.3", "0bb4", "0350")
    dev(root, "1-1.4", "248a", "8002", ifaces=((2, 2, 0), (10, 0, 0)))
    dev(root, "usb1", "1d6b", "0002", cls="09")
    os.makedirs(os.path.join(root, "1-1.9"))  # half-torn-down node with no attributes
    checks = {
        "lists the dongles": uf.exportable(set(), root) == ["1-1.3", "1-1.4"],
        "hub is not exported": "1-1" not in uf.exportable(set(), root),
        "root hub is not exported": "usb1" not in uf.exportable(set(), root),
        "interface nodes are not devices": not any(":" in b for b in uf.exportable(set(), root)),
        "exclude by vid:pid": uf.exportable({"248a:8002"}, root) == ["1-1.3"],
        "exclude by busid": uf.exportable({"1-1.3"}, root) == ["1-1.4"],
        "interfaces read in order": uf.interfaces("1-1.4", root) == [(2, 2, 0), (10, 0, 0)],
    }
    rec = uf.record("1-1.3", root)
    busnum, devnum, speed, vid, pid = struct.unpack(">IIIHH", rec[288:304])
    checks["record carries vid:pid"] = (vid, pid) == (0x0bb4, 0x0350)
    checks["record speed is full"] = speed == 2
    checks["record is 312 bytes"] = len(rec) == 312
    checks["record busid field"] = rec[256:288].rstrip(b"\0") == b"1-1.3"
    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print("PASS" if v else "FAIL", k)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
