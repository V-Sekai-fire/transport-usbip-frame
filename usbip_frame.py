"""A userspace USB/IP server over libusb (no usbip-host kernel module needed).

  python3 usbip_frame.py [--port 3240] [--exclude 1-1.2 --exclude 28de:2102]

Exports every non-hub USB device, enumerated from sysfs on each request so a device that
re-enumerates at a new busid is listed there. A device is opened only while imported, and
its kernel driver is returned on release. Implements OP_REQ_DEVLIST, OP_REQ_IMPORT, and URB traffic:
USBIP_CMD_SUBMIT (control, bulk, interrupt) and USBIP_CMD_UNLINK. Isochronous is refused.
"""
import argparse
import os
import collections
import ctypes
import socket
import struct
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
L.libusb_get_device_list.argtypes = [c_void_p, ctypes.POINTER(ctypes.POINTER(c_void_p))]
L.libusb_free_device_list.argtypes = [ctypes.POINTER(c_void_p), c_int]
L.libusb_open.argtypes = [c_void_p, ctypes.POINTER(c_void_p)]
L.libusb_close.argtypes = [c_void_p]
L.libusb_exit.argtypes = [c_void_p]
L.libusb_set_auto_detach_kernel_driver.argtypes = [c_void_p, c_int]
L.libusb_get_config_descriptor.argtypes = [c_void_p, c_uint8, ctypes.POINTER(c_void_p)]

TIMEOUT = -7
STALL = -9
OVERFLOW = -8
NO_DEVICE = -4
# Linux errno values the USB/IP client expects in RET_SUBMIT.status.
EPIPE, ENODEV, EOVERFLOW, EIO, ECONNRESET = -32, -19, -75, -5, -104
SPEED = {"1.5": 1, "12": 2, "480": 3, "5000": 5, "10000": 6, "20000": 6}  # sysfs Mb/s -> usb_device_speed
SYSFS = "/sys/bus/usb/devices"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Device:
    def __init__(self, busid):
        self.busid = busid
        self.bus, self.addr = int(attr(busid, "busnum")), int(attr(busid, "devnum"))
        self.ctx = c_void_p()
        assert L.libusb_init(ctypes.byref(self.ctx)) == 0
        self.h = self._open()
        if not self.h:
            L.libusb_exit(self.ctx)
            raise OSError("cannot open %s (gone, or no permission on its usbfs node)" % busid)
        L.libusb_set_auto_detach_kernel_driver(self.h, 1)
        self.desc = DevDesc()
        L.libusb_get_device_descriptor(L.libusb_get_device(self.h), ctypes.byref(self.desc))
        self.ifaces = self._interfaces()
        self.claimed = set()
        self.gone = False
        self.claim_all()
        self.interrupt_eps = endpoints(self)

    def _open(self):
        lst, h = ctypes.POINTER(c_void_p)(), c_void_p()
        n = L.libusb_get_device_list(self.ctx, ctypes.byref(lst))
        try:
            for i in range(max(n, 0)):
                d = lst[i]
                if L.libusb_get_bus_number(d) == self.bus and L.libusb_get_device_address(d) == self.addr:
                    return h.value if L.libusb_open(d, ctypes.byref(h)) == 0 else None
        finally:
            if n > 0:
                L.libusb_free_device_list(lst, 1)
        return None

    def close(self):
        for n in self.claimed:
            L.libusb_release_interface(self.h, n)
        L.libusb_close(self.h)
        L.libusb_exit(self.ctx)

    def _interfaces(self):
        # One config assumed; read its interface triples.
        raw = ctypes.create_string_buffer(512)
        n = L.libusb_control_transfer(self.h, 0x80, 6, 0x0200, 0, raw, 512, 1000)
        out, i, b = [], 0, raw.raw[:max(n, 0)]
        while i + 1 < len(b) and b[i] > 0:
            if b[i + 1] == 4:
                out.append((b[i + 2], b[i + 5], b[i + 6], b[i + 7]))
            i += b[i]
        return [t for t in out if t[0] not in [o[0] for o in out[:out.index(t)]]]

    def claim_all(self):
        for num, *_ in self.ifaces:
            if num in self.claimed:
                continue
            r = L.libusb_claim_interface(self.h, num)
            if r == 0:
                self.claimed.add(num)
            else:
                log("claim interface %d failed: %d" % (num, r))


def attr(busid, name, base=SYSFS):
    with open(os.path.join(base, busid, name)) as f:
        return f.read().strip()


def exportable(exclude, base=SYSFS):
    out = []
    for name in sorted(os.listdir(base)):
        if ":" in name or name.startswith("usb"):
            continue
        try:
            if attr(name, "bDeviceClass", base) == "09":
                continue
            vp = "%s:%s" % (attr(name, "idVendor", base), attr(name, "idProduct", base))
        except OSError:
            continue
        if name not in exclude and vp not in exclude:
            out.append(name)
    return out


def interfaces(busid, base=SYSFS):
    out = []
    for name in sorted(os.listdir(os.path.join(base, busid))):
        if name.startswith(busid + ":"):
            out.append(tuple(int(attr(os.path.join(busid, name), k, base), 16) for k in
                             ("bInterfaceClass", "bInterfaceSubClass", "bInterfaceProtocol")))
    return out


def record(busid, base=SYSFS):
    a = lambda k: attr(busid, k, base)
    h = lambda k: int(a(k), 16)
    return (("/sys/bus/usb/devices/" + busid).encode().ljust(256, b"\0") + busid.encode().ljust(32, b"\0") +
            struct.pack(">IIIHHHBBBBBB", int(a("busnum")), int(a("devnum")), SPEED.get(a("speed"), 2),
                        h("idVendor"), h("idProduct"), h("bcdDevice"), h("bDeviceClass"), h("bDeviceSubClass"),
                        h("bDeviceProtocol"), int(a("bConfigurationValue") or 1), int(a("bNumConfigurations")),
                        len(interfaces(busid, base))))


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


def keepalive(sock):
    # A client that vanished without a FIN would otherwise hold its device forever.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 30000)


def endpoints(dev):
    raw = ctypes.create_string_buffer(512)
    n = L.libusb_control_transfer(dev.h, 0x80, 6, 0x0200, 0, raw, 512, 1000)
    b, i, eps = raw.raw[:max(n, 0)], 0, []
    while i + 1 < len(b) and b[i] > 0:
        if b[i + 1] == 5 and (b[i + 3] & 3) == 3:
            eps.append(b[i + 2])
        i += b[i]
    return eps


class Server:
    def __init__(self, port, exclude):
        self.port, self.exclude = port, exclude
        self.active = {}
        self.lock = threading.Lock()

    def available(self):
        with self.lock:
            return [b for b in exportable(self.exclude) if b not in self.active]

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(8)
        log("serving tcp %d; exportable now: %s" % (self.port, exportable(self.exclude)))
        while True:
            sock, peer = srv.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            keepalive(sock)
            threading.Thread(target=self.handle, args=(sock, peer), daemon=True).start()

    def handle(self, sock, peer):
        try:
            ver, code, _status = struct.unpack(">HHI", recv_exact(sock, 8))
            if code == 0x8005:  # OP_REQ_DEVLIST
                devs = self.available()
                body = b"".join(record(b) + b"".join(struct.pack("BBBB", *i, 0) for i in interfaces(b)) for b in devs)
                sock.sendall(struct.pack(">HHII", ver, 0x0005, 0, len(devs)) + body)
            elif code == 0x8003:  # OP_REQ_IMPORT
                self.import_(sock, peer, ver, recv_exact(sock, 32).split(b"\0")[0].decode())
            else:
                log("unknown op 0x%04x from %s" % (code, peer))
        except (ConnectionError, OSError, struct.error) as e:
            log("connection error from %s: %s" % (peer, e))
        finally:
            sock.close()

    def import_(self, sock, peer, ver, busid):
        with self.lock:
            ok = busid in exportable(self.exclude) and busid not in self.active
            if ok:
                self.active[busid] = peer
        dev = None
        try:
            if ok:
                dev = Device(busid)
        except (OSError, ValueError) as e:
            log("import of %s failed: %s" % (busid, e))
        if dev is None:
            log("refusing import of %s by %s" % (busid, peer))
            with self.lock:
                if ok:
                    del self.active[busid]
            sock.sendall(struct.pack(">HHI", ver, 0x0003, 1))
            return
        try:
            sock.sendall(struct.pack(">HHI", ver, 0x0003, 0) + record(busid))
            log("%s %04x:%04x imported by %s (interfaces %s, interrupt eps %s)" % (
                busid, dev.desc.idVendor, dev.desc.idProduct, peer, [i[0] for i in dev.ifaces],
                [hex(e) for e in dev.interrupt_eps]))
            elog = EtnfLog(LOG_ROOT, busid, dev.desc.idVendor, dev.desc.idProduct, "%s:%d" % peer)
            try:
                Session(sock, dev, elog).run()
            finally:
                elog.close()
        finally:
            dev.close()
            with self.lock:
                del self.active[busid]
            log("%s released" % busid)


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
    ap.add_argument("--port", type=int, default=3240)
    ap.add_argument("--exclude", action="append", default=[], help="busid or vid:pid to keep off the export list")
    ap.add_argument("--log-dir", default=os.path.expanduser("~/usbip/log"))
    a = ap.parse_args()
    LOG_ROOT = a.log_dir
    Server(a.port, set(e.lower() for e in a.exclude)).serve()
