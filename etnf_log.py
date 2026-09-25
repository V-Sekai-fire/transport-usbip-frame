"""USB/IP traffic as ETNF relations in zstd Parquet, one directory per session.

Relations (every column always set; optional facts live in satellites). session_id is the
hive partition key (session_id=<n>/), so it is not repeated inside the files:
  session(busid, vid, pid, peer)
  transfer_type(transfer_type_id, name)                          vocabulary
  endpoint(ep, direction, transfer_type_id)
  submit(seq, t_ns, ep, direction)
  submit_setup(seq, bm_request_type, b_request, w_value, w_index, w_length)   control
  submit_in_len(seq, requested_len)                              bulk/interrupt IN
  submit_data(seq, data)                                         OUT payloads (length is len(data))
  ret(seq, t_ns, status)
  ret_out_len(seq, actual_len)                                   OUT only (IN length is len(data))
  ret_data(seq, data)                                            IN payloads
  unlink(seq, victim_seq, t_ns)
  unlink_ret(seq, t_ns, status)
  readahead_drop(t_ns, ep, direction, bytes)
"""
import os
import threading
import time

import pyarrow as pa
import pyarrow.parquet as pq

I8, I16, I32, I64, S, B = pa.int8(), pa.int16(), pa.int32(), pa.int64(), pa.string(), pa.binary()
SCHEMAS = {
    "session": [("busid", S), ("vid", I32), ("pid", I32), ("peer", S)],
    "transfer_type": [("transfer_type_id", I8), ("name", S)],
    "endpoint": [("ep", I8), ("direction", I8), ("transfer_type_id", I8)],
    "submit": [("seq", I64), ("t_ns", I64), ("ep", I8), ("direction", I8)],
    "submit_setup": [("seq", I64), ("bm_request_type", I16), ("b_request", I16),
                     ("w_value", I32), ("w_index", I32), ("w_length", I32)],
    "submit_in_len": [("seq", I64), ("requested_len", I32)],
    "submit_data": [("seq", I64), ("data", B)],
    "ret": [("seq", I64), ("t_ns", I64), ("status", I32)],
    "ret_out_len": [("seq", I64), ("actual_len", I32)],
    "ret_data": [("seq", I64), ("data", B)],
    "unlink": [("seq", I64), ("victim_seq", I64), ("t_ns", I64)],
    "unlink_ret": [("seq", I64), ("t_ns", I64), ("status", I32)],
    "readahead_drop": [("t_ns", I64), ("ep", I8), ("direction", I8), ("bytes", I64)],
}
TRANSFER_TYPES = [(0, "control"), (1, "isochronous"), (2, "bulk"), (3, "interrupt")]


class EtnfLog:
    def __init__(self, root, busid, vid, pid, peer, flush_s=10.0):
        self.session_id = time.time_ns()
        self.dir = os.path.join(root, "session_id=%d" % self.session_id)
        self.rows = {k: [] for k in SCHEMAS}
        self.lock = threading.Lock()
        self.part = 0
        self.flush_s = flush_s
        self.alive = True
        self.endpoints = set()
        self.add("session", busid, vid, pid, peer)
        for t in TRANSFER_TYPES:
            self.add("transfer_type", *t)
        self._flush()
        threading.Thread(target=self._loop, daemon=True).start()

    def add(self, table, *values):
        with self.lock:
            self.rows[table].append(values)

    def endpoint(self, ep, direction, transfer_type_id):
        if (ep, direction) not in self.endpoints:
            self.endpoints.add((ep, direction))
            self.add("endpoint", ep, direction, transfer_type_id)

    def _loop(self):
        while self.alive:
            time.sleep(self.flush_s)
            self._flush()

    def _flush(self):
        with self.lock:
            batch, self.rows = self.rows, {k: [] for k in SCHEMAS}
            part = self.part
            self.part += 1
        for table, rows in batch.items():
            if not rows:
                continue
            cols = list(zip(*rows))
            schema = pa.schema(SCHEMAS[table])
            arrays = [pa.array(list(c), type=f.type) for c, f in zip(cols, schema)]
            os.makedirs(os.path.join(self.dir, table), exist_ok=True)
            dst = os.path.join(self.dir, table, "part-%06d.parquet" % part)
            pq.write_table(pa.Table.from_arrays(arrays, schema=schema), dst + ".tmp", compression="zstd")
            os.replace(dst + ".tmp", dst)

    def close(self):
        self.alive = False
        self._flush()
