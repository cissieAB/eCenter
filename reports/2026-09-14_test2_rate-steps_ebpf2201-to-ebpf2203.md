# Test 2: TCP rate steps, ebpf2201 → ebpf2203

**Status:** PLANNED, not yet run. Result cells marked _TBD_ are filled in
after the run.
**Date:** _TBD_
**Goal:** check that the dashboard follows **changes** in rate, not just
totals. Three TCP steps are run at about 1 Gbps, 10 Gbps and unlimited:
- Each step's per-second rate and total bytes must match iperf3.
- The dashboard must show each step, and the drop back to idle between
  steps, within a few seconds.

This test builds on
[Test 1](2026-09-14_test1_ebpf2201-to-ebpf2203.md), which checked totals for
a single 100 GiB transfer. The byte-count reasoning (headers, GRO) is
explained there and not repeated here.

## Setup

| | Sender (client) | Receiver (server) |
|---|---|---|
| Host | `ebpf2201` | `ebpf2203` |
| IPv4 | `129.57.178.85` | `129.57.178.86` |
| NIC | | `enp1s0f1np1`, MTU 9000 |
| Counted by | | TC ingress hook + `tc_collector` on ebpf2203 |

### Host state (record before running)

Throughput changed between runs on the same day. Test 1 stepped down from
56 to 19 Gbps, while later runs at 15:24–15:28 reached 99 Gbps. Record the
state of both hosts so the results can be interpreted:

```bash
iperf3 --version | head -1
cpupower frequency-info -p 2>/dev/null | grep -i governor
sysctl net.core.rmem_max net.core.wmem_max net.ipv4.tcp_rmem net.ipv4.tcp_wmem
ip link show dev $IFACE | grep -o 'mtu [0-9]*'
```

| | ebpf2201 | ebpf2203 |
|---|---|---|
| iperf3 version | _TBD_ | _TBD_ |
| CPU governor | _TBD_ | _TBD_ |
| `rmem_max` / `wmem_max` | _TBD_ | _TBD_ |
| MTU | _TBD_ | _TBD_ |

## Procedure

### 1. Before the traffic

**On ebpf2203 (receiver):** confirm the hook is attached, save the NIC
counters, and start the server:

```bash
sudo tc filter show dev $IFACE ingress | grep tc-ing     # hook attached
ip -s -s link show dev $IFACE                             # save "before"
iperf3 -s -B 129.57.178.86
```

**On the machine that will run the verification** (the central host, or a
laptop with Redis 6379 and the backend 8080 forwarded), start the frame
recorder. It captures what the dashboard is served:

```bash
cd $ECENTER        # the eCenter checkout
PY=ld2606_daos_redis/traffic-simulator/venv/bin/python
$PY reports/tools/verify_edge.py --src 129.57.178.85 --dest 129.57.178.86 \
    record --out test2_frames.jsonl &
```

Open the dashboard in live mode, and pin the `ebpf2201 → ebpf2203` edge so
its detail panel stays open.

### 2. Run the three steps (on ebpf2201)

```bash
for rate in 250M 2500M 0; do     # per stream × 4 streams: ~1 Gbps, ~10 Gbps, unlimited
  iperf3 -c 129.57.178.86 -B 129.57.178.85 -P 4 -b $rate -t 20 -J > test2_b${rate}.json
  sleep 15                       # idle gap, so each step is clearly separated
done
```

- `-J` writes JSON with exact byte counts and the start time. The script
  reads these files, so don't replace `-J` with the text output.
- `-b` is the rate **per stream**; `-b 0` means unlimited.
- While it runs, write down what the dashboard shows at each step (see
  step 4).

### 3. After the traffic

1. On ebpf2203, run `ip -s -s link show dev $IFACE` again and subtract the
   "before" values.
2. Stop the recorder: `kill %1`.
3. Copy the three JSON files to the verification machine.
4. **Within one hour** (Redis deletes the data after 3600 s), compare each
   step:

```bash
for f in test2_b250M test2_b2500M test2_b0; do
  echo "=== $f"
  $PY reports/tools/verify_edge.py --src 129.57.178.85 --dest 129.57.178.86 compare \
      --iperf-json $f.json --served test2_frames.jsonl --busy-gbps 0.5 --per-second
done
```

5. For the NIC check, compare the whole sequence at once. Set `--start` to
   the first step's start − 3 and `--end` to the last step's end + 3 (the
   script prints both times). Set `--ref-bytes` to the **sum** of the three
   steps' `bytes received`:

```bash
$PY reports/tools/verify_edge.py --src 129.57.178.85 --dest 129.57.178.86 compare \
    --start <first-3> --end <last+3> --ref-bytes <sum> \
    --nic-bytes <delta> --nic-packets <delta>
```

The NIC delta covers everything received between the two readings, including
the idle gaps and any other traffic. Keep the time between the readings
short.

### 4. What to check on the dashboard

**Edge color is not a rate indicator.** The five colors are assigned on a log
scale between the smallest and largest edge **in the current frame**
(`trafficRange` in `ldrd2606_frontend/src/TrafficGraph.tsx`). The iperf3 edge
is the largest edge during every step, so it is red at 1 Gbps and at
100 Gbps alike. Check these instead:

| Check | Expected |
|---|---|
| Detail panel totals | follow the per-second Redis series, about 3 s behind |
| Legend ranges | the top range's upper bound ≈ the iperf3 edge's bytes per second at that step |
| Idle gaps | the edge **stays visible**, because about 700 B/s of background traffic flows from .85 to .86. Its total drops to KB, and it may lose its red color |
| Detail charts | a flat line at the step's rate, with no sawtooth or regular dropouts |

## Pass criteria

| Check | Pass when |
|---|---|
| `seconds present` | every second, in each step's window |
| `inconsistent seconds` | 0 |
| Mean rate, 1 Gbps step | within ±5% of the iperf3 sender Gbps |
| Mean rate, 10 Gbps step | within ±5% of the iperf3 sender Gbps |
| `unexplained` bytes, each step | within ±0.01% of the reference (at 1 Gbps, background traffic is a larger share, so allow up to ±0.05%) |
| NIC `unexplained`, whole sequence | a few hundred KB × the number of seconds between readings, or less |
| `value mismatches vs Redis` | 0 |
| `busy seconds never served` | 0 |
| Dashboard | every step and gap visible, no more than about 4 s late |

## Results

### iperf3 (client JSON)

| Step | `-b` per stream | Start (EDT) | Seconds | Bytes received | Sender Gbps | Retransmits |
|---|---|---|---|---|---|---|
| A | 250M | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| B | 2500M | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| C | unlimited | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |

### eBPF → Redis vs iperf3

| Step | Redis bytes | Counted packets | Mean / peak Gbps | eBPF − iperf3 | Unexplained | Missing s | Inconsistent s |
|---|---|---|---|---|---|---|---|
| A | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| B | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| C | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |

GRO merging factor (NIC wire packets ÷ counted packets), whole sequence:
_TBD_. It is expected to rise with rate.

### NIC counters, ebpf2203

| | RX bytes | RX packets | dropped |
|---|---|---|---|
| Before | _TBD_ | _TBD_ | _TBD_ |
| After | _TBD_ | _TBD_ | _TBD_ |
| Delta | _TBD_ | _TBD_ | _TBD_ |
| Unexplained vs iperf3 + headers | _TBD_ | | |

### Backend and dashboard

| Check | A | B | C |
|---|---|---|---|
| `/latest` frames captured | _TBD_ | _TBD_ | _TBD_ |
| Value mismatches vs Redis | _TBD_ | _TBD_ | _TBD_ |
| Busy seconds never served | _TBD_ | _TBD_ | _TBD_ |
| Frame lag, median / max | _TBD_ | _TBD_ | _TBD_ |
| Detail panel follows the rate | _TBD_ | _TBD_ | _TBD_ |
| Legend top range ≈ edge rate | _TBD_ | _TBD_ | _TBD_ |

### Per-second series

_TBD: paste a condensed `--per-second` table per step. Collapse runs of
steady seconds into a range, as Test 1 does._

## Verdict

_TBD_

## Observations

_TBD. In particular, record whether step C shows a step down like Test 1's
(56 → 19 Gbps), and the host state at the time._
