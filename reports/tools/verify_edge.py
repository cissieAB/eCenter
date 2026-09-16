#!/usr/bin/env python3
"""Verify one directed edge of the eCenter pipeline against a known traffic run.

Two subcommands:

  record   Poll the backend's GET /latest (what the dashboard draws) and append
           every new frame for the edge, in both directions, to a JSONL file.
           Start it before the traffic and stop it (Ctrl-C) afterwards.

  compare  Read the per-second Redis hashes packet:{dest}:{src}:{ts} for the
           window and report totals, per-second rates, internal consistency,
           and, optionally, agreement with a reference byte count (iperf3) and
           with the frames captured by `record`.

Needs the `redis` Python package (the simulator venv has it):
  ld2606_daos_redis/traffic-simulator/venv/bin/python reports/tools/verify_edge.py ...
"""
import argparse
import datetime
import json
import sys
import time
import urllib.request

try:
    import redis
except ImportError:  # only `compare` needs it
    redis = None


def cmd_record(a):
    keys = (f"{a.src}:{a.dest}", f"{a.dest}:{a.src}")  # /latest keys are src:dest
    seen = set()
    with open(a.out, "a") as out:
        while True:
            try:
                with urllib.request.urlopen(f"{a.backend}/latest", timeout=2) as resp:
                    data = json.load(resp)["data"]
            except Exception as e:  # keep recording through backend restarts
                print(json.dumps({"err": str(e), "wall": time.time()}), file=out, flush=True)
                time.sleep(1)
                continue
            for k in keys:
                e = data.get(k)
                if e and (k, e["timestamp"]) not in seen:
                    seen.add((k, e["timestamp"]))
                    e["wall"] = time.time()
                    print(json.dumps(e), file=out, flush=True)
            time.sleep(0.5)


def load_series(r, src, dest, start, end):
    rows = {}
    for t in range(start, end + 1):
        h = r.hgetall(f"packet:{dest}:{src}:{t}")  # key order: dest first
        if not h:
            continue
        tcp_b, udp_b = json.loads(h["tcp_bytes"]), json.loads(h["udp_bytes"])
        rows[t] = {
            "bytes": int(h["total_bytes"]),
            "packets": int(h["total_packets"]),
            "sps": int(h["samples_per_second"]),
            "nsamples": len(tcp_b),
            "array_sum": sum(tcp_b) + sum(udp_b),
            "tcp_bytes": sum(tcp_b),
            "udp_bytes": sum(udp_b),
        }
    return rows


def read_iperf_json(path):
    """Window and reference byte count from the *client's* `iperf3 -J` output.

    Reference = data bytes that reached the receiving iperf3:
      TCP: end.sum_received.bytes
      UDP: (packets - lost_packets) * blksize, from end.sum (sent side counts)
    For UDP, packets lost on the receiver *after* the TC hook (socket buffer
    overflow) are still counted by eBPF, so eBPF can land between the
    received and sent counts; both are printed.
    """
    j = json.load(open(path))
    st, end = j["start"], j["end"]
    t0 = int(st["timestamp"]["timesecs"])
    test = st["test_start"]
    proto = test.get("protocol", "TCP")
    duration = end.get("sum_sent", end.get("sum", {})).get("end") or test.get("duration")
    info = {}
    if proto == "UDP":
        s = end["sum"]
        blk = int(test["blksize"])
        sent_bytes = int(s["packets"]) * blk
        ref = (int(s["packets"]) - int(s["lost_packets"])) * blk
        info = {"blksize": blk, "packets sent": f"{s['packets']:,}", "lost packets": f"{s['lost_packets']:,}",
                "lost percent": f"{s['lost_percent']:.3f}", "data bytes sent": f"{sent_bytes:,}",
                "data bytes received": f"{ref:,}"}
    else:
        sent, recv = end["sum_sent"], end["sum_received"]
        ref = int(recv["bytes"])
        info = {"bytes sent": f"{int(sent['bytes']):,}", "bytes received": f"{ref:,}",
                "retransmits": sent.get("retransmits"), "sender Gbps": f"{sent['bits_per_second'] / 1e9:.2f}"}
    return {"start": t0, "end": t0 + int(float(duration)) + 1, "ref_bytes": ref, "protocol": proto,
            "streams": test.get("num_streams"), "reverse": bool(test.get("reverse")), "info": info}


def cmd_compare(a):
    if redis is None:
        sys.exit("compare needs the redis package: pip install redis")
    r = redis.Redis(host=a.redis_host, port=a.redis_port, decode_responses=True)
    tz = datetime.timezone(datetime.timedelta(hours=a.utc_offset))
    fmt = lambda t: datetime.datetime.fromtimestamp(t, tz).strftime("%H:%M:%S")

    if a.iperf_json:
        ip = read_iperf_json(a.iperf_json)
        # Pad by a few seconds: collector and iperf3 clocks and second boundaries differ.
        a.start = a.start if a.start is not None else ip["start"] - 3
        a.end = a.end if a.end is not None else ip["end"] + 3
        a.ref_bytes = a.ref_bytes or ip["ref_bytes"]
        print(f"iperf3 JSON: {ip['protocol']}, {ip['streams']} stream(s), reverse={ip['reverse']}, "
              f"{fmt(ip['start'])}-{fmt(ip['end'])}")
        for k, v in ip["info"].items():
            print(f"  {k:<20} {v}")
        print()
    if a.start is None or a.end is None:
        sys.exit("give --start and --end, or --iperf-json")

    fwd = load_series(r, a.src, a.dest, a.start, a.end)
    rev = load_series(r, a.dest, a.src, a.start, a.end)
    if not fwd:
        sys.exit(f"no packet:{a.dest}:{a.src}:* keys in [{a.start}, {a.end}] (expired? TTL is 3600s)")

    busy = [t for t in sorted(fwd) if fwd[t]["bytes"] * 8 >= a.busy_gbps * 1e9]
    b = sum(v["bytes"] for v in fwd.values())
    p = sum(v["packets"] for v in fwd.values())
    print(f"edge {a.src} -> {a.dest}, window {a.start}..{a.end} ({fmt(a.start)}-{fmt(a.end)})")
    print(f"  seconds present      {len(fwd)} of {a.end - a.start + 1}; missing: "
          f"{[fmt(t) for t in range(a.start, a.end + 1) if t not in fwd][:10]}")
    print(f"  total_bytes          {b:,}  ({b / 2**30:.3f} GiB)")
    print(f"  total_packets        {p:,}  (avg {b / max(p, 1):,.0f} B per counted packet)")
    print(f"  tcp / udp bytes      {sum(v['tcp_bytes'] for v in fwd.values()):,} / "
          f"{sum(v['udp_bytes'] for v in fwd.values()):,}")
    if busy:
        bb = sum(fwd[t]["bytes"] for t in busy)
        print(f"  busy seconds         {len(busy)} ({fmt(busy[0])}-{fmt(busy[-1])}), "
              f"mean {bb * 8 / len(busy) / 1e9:.2f} Gbps, "
              f"peak {max(fwd[t]['bytes'] for t in busy) * 8 / 1e9:.2f} Gbps")
    bad = [t for t in fwd if fwd[t]["array_sum"] != fwd[t]["bytes"] or fwd[t]["nsamples"] != fwd[t]["sps"]]
    print(f"  inconsistent seconds {len(bad)} (sample arrays must sum to total_bytes, length = sps)")
    rb = sum(v["bytes"] for v in rev.values())
    print(f"  reverse {a.dest} -> {a.src}: {rb:,} bytes, {sum(v['packets'] for v in rev.values()):,} packets")

    if a.ref_bytes:
        diff = b - a.ref_bytes
        print(f"\nreference (iperf3)     {a.ref_bytes:,} bytes")
        print(f"  eBPF - reference     {diff:+,} ({diff / a.ref_bytes * 100:+.3f}%)")
        per_pkt = a.l2_header + a.l3l4_header
        hdr = p * per_pkt
        print(f"  expected L2-L4 hdrs  {p:,} x {per_pkt} B = {hdr:,}")
        print(f"  unexplained          {diff - hdr:+,} bytes ({(diff - hdr) / a.ref_bytes * 100:+.4f}%)")
    if a.nic_bytes and a.nic_packets and a.ref_bytes:
        nd = a.nic_bytes - a.ref_bytes
        nh = a.nic_packets * (a.l3l4_header + 14)
        print(f"\nNIC rx delta           {a.nic_bytes:,} bytes, {a.nic_packets:,} wire packets")
        print(f"  NIC - reference      {nd:+,}; expected {a.nic_packets:,} x {a.l3l4_header + 14} B = {nh:,}; "
              f"unexplained {nd - nh:+,}")
        print(f"  wire packets per counted packet (GRO factor) {a.nic_packets / max(p, 1):.2f}")

    if a.served:
        served = {}
        for line in open(a.served):
            e = json.loads(line)
            if e.get("src") == a.src and e.get("dest") == a.dest and a.start <= e["timestamp"] <= a.end:
                served[e["timestamp"]] = e
        mism = [t for t in served if t in fwd and served[t]["total_bytes"] != fwd[t]["bytes"]]
        unseen = [t for t in busy if t not in served]
        lags = sorted(e["wall"] - t for t, e in served.items())
        print(f"\n/latest frames captured {len(served)}; value mismatches vs Redis {len(mism)}; "
              f"busy seconds never served {len(unseen)}")
        if lags:
            print(f"  frame lag (wall - timestamp) median {lags[len(lags) // 2]:.2f}s, max {lags[-1]:.2f}s")

    if a.per_second:
        print("\nper second: time  Gbps  packets  min/max sub-second Gbps")
        for t in range(a.start, a.end + 1):
            v = fwd.get(t)
            if v is None:
                print(f"  {fmt(t)}  MISSING")
                continue
            tcp, udp = r.hmget(f"packet:{a.dest}:{a.src}:{t}", "tcp_bytes", "udp_bytes")
            s = [(x + y) * 8 * v["sps"] / 1e9 for x, y in zip(json.loads(tcp), json.loads(udp))]
            print(f"  {fmt(t)}  {v['bytes'] * 8 / 1e9:6.2f}  {v['packets']:>8,}  {min(s):5.1f}/{max(s):5.1f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="source IPv4 (traffic sender)")
    ap.add_argument("--dest", required=True, help="destination IPv4 (traffic receiver)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record")
    rec.add_argument("--backend", default="http://localhost:8080")
    rec.add_argument("--out", required=True)

    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--start", type=int, help="first Unix second of the window")
    cmp_.add_argument("--end", type=int, help="last Unix second of the window")
    cmp_.add_argument("--iperf-json", help="client's `iperf3 -J` output; sets window and --ref-bytes")
    cmp_.add_argument("--redis-host", default="localhost")
    cmp_.add_argument("--redis-port", type=int, default=6379)
    cmp_.add_argument("--ref-bytes", type=int, help="reference byte count, e.g. iperf3 -n size in bytes")
    cmp_.add_argument("--l3l4-header", type=int, default=52,
                      help="IP + L4 header bytes per packet: 52 for TCP with timestamps, 28 for UDP")
    cmp_.add_argument("--l2-header", type=int, default=14,
                      help="Ethernet header bytes eBPF counts per packet: 14 for TC, or 0 for XDP "
                           "and for TC runs before the switch from ip->tot_len to skb->len")
    cmp_.add_argument("--nic-bytes", type=int, help="receiver NIC rx_bytes delta over the run")
    cmp_.add_argument("--nic-packets", type=int, help="receiver NIC rx_packets delta over the run")
    cmp_.add_argument("--served", help="JSONL written by `record`")
    cmp_.add_argument("--busy-gbps", type=float, default=1.0)
    cmp_.add_argument("--utc-offset", type=float, default=-4, help="hours, for display (default -4, EDT)")
    cmp_.add_argument("--per-second", action="store_true")

    a = ap.parse_args()
    cmd_record(a) if a.cmd == "record" else cmd_compare(a)


if __name__ == "__main__":
    main()
