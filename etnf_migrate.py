"""Rewrite log/session=<id>/ (first layout) as log/session_id=<id>/ (etnf_log.py's layout).

  python etnf_migrate.py <log_root>

Refuses a session when a dropped column is not derivable, and removes the old directory only
after row counts and SHA-256 of every payload column match.
"""
import glob
import hashlib
import os
import shutil
import sys

import pyarrow as pa
import pyarrow.parquet as pq

from etnf_log import SCHEMAS


def read(d, n):
    fs = sorted(glob.glob(os.path.join(d, n, "*.parquet")))
    return pa.concat_tables([pq.read_table(f) for f in fs]).to_pylist() if fs else []


def digest(rows, col):
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: r["seq"]):
        h.update(r["seq"].to_bytes(8, "little") + r[col])
    return h.hexdigest()


def migrate(old):
    sid = int(old.rsplit("=", 1)[1])
    new = os.path.join(os.path.dirname(old), "session_id=%d" % sid)
    t = {n: read(old, n) for n in ("session", "transfer_type", "submit", "submit_setup", "submit_data", "ret",
                                   "ret_out_len", "ret_data", "unlink", "unlink_ret", "readahead_drop")}
    if any(r["session_id"] != sid for n, v in t.items() if n != "transfer_type" for r in v):
        raise SystemExit("%s: a row's session_id differs from its directory" % old)
    ep_type, out_len = {}, {r["seq"]: len(r["data"]) for r in t["submit_data"]}
    w_len = {r["seq"]: r["w_length"] for r in t["submit_setup"]}
    for r in t["submit"]:
        if ep_type.setdefault((r["ep"], r["direction"]), r["transfer_type_id"]) != r["transfer_type_id"]:
            raise SystemExit("%s: endpoint %s has two transfer types" % (old, (r["ep"], r["direction"])))
        if r["ep"] == 0 and w_len.get(r["seq"]) != r["requested_len"]:
            raise SystemExit("%s seq %d: control requested_len != w_length" % (old, r["seq"]))
        if r["ep"] != 0 and r["direction"] == 0 and out_len.get(r["seq"], 0) != r["requested_len"]:
            raise SystemExit("%s seq %d: OUT requested_len != len(data)" % (old, r["seq"]))
    strip = lambda rows: [{k: v for k, v in r.items() if k != "session_id"} for r in rows]
    out = {n: strip(v) for n, v in t.items()}
    out["endpoint"] = [{"ep": e, "direction": d, "transfer_type_id": k} for (e, d), k in sorted(ep_type.items())]
    out["submit_in_len"] = [{"seq": r["seq"], "requested_len": r["requested_len"]}
                            for r in t["submit"] if r["ep"] != 0 and r["direction"] == 1]
    out["submit"] = [{k: r[k] for k in ("seq", "t_ns", "ep", "direction")} for r in t["submit"]]
    out["readahead_drop"] = [{"t_ns": r["t_ns"], "ep": r["ep"] & 0x0F, "direction": 1, "bytes": r["bytes"]}
                             for r in t["readahead_drop"]]
    for n, rows in out.items():
        if not rows:
            continue
        os.makedirs(os.path.join(new, n), exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows, schema=pa.schema(SCHEMAS[n])),
                       os.path.join(new, n, "part-000000.parquet"), compression="zstd")
    back = {n: read(new, n) for n in t}
    for n in t:
        if len(back[n]) != len(t[n]):
            raise SystemExit("%s: %s has %d rows, expected %d" % (new, n, len(back[n]), len(t[n])))
    for n in ("submit_data", "ret_data"):
        if digest(back[n], "data") != digest(t[n], "data"):
            raise SystemExit("%s: %s payload hash differs" % (new, n))
    shutil.rmtree(old)
    print("ok %s -> %s (%s)" % (os.path.basename(old), os.path.basename(new),
                                 ", ".join("%s %d" % (n, len(v)) for n, v in out.items() if v)))


if __name__ == "__main__":
    for d in sorted(glob.glob(os.path.join(sys.argv[1], "session=*"))):
        migrate(d)
