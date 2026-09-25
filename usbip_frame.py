"""A userspace USB/IP server for one device, over libusb (no usbip-host kernel module needed).

  python3 usbip_frame.py [--vid 248a --pid 8002] [--port 3240] [--busid 1-1]

Exports the device to a USB/IP client, which attaches it so the client's own class driver
binds it. Implements OP_REQ_DEVLIST, OP_REQ_IMPORT, and URB traffic:
USBIP_CMD_SUBMIT (control, bulk, interrupt) and USBIP_CMD_UNLINK. Isochronous is refused.
"""
import argparse
import os
import collections
import ctypes
import socket
import struct
import sys
import threading
import time

from etnf_log import EtnfLog

L = ctypes.CDLL("libusb-1.0.so.0")
c_void_p, c_int, c_uint8, c_uint16, c_uint = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint


class DevDesc(ctypes.Structure):
    _fields_ = [("bLength", c_uint8), ("bDescriptorType", c_uint8), ("bcdUSB", c_uint16), ("bDeviceClass", c_uint8),
                ("bDeviceSubClass", c_uint8), ("bDeviceProtocol", c_uint8), ("bMaxPacketSize0", c_uint8),
                ("idVendor", c_uint16), ("idProduct", c_uint16), ("bcdDevice", c_uint16), ("iManufacturer", c_uint8),
                ("iProduct", c_uint8), ("iSerialNumber", c_uint8), ("bNumConfigurations", c_uint8)]


L.libusb_init.argtypes = [ctypes.POINTER(c_void_p)]
L.libusb_open_device_with_vid_pid.restype = c_void_p
L.libusb_open_device_with_vid_pid.argtypes = [c_void_p, c_uint16, c_uint16]
L.libusb_get_device.restype = c_void_p
L.libusb_get_device.argtypes = [c_void_p]
L.libusb_get_device_descriptor.argtypes = [c_void_p, ctypes.POINTER(DevDesc)]
for f in ("libusb_get_bus_number", "libusb_get_device_address", "libusb_get_device_speed"):
    getattr(L, f).argtypes = [c_void_p]
for f in ("libusb_claim_interface", "libusb_release_interface", "libusb_kernel_driver_active",
          "libusb_detach_kernel_driver", "libusb_set_configuration", "libusb_clear_halt"):
    getattr(L, f).argtypes = [c_void_p, c_int]
L.libusb_set_interface_alt_setting.argtypes = [c_void_p, c_int, c_int]
L.libusb_get_configuration.argtypes = [c_void_p, ctypes.POINTER(c_int)]
L.libusb_control_transfer.argtypes = [c_void_p, c_uint8, c_uint8, c_uint16, c_uint16, ctypes.c_char_p, c_uint16, c_uint]
L.libusb_bulk_transfer.argtypes = [c_void_p, c_uint8, ctypes.c_char_p, c_int, ctypes.POINTER(c_int), c_uint]
L.libusb_interrupt_transfer.argtypes = [c_void_p, c_uint8, ctypes.c_char_p, c_int, ctypes.POINTER(c_int), c_uint]
L.libusb_get_config_descriptor.argtypes = [c_void_p, c_uint8, ctypes.POINTER(c_void_p)]

TIMEOUT = -7
STALL = -9
OVERFLOW = -8
NO_DEVICE = -4
# Linux errno values the USB/IP client expects in RET_SUBMIT.status.
EPIPE, ENODEV, EOVERFLOW, EIO, ECONNRESET = -32, -19, -75, -5, -104
SPEED = {1: 1, 2: 2, 3: 3, 4: 5, 5: 6}  # libusb speed -> usb_device_speed (low, full, high, super, super+)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Device:
    def __init__(self, vid, pid, busid):
        self.ctx = c_void_p()
        assert L.libusb_init(ctypes.byref(self.ctx)) == 0
        self.h = L.libusb_open_device_with_vid_pid(self.ctx, vid, pid)
        if not self.h:
            sys.exit("cannot open %04x:%04x (not present, or no permission on its usbfs node)" % (vid, pid))
        dev = L.libusb_get_device(self.h)
        self.desc = DevDesc()
        L.libusb_get_device_descriptor(dev, ctypes.byref(self.desc))
        self.bus, self.addr = L.libusb_get_bus_number(dev), L.libusb_get_device_address(dev)
        self.speed = SPEED.get(L.libusb_get_device_speed(dev), 2)
        self.busid = busid
        self.ifaces = self._interfaces()
        self.claimed = set()
        self.gone = False
        self.claim_all()

    def _interfaces(self):
        # One config assumed (bNumConfigurations == 1 for the dongle); read its interface triples.
        raw = ctypes.create_string_buffer(512)
        n = L.libusb_control_transfer(self.h, 0x80, 6, 0x0200, 0, raw, 512, 1000)
        out, i, b = [], 0, raw.raw[:max(n, 0)]
        while i + 1 < len(b) and b[i] > 0:
            if b[i + 1] == 4:
                out.append((b[i + 2], b[i + 5], b[i + 6], b[i + 7]))
            i += b[i]
        self.config_value = b[5] if len(b) > 5 else 1
        return [t for t in out if t[0] not in [o[0] for o in out[:out.index(t)]]]

    def claim_all(self):
        for num, *_ in self.ifaces:
            if num in self.claimed:
                continue
            if L.libusb_kernel_driver_active(self.h, num) == 1:
                L.libusb_detach_kernel_driver(self.h, num)
            r = L.libusb_claim_interface(self.h, num)
            if r == 0:
                self.claimed.add(num)
            else:
                log("claim interface %d failed: %d" % (num, r))

    def record(self):
        path = ("/sys/bus/usb/devices/" + self.busid).encode().ljust(256, b"\0")
        busid = self.busid.encode().ljust(32, b"\0")
        d = self.desc
        return (path + busid + struct.pack(">IIIHHHBBBBBB", self.bus, self.addr, self.speed, d.idVendor, d.idProduct,
                d.bcdDevice, d.bDeviceClass, d.bDeviceSubClass, d.bDeviceProtocol, self.config_value,
                d.bNumConfigurations, len(self.ifaces)))


class Session:
    def __init__(self, sock, dev, log_):
        self.sock, self.dev, self.log = sock, dev, log_
        self.send_lock = threading.Lock()
        self.cancelled = set()
        self.pending = {}
        self.lock = threading.Lock()
        self.alive = True
        self.readers = {}  # bulk IN endpoint -> [bytearray, Condition, dropped]

    def send(self, data):
        with self.send_lock:
            self.sock.sendall(data)

    def ret_submit(self, seq, direction, ep, status, data=b"", actual=None):
        actual = len(data) if actual is None else actual
        self.log.add("ret", seq, time.time_ns(), status)
        if direction == 1 and data:
            self.log.add("ret_data", seq, bytes(data))
        elif direction == 0:
            self.log.add("ret_out_len", seq, actual)
        hdr = struct.pack(">IIIII", 3, seq, 0, 0, 0) + struct.pack(">iiiii", status, actual, 0, 0, 0) + b"\0" * 8
        self.send(hdr + (data if direction == 1 else b""))

    def control(self, seq, direction, setup, out_data, length):
        bm, breq, wval, widx, wlen = struct.unpack("<BBHHH", setup)
        h = self.dev.h
        if bm == 0x00 and breq == 9:  # SET_CONFIGURATION: libusb must do it, then interfaces are re-claimed
            for n in list(self.dev.claimed):
                L.libusb_release_interface(h, n)
            self.dev.claimed.clear()
            r = L.libusb_set_configuration(h, wval)
            self.dev.claim_all()
            return self.ret_submit(seq, direction, 0, 0 if r in (0, -6) else EPIPE)
        if bm == 0x01 and breq == 11:  # SET_INTERFACE
            r = L.libusb_set_interface_alt_setting(h, widx, wval)
            return self.ret_submit(seq, direction, 0, 0 if r == 0 else EPIPE)
        if bm == 0x02 and breq == 1 and wval == 0:  # CLEAR_FEATURE(ENDPOINT_HALT)
            r = L.libusb_clear_halt(h, widx & 0xFF)
            if r == NO_DEVICE:
                return self.gone()
            return self.ret_submit(seq, direction, 0, 0 if r == 0 else EPIPE)
        buf = ctypes.create_string_buffer(out_data if direction == 0 else max(wlen, length, 1))
        r = L.libusb_control_transfer(h, bm, breq, wval, widx, buf, wlen, 2000)
        if r == NO_DEVICE:
            return self.gone()
        if r < 0:
            return self.ret_submit(seq, direction, 0, EPIPE if r == STALL else EIO)
        if direction == 1:
            return self.ret_submit(seq, direction, 0, 0, buf.raw[:r])
        return self.ret_submit(seq, direction, 0, 0, actual=r)

    # A bulk IN endpoint is drained continuously into a buffer, so the device never waits on a
    # network round trip for its next IN token; URBs are answered from the buffer.
    def _reader(self, addr):
        buf, cond, _ = self.readers[addr]
        chunk, got = ctypes.create_string_buffer(4096), c_int()
        while self.alive:
            r = L.libusb_bulk_transfer(self.dev.h, addr, chunk, 4096, ctypes.byref(got), 100)
            if got.value:
                with cond:
                    buf.extend(chunk.raw[:got.value])
                    if len(buf) > (1 << 20):
                        self.readers[addr][2] += len(buf) - (1 << 20)
                        self.log.add("readahead_drop", time.time_ns(), addr & 0x0F, 1, len(buf) - (1 << 20))
                        del buf[:len(buf) - (1 << 20)]
                    cond.notify_all()
            elif r not in (0, TIMEOUT):
                log("reader 0x%02x stopped: %d" % (addr, r))
                return self.gone() if r == NO_DEVICE else None

    def bulk_in(self, seq, ep, length):
        addr = ep | 0x80
        if addr not in self.readers:
            self.readers[addr] = [bytearray(), threading.Condition(), 0]
            threading.Thread(target=self._reader, args=(addr,), daemon=True).start()
        buf, cond, _ = self.readers[addr]
        with cond:
            while self.alive and not buf:
                with self.lock:
                    if seq in self.cancelled:
                        self.cancelled.discard(seq)
                        return
                cond.wait(0.05)
            data = bytes(buf[:length])
            del buf[:len(data)]
        self.ret_submit(seq, 1, ep, 0, data)

    def gone(self):
        log("device gone; ending the session")
        self.alive = False
        self.dev.gone = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def transfer(self, seq, direction, ep, out_data, length, interrupt):
        if direction == 1 and not interrupt:
            return self.bulk_in(seq, ep, length)
        fn = L.libusb_interrupt_transfer if interrupt else L.libusb_bulk_transfer
        addr = ep | (0x80 if direction == 1 else 0)
        got = c_int()
        if direction == 0:
            buf = ctypes.create_string_buffer(out_data, len(out_data))
            r = fn(self.dev.h, addr, buf, len(out_data), ctypes.byref(got), 2000)
            status = 0 if r == 0 else (EPIPE if r == STALL else EIO)
            return self.ret_submit(seq, 0, ep, status, actual=got.value)
        buf = ctypes.create_string_buffer(max(length, 1))
        while self.alive:  # an IN URB stays pending until data arrives or the client unlinks it
            with self.lock:
                if seq in self.cancelled:
                    self.cancelled.discard(seq)
                    return
            r = fn(self.dev.h, addr, buf, length, ctypes.byref(got), 100)
            if r == TIMEOUT and got.value == 0:
                continue
            if r in (0, TIMEOUT):
                return self.ret_submit(seq, 1, ep, 0, buf.raw[:got.value])
            if r == NO_DEVICE:
                return self.gone()
            status = {STALL: EPIPE, OVERFLOW: EOVERFLOW, NO_DEVICE: ENODEV}.get(r, EIO)
            return self.ret_submit(seq, 1, ep, status)

    def handle_submit(self, seq, direction, ep, body):
        flags, length, _start, npk, _interval = struct.unpack(">IiiiI", body[:20])
        setup = body[20:28]
        out = self.recv(length) if direction == 0 and length > 0 else b""
        ttype = 0 if ep == 0 else (3 if any(e == (ep | (0x80 if direction else 0)) for e in self.dev.interrupt_eps) else 2)
        self.log.endpoint(ep, direction, ttype)
        self.log.add("submit", seq, time.time_ns(), ep, direction)
        if ep == 0:
            bm, breq, wval, widx, wlen = struct.unpack("<BBHHH", setup)
            self.log.add("submit_setup", seq, bm, breq, wval, widx, wlen)
        elif direction == 1:
            self.log.add("submit_in_len", seq, length)
        if out:
            self.log.add("submit_data", seq, bytes(out))
        if npk not in (0, -1, 0xFFFFFFFF) and npk > 0:
            return self.ret_submit(seq, direction, ep, EIO)
        if ep == 0:
            self.control(seq, direction, setup, out, length)
            return
        interrupt = any(e == (ep | (0x80 if direction else 0)) for e in self.dev.interrupt_eps)
        t = threading.Thread(target=self.transfer, args=(seq, direction, ep, out, length, interrupt), daemon=True)
        t.start()

    def recv(self, n):
        b = b""
        while len(b) < n:
            c = self.sock.recv(n - len(b))
            if not c:
                raise ConnectionError("client closed")
            b += c
        return b

    def run(self):
        try:
            while True:
                hdr = self.recv(48)
                cmd, seq, _devid, direction, ep = struct.unpack(">IIIII", hdr[:20])
                if cmd == 1:
                    self.handle_submit(seq, direction, ep, hdr[20:48])
                elif cmd == 2:
                    victim = struct.unpack(">I", hdr[20:24])[0]
                    now = time.time_ns()
                    self.log.add("unlink", seq, victim, now)
                    self.log.add("unlink_ret", seq, now, ECONNRESET)
                    with self.lock:
                        self.cancelled.add(victim)
                    self.send(struct.pack(">IIIII", 4, seq, 0, 0, 0) + struct.pack(">i", ECONNRESET) + b"\0" * 24)
                else:
                    log("unknown command", cmd)
                    return
        except (ConnectionError, OSError) as e:
            log("session ended:", e)
        finally:
            self.alive = False


def endpoints(dev):
    raw = ctypes.create_string_buffer(512)
    n = L.libusb_control_transfer(dev.h, 0x80, 6, 0x0200, 0, raw, 512, 1000)
    b, i, eps = raw.raw[:max(n, 0)], 0, []
    while i + 1 < len(b) and b[i] > 0:
        if b[i + 1] == 5 and (b[i + 3] & 3) == 3:
            eps.append(b[i + 2])
        i += b[i]
    return eps


def serve(dev, port):
    dev.interrupt_eps = endpoints(dev)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(1)
    log("exporting %04x:%04x as busid %s on tcp %d (interfaces %s, interrupt eps %s)" % (
        dev.desc.idVendor, dev.desc.idProduct, dev.busid, port, [i[0] for i in dev.ifaces], [hex(e) for e in dev.interrupt_eps]))
    while True:
        sock, peer = srv.accept()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        log("connection from", peer)
        try:
            ver, code, _status = struct.unpack(">HHI", recv_exact(sock, 8))
            if code == 0x8005:  # OP_REQ_DEVLIST
                rec = dev.record() + b"".join(struct.pack("BBBB", c, s, p, 0) for _n, c, s, p in dev.ifaces)
                sock.sendall(struct.pack(">HHII", ver, 0x0005, 0, 1) + rec)
                sock.close()
            elif code == 0x8003:  # OP_REQ_IMPORT
                want = recv_exact(sock, 32).split(b"\0")[0].decode()
                if want != dev.busid:
                    log("import of unknown busid", want)
                    sock.sendall(struct.pack(">HHI", ver, 0x0003, 1))
                    sock.close()
                    continue
                sock.sendall(struct.pack(">HHI", ver, 0x0003, 0) + dev.record())
                log("imported by", peer)
                elog = EtnfLog(LOG_ROOT, dev.busid, dev.desc.idVendor, dev.desc.idProduct, "%s:%d" % peer)
                log("logging traffic to", elog.dir)
                try:
                    Session(sock, dev, elog).run()
                finally:
                    elog.close()
                sock.close()
                if dev.gone:
                    log("device handle stale; exiting for a fresh supervisor restart")
                    srv.close()
                    return
            else:
                log("unknown op 0x%04x" % code)
                sock.close()
        except (ConnectionError, OSError, struct.error) as e:
            log("connection error:", e)
            sock.close()


def recv_exact(sock, n):
    b = b""
    while len(b) < n:
        c = sock.recv(n - len(b))
        if not c:
            raise ConnectionError("closed")
        b += c
    return b


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vid", default="248a")
    ap.add_argument("--pid", default="8002")
    ap.add_argument("--port", type=int, default=3240)
    ap.add_argument("--busid", default="1-1")
    ap.add_argument("--log-dir", default=os.path.expanduser("~/usbip/log"))
    a = ap.parse_args()
    LOG_ROOT = a.log_dir
    serve(Device(int(a.vid, 16), int(a.pid, 16), a.busid), a.port)
