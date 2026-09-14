# Reports

Dated end-to-end tests of eCenter on the `ebpf` testbed. Each test sends known
traffic with `iperf3` and checks every stage of the pipeline against it:
eBPF counter → `tc_collector` → Redis → backend → dashboard. For how to set up
the pipeline itself, see [docs/guide_real-traffic.md](../docs/guide_real-traffic.md).

## Test reports

| Report | What it tests | Status |
|---|---|---|
| [Test 1](2026-09-14_test1_ebpf2201-to-ebpf2203.md) | One 100 GiB TCP transfer, ebpf2201 → ebpf2203. Totals, per-second series, and backend frames checked against iperf3 and the receiver's NIC counters. | **Pass**. Also records an open issue: throughput stepped down from 56 to 19 Gbps partway through |
| [Test 2](2026-09-14_test2_rate-steps_ebpf2201-to-ebpf2203.md) | TCP steps at about 1 Gbps, 10 Gbps and unlimited. Checks that the per-second rate and the dashboard follow changes in rate. | Planned |
| [Test 3](2026-09-14_test3_udp-and-reverse_ebpf2201-ebpf2203.md) | 3a: UDP forward (counted as `udp_*`, with loss accounted for). 3b: TCP reverse, ebpf2203 → ebpf2201, which exercises ebpf2201's collector. | Planned |

Planned reports are runbooks: commands, pass criteria, and empty results
tables (_TBD_). They are filled in after the run.

## Tools

| File | Purpose |
|---|---|
| [tools/verify_edge.py](tools/verify_edge.py) | `record` saves what the backend's `/latest` endpoint serves for one edge. `compare` reads that edge's per-second Redis hashes and checks them against iperf3 (`--iperf-json`), the NIC counters, and the recorded frames. It needs the `redis` Python package; the simulator venv has it. |

Redis deletes the data after 3600 s, so run `compare` within an hour of the
test.

## Adding a report

- Name it `YYYY-MM-DD_testN_<what>_<hosts>.md`.
- Start from the closest existing report.
- Add a row to the table above, and update its status when results are in.
