# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Target System Design

The goal is a **real-time, multi-node network traffic monitoring and visualization system** for JLab LDRD 100Gbps testbeds. This is the full intended architecture — current code covers parts of it; the gaps are what remain to be built.

```
 Node A                     Node B                     Node C
 ┌─────────────────┐        ┌─────────────────┐        ┌─────────────────┐
 │ NIC0  NIC1  ... │        │ NIC0  NIC1  ... │        │ NIC0  NIC1  ... │
 │  ↓     ↓        │        │  ↓     ↓        │        │  ↓     ↓        │
 │ eBPF  eBPF      │        │ eBPF  eBPF      │        │ eBPF  eBPF      │
 │  ↓     ↓        │        │  ↓     ↓        │        │  ↓     ↓        │
 │ tc_collector    │        │ tc_collector    │        │ tc_collector    │
 │ (hiredis write) │        │ (hiredis write) │        │ (hiredis write) │
 └────────┬────────┘        └────────┬────────┘        └────────┬────────┘
          └──────────────────────────┼──────────────────────────┘
                                     ↓
                             Redis (shared, remote)
                             SET traffic:{src_ip}:{dst_ip}
                             TTL = 3600s
                                     ↓
                          Go viz backend (1s ticker)          DAOS backend service
                          SCAN traffic:* → directed graph      (drains Redis → DAOS)
                                     ↓ WebSocket
                                  Browser
                          ┌────────────────────────┐
                          │ Topo view              │
                          │ (directed graph,       │
                          │  edge = active flow,   │
                          │  weight = total_bytes) │
                          │                        │
                          │ click edge →           │
                          │ Detail view            │
                          │ (time-series plots:    │
                          │  tcp/udp bytes/pkts)   │
                          └────────────────────────┘
```

**What each node writes per second** (one Redis key per active `src→dst` flow, TTL 3600s):
```json
{
  "total_bytes": 1024,
  "tcp_bytes":    [...],
  "udp_bytes":    [...],
  "tcp_packets":  [...],
  "udp_packets":  [...]
}
```

**Key architectural decisions:**
- `tc_collector` writes to Redis directly via hiredis — not stdout sidecar (multi-NIC nodes would corrupt a shared pipe)
- Each NIC has a unique IP, so `traffic:{src_ip}:{dst_ip}` keys never collide across instances
- Flows are **directed** — `A→B` and `B→A` are separate edges, no merging
- Viz backend polls Redis every 1s (not pub/sub) — at 1s granularity, polling keeps the backend stateless; Redis TTL handles expiry automatically

**Gaps to build:**
1. hiredis write integration in `tc_collector`
2. Go viz backend (replaces/extends `ld2606_daos_redis/backend`)
3. Browser viz (topo view + detail view)

---

## Repository Overview

This is a multi-project JLab LDRD research repository with two independent sub-projects, each with its own `.git` history:

- **`dpu-telemetry-eBPF/`** — eBPF-based DPU network traffic counter (C/C++, kernel + userspace) · [github.com/JeffersonLab/dpu-telemetry-eBPF](https://github.com/JeffersonLab/dpu-telemetry-eBPF)
- **`ld2606_daos_redis/`** — Real-time traffic monitoring pipeline with Redis pub/sub, WebSocket streaming, and DAOS storage (Go backend + Python simulator + DAOS client) · [github.com/cissieAB/ld2606_daos_redis](https://github.com/cissieAB/ld2606_daos_redis)
- **`viz/`** *(planned, new)* — Go viz backend + browser UI (vis.js + uPlot); the only component that does not belong to either sub-project

Both sub-projects are intended to be **git submodules** of a parent `eCenter` repo. Clone with `git clone --recurse-submodules <url>`. New viz code lives directly in the parent repo under `viz/`.

---

## Project 1: dpu-telemetry-eBPF

### Architecture

Kernel-space eBPF programs (C) attach to network interfaces via TC (Traffic Control) or XDP hooks and populate pinned BPF LRU hash maps. A C++ userspace collector (`tc_collector`) polls those maps at a configurable frequency, computes per-interval deltas, and emits JSON time-series metrics per source/destination IP.

```
NIC → TC ingress/egress or XDP hook (eBPF kernel, C) → BPF LRU hash map (pinned)
                                                              ↓
                                          tc_collector (C++, userspace poller)
                                            └─ 60-second ring buffer → JSON output
```

Main code lives in `traffic_counter/`. Experimental ringbuf work is on the `ring_buf` and `per-core-map` branches. `simple-tc-demo/` has minimal standalone examples.

### Build & Run

```bash
cd traffic_counter

# Compile all three eBPF kernel objects (auto-detects aarch64)
./compile_kernel.sh
# Produces: kernel_ingress_tc.o, kernel_egress_tc.o, kernel_ingress_xdp.o

# Attach TC hooks (requires root)
sudo tc qdisc add dev <iface> clsact
sudo tc filter add dev <iface> ingress bpf da obj kernel_ingress_tc.o sec tc-ing
sudo tc filter add dev <iface> egress  bpf da obj kernel_egress_tc.o  sec tc-eg

# Attach XDP hook (alternative, requires MTU ≤ 3498 for driver mode)
sudo ip link set dev <iface> xdp obj kernel_ingress_xdp.o sec xdp-ing

# Build the C++ userspace collector
cmake -B build && cmake --build build
# Produces: build/tc_collector

# Run (default: 20 Hz polling, map at /sys/fs/bpf/tc-eg)
sudo ./build/tc_collector
sudo ./build/tc_collector -p 4000 -m /sys/fs/bpf/map_in_xdp -v
```

**CLI flags**: `-p|--poll-hz FREQ` (10–4000), `-m|--map-path PATH`, `-v|--verbose`

### Data Structures (`tc_common.h`)

```c
struct traffic_key_t { __u32 ip; __u8 proto; __u8 pad[3]; };  // map key
struct traffic_val_t { __u64 packets; __u64 bytes; };           // map value
```

Map type: `BPF_MAP_TYPE_LRU_HASH`, 2048 max entries. Kernel programs track ingress by **source IP**, egress by **destination IP**.

### JSON Output Format

60-second sliding ring buffer, one snapshot per second, keyed by Unix timestamp then IP (as integer):

```json
{"1763113319": {"2449791234": {"tcp_bytes": [...], "tcp_packets": [...], "udp_bytes": [...], "udp_packets": [...]}}}
```

### Key Files

| File | Purpose |
|------|---------|
| `kernel_ingress_tc.c` | TC ingress hook — counts by source IP (map `map_in_tc`, section `tc-ing`) |
| `kernel_egress_tc.c` | TC egress hook — counts by dest IP (map `map_out_tc`, section `tc-eg`) |
| `kernel_ingress_xdp.c` | XDP ingress hook — counts by source IP (map `map_in_xdp`, section `xdp-ing`) |
| `tc_userspace.cpp` | C++ collector: polls map, maintains ring buffer, outputs JSON |
| `tc_common.h` | Shared `traffic_key_t` / `traffic_val_t` structs |
| `compile_kernel.sh` | Clang compilation script for all three kernel objects |
| `CMakeLists.txt` | C++17 build config, links libbpf and pthread |

### Redis Integration (planned)

`tc_collector` will write directly to Redis via **hiredis** (not via stdout pipe to a sidecar).

**Rationale**: A node may run multiple `tc_collector` instances (one per NIC). Merging their stdout streams into a shared sidecar risks JSON corruption — Linux `PIPE_BUF` (4096 bytes) makes concurrent writes from multiple processes non-atomic. Direct hiredis gives each instance an independent connection with no coordination needed.

**Key schema**: `traffic:{src_ip}:{dst_ip}` — no interface field needed because each NIC has a unique IP, so keys are naturally non-overlapping across instances.

**Write pattern**: Pipeline all IP-pair writes for a given 1-second window in one `redisAppendCommand` / `redisGetReply` round-trip. Set TTL = 3600s — keys must survive long enough for the DAOS backend service to drain them from Redis into DAOS. No pub/sub — the viz backend polls directly (see Visualization section below).

---

## Project 2: ld2606_daos_redis

### Architecture

```
Traffic Simulator (Python)
  └─ publishes JSON packets → Redis Pub/Sub channel
                                  ↓
                           Go Backend Server
                             ├─ aggregates latest state (RWMutex)
                             ├─ HTTP GET /latest  → snapshot
                             └─ WebSocket /ws     → live stream
```

DAOS client (`daos-client/`) is a placeholder — not yet implemented.

### Backend (Go)

```bash
cd ld2606_daos_redis/backend

# First-time setup
./setup.sh

# Run (defaults: Redis at localhost:6379, port 8080)
go run .

# With overrides
DEBUG=true REDIS_ADDR=localhost:6379 SERVER_PORT=8080 REDIS_CHANNEL=traffic_channel go run .

# Build binary
go build -o backend .
```

**Go module**: `go 1.25.5`, deps: `gorilla/websocket v1.5.3`, `go-redis/v9 v9.17.3`

**HTTP endpoints**: `GET /` (health), `GET /latest` (latest JSON), `GET /ws` (WebSocket)

**Concurrency design** (see `backend/PROJECT_SUMMARY.md`): RWMutex guards the `latest` state (read-heavy); a separate Mutex guards the WebSocket clients map; a buffered channel (size 100) decouples pub/sub ingestion from WebSocket fan-out.

**Key source files**: `config.go`, `redis.go`, `websocket.go`, `handlers.go`, `types.go`, `main.go`

### Traffic Simulator (Python)

```bash
cd ld2606_daos_redis/traffic-simulator

# First-time setup (creates venv)
./setup.sh
source venv/bin/activate

# Run basic simulation (1 node, 100 pps, Redis storage enabled)
python simulator_bk.py --redis-host localhost

# Run V2 (recommended for ejfat-5/6 nodes)
python simulator_v2.py --redis-host localhost --nodes 5 --packets-per-second 500 --duration 60

# MPI version for distributed simulation
mpirun -n <N> python traffic_simulator_mpi.py
```

**Python deps**: `redis>=5.0.0`, `flask>=3.0.0` (see `requirements.txt`)

**Simulator versions**: `simulator_bk.py` (original), `simulator_v2.py` (two modes, tested on cluster), `traffic_simulator_mpi.py` (distributed)

### Redis Data Format

Packets are stored as hashes with key `packet:{dest_ip}:{source_ip}:{timestamp}` (TTL: 1 hour) and published as JSON to the configured channel:

```json
{
  "timestamp": 1770147907,
  "source_ip": "192.168.45.123",
  "dest_ip": "10.0.78.234",
  "total_bytes": 1024,
  "udp_packets": [...],
  "udp_bytes": [...],
  "tcp_packets": [...],
  "tcp_bytes": [...]
}
```

---

## Planned Visualization (new component)

### Architecture

```
tc_collector (hiredis) → SET traffic:{src_ip}:{dst_ip} {data} EX 3600
                                        ↓
                          Go viz backend (1s ticker)
                            └─ SCAN traffic:* → build directed graph
                            └─ WebSocket push → browsers
                                        ↓
                          Browser
                            ├─ Topo view: directed graph of active flows
                            └─ Detail view: click edge → fine-grained time-series plots
```

### Key Decisions

**Q1 — Redis write method**: `tc_collector` writes directly via hiredis (not stdout sidecar). Reason: nodes have multiple NICs, each running a separate instance; stdout pipe merging causes `PIPE_BUF` corruption.

**Q2 — Viz refresh method**: Go backend polls Redis directly every 1s (not pub/sub). Reason: update granularity is already 1s, so pub/sub push offers no latency benefit; direct polling keeps the backend stateless (Redis TTL handles expiry, no in-memory accumulation needed).

**Q3 — Browser viz stack**: [vis.js Network](https://visjs.github.io/vis-network/) for the topology graph + [uPlot](https://github.com/leeoniya/uPlot) for time-series plots. Both loaded via CDN, no build toolchain. GSAP rejected (animation library, not a graph/charting library; overkill for a monitoring tool).

### Redis Key Schema (planned)

`traffic:{src_ip}:{dst_ip}` → JSON value with `total_bytes`, `tcp_bytes[]`, `udp_bytes[]`, `tcp_packets[]`, `udp_packets[]`. TTL = 3600s (keys must outlive the DAOS drain window).

Flows are **directed** (`A→B` and `B→A` are separate keys). No client-side deduplication.

### Views

- **Topo view**: rack-aligned layout — server boxes drawn manually via vis.js `beforeDrawing` canvas API; each server box shows NIC ports (NIC0 blue, NIC1 green) as vis.js nodes inside the box. Flow arcs curve to the right with varying roundness so they never overlap. Arc color encodes `total_bytes` on a continuous blue→amber→red gradient; arc width also scales with traffic.
- **Detail view**: click a flow arc → two uPlot charts: bytes/s (TCP + UDP) and packets/s (TCP + UDP), 60-second scrolling history.

A working prototype of this UI lives in `demo.html` at the repo root (mock data, no real Redis).

### Simulator Gaps (`ld2606_daos_redis/traffic-simulator/simulator_v2.py`)

The simulator partially covers the data-producer role but has four concrete gaps vs the target design:

| # | What | Current | Target |
|---|------|---------|--------|
| 1 | **Key schema** | `packet:{destIP}:{srcIP}:{timestamp_ms}` — timestamp baked into the key, one key **per packet** | `traffic:{src_ip}:{dst_ip}` — no timestamp, one key **per flow per second** (aggregated) |
| 2 | **Write pattern** | Writes every individual packet (100 pps = 100 keys/s/node); no aggregation | Accumulate all packets for a given `(src, dst)` pair over 1s, write one aggregate key at the end of the second |
| 3 | ~~**TTL**~~ | ~~3600s (1 hour)~~ | ~~2s~~ | **Not a gap** — TTL stays 3600s; a DAOS backend service will drain Redis keys into DAOS before expiry |
| 4 | **Multi-NIC** | Each simulated node has one IP (`192.168.{node//256}.{node%256}`); no NIC concept | Each node has N NICs, each with a distinct IP — the src/dst IPs in the key must be NIC IPs, not node IDs |
| 5 | **Bins** | `tcp_bytes[]`, `udp_bytes[]`, etc. are random static arrays of size `--bin-no` | Bins are the actual sub-second samples accumulated during the 1s window (length = number of eBPF poll ticks within that second) |

**What does not need to change**: the threading model (`HPCNode` per node), Redis pipeline batching, and the `SimulatorConfig` / `SimulationController` structure are all reusable.
