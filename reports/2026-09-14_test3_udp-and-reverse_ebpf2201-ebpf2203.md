# Test 3: UDP and reverse direction, ebpf2201 ↔ ebpf2203

**Status:** PLANNED, not yet run. Result cells marked _TBD_ are filled in
after the run.
**Date:** _TBD_
**Goal:** cover the two paths Tests 1 and 2 did not exercise:

- **3a, UDP forward** (ebpf2201 → ebpf2203). The bytes must be counted as
  `udp_*`, not `tcp_*`. The totals must match iperf3 once packet loss is
  accounted for.
- **3b, TCP reverse** (ebpf2203 → ebpf2201). The traffic must appear as a
  separate directed edge, counted by **ebpf2201's** collector, with totals
  matching iperf3. This is the first test of the second host's collector
  under load.

See [Test 1](2026-09-14_test1_ebpf2201-to-ebpf2203.md) for why eBPF bytes
exceed iperf3 bytes (headers) and why packet counts differ from the NIC's
(GRO merging).

## Setup

| | ebpf2201 | ebpf2203 |
|---|---|---|
| IPv4 | `129.57.178.85` | `129.57.178.86` |
| NIC | _TBD_ (`$IFACE` on this host), MTU _TBD_ | `enp1s0f1np1`, MTU 9000 |
| iperf3 role | client, in both 3a and 3b | server |
| Collector counts | 3b (receiver) | 3a (receiver) |

In **3b**, the client on ebpf2201 runs with `-R`, so the server on ebpf2203
does the sending. The receiver, and so the collector and NIC counters that
matter, is **ebpf2201**.

### Host state (record before running)

```bash
iperf3 --version | head -1
cpupower frequency-info -p 2>/dev/null | grep -i governor
sysctl net.core.rmem_max net.core.rmem_default
ip link show dev $IFACE | grep -o 'mtu [0-9]*'
sudo tc filter show dev $IFACE ingress | grep tc-ing        # hook attached
pgrep -a tc_collector                                        # collector running, note -p
```

| | ebpf2201 | ebpf2203 |
|---|---|---|
| iperf3 version | _TBD_ | _TBD_ |
| CPU governor | _TBD_ | _TBD_ |
| `rmem_max` / `rmem_default` | _TBD_ | _TBD_ |
| MTU | _TBD_ | _TBD_ |
| Hook attached / collector running (`-p`) | _TBD_ | _TBD_ |

## How UDP numbers reconcile

Each UDP packet has a 20 B IP header, an 8 B UDP header and 8948 B of data
(`-l 8948`), 8976 B in all. That fits a 9000 B MTU, so nothing is
fragmented. Consequences for the checks:

- **Headers:** use `--l3l4-header 28`. The script then expects
  Ethernet+IP+UDP = 42 B per packet on the NIC.
- **GRO factor ≈ 1.0 is expected.** iperf3 does not turn on UDP GRO, so
  counted packets should roughly equal wire packets. A factor well above 1
  is itself an observation worth recording.
- **Loss decides which reference applies.** iperf3 reports packets *sent*
  and *lost*. Where a packet was lost determines whether eBPF counted it:

  | Where the loss happened | Seen by eBPF? | How to tell |
  |---|---|---|
  | Before the NIC (wire, sender) | no | `ip -s link` RX packets < sent |
  | In the NIC (ring full) | no | `ethtool -S` `rx_out_of_buffer` / `rx_discards` rise |
  | After the hook (socket buffer full) | **yes** | `nstat` `UdpRcvbufErrors` rises |

  The script's reference is the data *received* by iperf3, so
  `unexplained` ≈ (packets lost after the hook) × 8948 B. If loss is 0,
  `unexplained` should be a few hundred KB at most, as in Test 1.

## Procedure

### 1. Before each sub-test

**On the receiver** (ebpf2203 for 3a, ebpf2201 for 3b), save the counters:

```bash
ip -s -s link show dev $IFACE
ethtool -S $IFACE | grep -iE 'out_of_buffer|discard' | grep -v ': 0'
nstat -az | grep -E 'UdpInDatagrams|UdpRcvbufErrors|UdpInErrors'
```

**On ebpf2203:** `iperf3 -s -B 129.57.178.86` (leave it running for both
sub-tests).

**On the verification machine,** start one recorder. It captures both
directions:

```bash
cd $ECENTER
PY=ld2606_daos_redis/traffic-simulator/venv/bin/python
$PY reports/tools/verify_edge.py --src 129.57.178.85 --dest 129.57.178.86 \
    record --out test3_frames.jsonl &
```

### 2. 3a: UDP forward (on ebpf2201)

```bash
iperf3 -c 129.57.178.86 -B 129.57.178.85 -u -b 5G -l 8948 -t 30 -J > test3a_udp_5G.json
```

Optional second run at a higher rate, to see where loss starts (`-b` is per
stream):

```bash
iperf3 -c 129.57.178.86 -B 129.57.178.85 -u -b 5G -l 8948 -t 30 -P 4 -J > test3a_udp_4x5G.json
```

Save the receiver counters again (step 1 commands, on ebpf2203) after
**each** run.

### 3. 3b: TCP reverse (on ebpf2201)

Save the counters on **ebpf2201** first (step 1 commands), then:

```bash
iperf3 -c 129.57.178.86 -B 129.57.178.85 -R -n 100G -P 4 -J > test3b_reverse.json
```

Save the ebpf2201 counters again afterwards.

### 4. Compare (within one hour)

Stop the recorder (`kill %1`) and copy the JSON files to the verification
machine.

**3a**, edge `.85 → .86`, UDP headers:

```bash
$PY reports/tools/verify_edge.py --src 129.57.178.85 --dest 129.57.178.86 compare \
    --iperf-json test3a_udp_5G.json --l3l4-header 28 \
    --nic-bytes <delta> --nic-packets <delta> \
    --served test3_frames.jsonl --per-second
```

**3b**, edge `.86 → .85`. Note that `--src` and `--dest` are **swapped**:

```bash
$PY reports/tools/verify_edge.py --src 129.57.178.86 --dest 129.57.178.85 compare \
    --iperf-json test3b_reverse.json \
    --nic-bytes <ebpf2201 delta> --nic-packets <ebpf2201 delta> \
    --served test3_frames.jsonl --per-second
```

The recorder was started with `--src .85 --dest .86`, but it saves both
directions, so the same frames file works for 3b.

For 3a, the script's `iperf3 JSON` block prints both `data bytes sent` and
`data bytes received`. If `unexplained` is large and positive, compare it
with (lost packets × 8948) and the `UdpRcvbufErrors` delta.

### 5. What to check on the dashboard

| Check | 3a | 3b |
|---|---|---|
| Edge drawn | ebpf2201 → ebpf2203 | ebpf2203 → ebpf2201, a **separate** edge from the forward one |
| Detail panel protocol split | UDP charts carry the traffic; TCP near zero (iperf3's control connection only) | TCP carries the traffic |
| Reverse-direction edge | nothing new (UDP has no ACKs) | ebpf2201 → ebpf2203 shows ACK traffic, a few MB/s |

Edge color is relative to the other edges in the frame; see Test 2, step 4.

## Pass criteria

| Check | 3a UDP | 3b reverse |
|---|---|---|
| `seconds present` | all | all |
| `inconsistent seconds` | 0 | 0 |
| `tcp / udp bytes` | UDP ≥ 99.99% of the total | UDP = 0 |
| `unexplained` vs iperf3 | within ±0.01%, **or** equal to losses after the hook (see table above) | within ±0.01% |
| NIC `unexplained` | a few hundred KB | a few hundred KB |
| GRO factor | ≈ 1.0 (record the value) | > 1 (record the value) |
| `value mismatches vs Redis` | 0 | 0 |
| `busy seconds never served` | 0 | 0 |
| Dashboard | edge and protocol split as in step 5 | separate reverse edge as in step 5 |

## Results

### 3a: UDP forward

| iperf3 | 5G × 1 | 5G × 4 (optional) |
|---|---|---|
| Start (EDT) | _TBD_ | _TBD_ |
| Packets sent | _TBD_ | _TBD_ |
| Lost packets / % | _TBD_ | _TBD_ |
| Data bytes received | _TBD_ | _TBD_ |
| Receiver Gbps | _TBD_ | _TBD_ |

| eBPF → Redis, `.85 → .86` | 5G × 1 | 5G × 4 |
|---|---|---|
| Redis bytes (TCP / UDP) | _TBD_ | _TBD_ |
| Counted packets | _TBD_ | _TBD_ |
| Mean / peak Gbps | _TBD_ | _TBD_ |
| eBPF − iperf3 received | _TBD_ | _TBD_ |
| Expected headers (× 28 B) | _TBD_ | _TBD_ |
| Unexplained | _TBD_ | _TBD_ |
| Missing / inconsistent seconds | _TBD_ | _TBD_ |

| Receiver counters, ebpf2203 (delta) | 5G × 1 | 5G × 4 |
|---|---|---|
| NIC RX bytes / packets | _TBD_ | _TBD_ |
| NIC unexplained (× 42 B) | _TBD_ | _TBD_ |
| GRO factor | _TBD_ | _TBD_ |
| `rx_out_of_buffer` / `rx_discards` | _TBD_ | _TBD_ |
| `UdpRcvbufErrors` | _TBD_ | _TBD_ |

### 3b: TCP reverse

| | Value |
|---|---|
| iperf3 bytes received / Gbps / retransmits | _TBD_ |
| Redis bytes, `.86 → .85` (TCP / UDP) | _TBD_ |
| Counted packets / mean / peak Gbps | _TBD_ |
| eBPF − iperf3, expected headers, unexplained | _TBD_ |
| NIC RX delta on ebpf2201, unexplained, GRO factor | _TBD_ |
| ebpf2201 collector samples per second (`sps`) | _TBD_ |
| ACK edge `.85 → .86` bytes / packets | _TBD_ |
| Missing / inconsistent seconds | _TBD_ |

### Backend and dashboard

| Check | 3a | 3b |
|---|---|---|
| `/latest` frames captured | _TBD_ | _TBD_ |
| Value mismatches vs Redis | _TBD_ | _TBD_ |
| Busy seconds never served | _TBD_ | _TBD_ |
| Frame lag, median / max | _TBD_ | _TBD_ |
| Detail panel as expected | _TBD_ | _TBD_ |

### Per-second series

_TBD: a condensed `--per-second` table for each run._

## Verdict

_TBD_

## Observations

_TBD_
