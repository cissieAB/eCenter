# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Known bugs and open work are tracked in [TODO.md](TODO.md). Several items there are
load-bearing for anything described below — in particular, two of the three eBPF
kernel objects do not currently compile.

## System Design

A **real-time, multi-node network traffic monitoring and visualization system** for JLab LDRD 100Gbps testbeds.

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
                        Redis Stack (shared, remote)
                        HSET packet:{dest_ip}:{source_ip}:{ts}
                        TTL = 3600s · RediSearch index idx:packets
                                     ↓
                       Go backend (1s ticker)         DAOS drain worker
                       FT.AGGREGATE/FT.SEARCH          (drains Redis → DAOS)
                       → directed graph
                                     ↓ WebSocket /ws
                                  Browser
                          ┌────────────────────────┐
                          │ Graph view             │
                          │ (Cytoscape.js,         │
                          │  edge = active flow,   │
                          │  color = total_bytes)  │
                          │                        │
                          │ hover/click edge →     │
                          │ Detail panel           │
                          │ (inline SVG charts:    │
                          │  tcp/udp bytes/pkts)   │
                          └────────────────────────┘
```

**What each producer writes per second** — one Redis **hash** per directed flow per second,
key `packet:{dest_ip}:{source_ip}:{timestamp}`, TTL 3600s:

| Field | Type | Notes |
|---|---|---|
| `timestamp` | int | Unix seconds; indexed and sortable |
| `source_ip`, `dest_ip` | string | dotted-quad IPv4 |
| `samples_per_second` | int | array length; the collector's poll Hz |
| `total_packets`, `total_bytes` | int | `total_bytes` is indexed |
| `tcp_bytes`, `tcp_packets`, `udp_bytes`, `udp_packets` | JSON array string | one entry per sub-second poll tick |

Note the key puts **dest before source**, while every JSON/HTTP payload orders them
`src` then `dest`. All three producers/consumers agree on this
(`tc_userspace.cpp`, `simulator_v3.py`, `backend/edge_history.go`) — it is easy to
get backwards when writing new code against it.

**Key architectural decisions:**
- `tc_collector` writes to Redis directly via hiredis — not a stdout sidecar (multi-NIC nodes would corrupt a shared pipe via `PIPE_BUF` interleaving)
- Each NIC has a unique IP, so keys never collide across collector instances on one node
- Flows are **directed** — `A→B` and `B→A` are separate keys and separate graph edges, no merging
- The backend polls Redis every 1s (not pub/sub) — at 1s granularity, push offers no latency benefit, and polling keeps the backend stateless
- The timestamp is **in the key**, so history is queried by time range rather than accumulated in backend memory. This is what makes `/history` replay possible, and it is why the DAOS drain's delete-after-archive behavior conflicts with the UI (see TODO.md §A)

---

## Repository Overview

Three independent sub-projects, each a git submodule with its own `.git` history:

- **`dpu-telemetry-eBPF/`** — eBPF DPU network traffic counter (C kernel + C++ userspace) · [github.com/JeffersonLab/dpu-telemetry-eBPF](https://github.com/JeffersonLab/dpu-telemetry-eBPF)
- **`ld2606_daos_redis/`** — Go backend + Python traffic simulator + DAOS drain worker · [github.com/cissieAB/ld2606_daos_redis](https://github.com/cissieAB/ld2606_daos_redis)
- **`ldrd2606_frontend/`** — React + Vite + Cytoscape.js dashboard · [github.com/cissieAB/ldrd2606_frontend](https://github.com/cissieAB/ldrd2606_frontend) (originally RaiqaRasool/ldrd2606_frontend)

Clone with `git clone --recurse-submodules <url>`. The parent repo holds only
`README.md`, `CLAUDE.md`, `TODO.md`, and `docs/`.

Setup walkthroughs live in [docs/setup.md](docs/setup.md) and
[docs/guide_real-traffic.md](docs/guide_real-traffic.md).

---

## Project 1: dpu-telemetry-eBPF

### Architecture

Kernel-space eBPF programs (C) attach to network interfaces via TC or XDP hooks and
populate pinned BPF LRU hash maps. A C++ userspace collector (`tc_collector`) polls
those maps at a configurable frequency, computes per-interval deltas, and writes one
Redis hash per directed flow per second.

```
NIC → TC ingress/egress or XDP hook (eBPF kernel, C) → BPF LRU hash map (pinned)
                                                              ↓
                                          tc_collector (C++, userspace poller)
                                            ├─ 60-slot ring buffer, 1 slot/second
                                            └─ hiredis pipelined HSET + EXPIRE
```

### Layout

| Path | Contents |
|---|---|
| `eCounter/v1_userspace-poll/` | The working implementation — all kernel programs, the C++ collector, CMake build, sample data |
| `eCounter/wip_ringbuf/` | Incomplete ringbuf experiment (a single stub file) |
| `simple-demos/` | Minimal standalone examples: `demo_ringbuf/`, `demo_traffic-control/` |
| `scripts/` | pktgen, iperf3, MTU/ring/XPS tuning, and plotting helpers |
| `docs/` | Traffic-collection notes, network headers, pktgen and iperf3 guides |

Further ringbuf and per-CPU-map work lives on the `ring_buf` and `per-core-map`
remote branches, not in the working tree.

### Build & Run

```bash
cd dpu-telemetry-eBPF/eCounter/v1_userspace-poll

# Compile the eBPF kernel objects (auto-detects aarch64 include path)
./compile_kernel.sh
# Intended output: kernel_ingress_tc.o, kernel_egress_tc.o, kernel_ingress_xdp.o
# CURRENTLY BROKEN: only kernel_ingress_tc.c compiles; the egress and XDP
# programs still use the pre-rename single-`ip` key field. See TODO.md §A.

# Attach TC hooks (requires root)
sudo tc qdisc add dev <iface> clsact
sudo tc filter add dev <iface> ingress bpf da obj kernel_ingress_tc.o sec tc-ing
sudo tc filter add dev <iface> egress  bpf da obj kernel_egress_tc.o  sec tc-eg

# Attach XDP hook (alternative; MTU ≤ 3498 for driver mode, else it falls back
# to generic mode, which is slower than TC)
sudo ip link set dev <iface> xdp obj kernel_ingress_xdp.o sec xdp-ing

# Build the C++ collector (needs libbpf, hiredis, pthread)
cmake -B build && cmake --build build

# Run
sudo ./build/tc_collector
sudo ./build/tc_collector -p 100 -m /sys/fs/bpf/map_in_xdp --redis-host redis-host -v
```

**CLI flags** (`parse_args` in `tc_userspace.cpp`):

| Flag | Default | Notes |
|---|---|---|
| `-p`, `--poll-hz` | `20` | Must be a positive **divisor of 1000000** — 3, 7, and 300 are rejected |
| `-m`, `--map-path` | `/sys/fs/bpf/tc-eg` | Pinned map to poll |
| `--redis-host` | `localhost` | |
| `--redis-port` | `6379` | |
| `--redis-ttl` | `3600` | Seconds; must be positive |
| `-v`, `--verbose` | off | Not listed by `print_usage` |

Any unrecognized argument prints usage and exits 1.

### Data Structures (`tc_common.h`)

```c
struct traffic_key_t {
    __u32 source_ip;        // network byte order
    __u32 destination_ip;   // network byte order
    __u8  proto;            // IPPROTO_TCP / IPPROTO_UDP only
    __u8  pad[3];           // BPF verifier requires 4-byte key alignment
};
struct traffic_val_t { __u64 packets; __u64 bytes; };
```

Map type `BPF_MAP_TYPE_LRU_HASH`, `max_entries` 2048. Note this key is
(src, dst, proto) — cardinality is squared relative to the older single-IP key,
so 2048 entries is likely undersized for a 100G testbed (TODO.md §A).

Byte counts come from `bpf_ntohs(ip->tot_len)`, i.e. L3 and above — the Ethernet
header is not counted.

### Collector behavior

- Maintains a 60-slot ring buffer, one slot per second, indexed by `timestamp % 60`.
- Each poll tick writes **absolute** counters into bin `polling_id` of the current slot.
- At each second boundary a reporter thread diffs consecutive bins against a
  per-edge `last_seen`, emits only edges with a non-zero delta, writes them to
  Redis, and zeroes the slot.
- Edges whose counters go backwards (LRU eviction, restart) are detected but not
  reliably distinguishable from genuine resets — see the `@bug` comments in
  `get_diff_vector`.

### Key Files

| File | Purpose |
|------|---------|
| `kernel_ingress_tc.c` | TC ingress hook (map `map_in_tc`, section `tc-ing`) |
| `kernel_egress_tc.c` | TC egress hook (map `map_out_tc`, section `tc-eg`) |
| `kernel_ingress_xdp.c` | XDP ingress hook (map `map_in_xdp`, section `xdp-ing`) |
| `tc_userspace.cpp` | C++ collector: polls the map, ring buffer, diffing, Redis writes |
| `tc_common.h` | Shared `traffic_key_t` / `traffic_val_t` |
| `json.hpp` | Vendored nlohmann/json |
| `compile_kernel.sh` | Clang build for all three kernel objects |
| `CMakeLists.txt` | C++17 build; links `bpf`, `pthread`, `hiredis` |

---

## Project 2: ld2606_daos_redis

### Architecture

```
simulator_v3.py (or tc_collector)
  └─ HSET packet:{dest}:{src}:{ts}  → Redis Stack
                                        ↓  FT.AGGREGATE / FT.SEARCH on idx:packets
                                  Go Backend
                                    ├─ 1s poller → selected live frame (RWMutex)
                                    ├─ GET  /latest   → snapshot
                                    ├─ GET  /edge     → one edge, live or historical
                                    ├─ GET  /history  → paginated frames
                                    └─ WS   /ws       → snapshot + live stream
                                        ↓
                            redis_daos_drain.py → DAOS
```

Redis must be **Redis Stack** (or otherwise have the RediSearch module): the
backend creates and queries the `idx:packets` index and will not work against
plain Redis.

### Backend (Go)

```bash
cd ld2606_daos_redis/backend
go run .
go build -o backend .
go test ./...
```

**Module**: `go 1.25.5`; deps `gorilla/websocket v1.5.3`, `redis/go-redis/v9 v9.17.3`.

**Environment** (`config.go`):

| Var | Default | Notes |
|---|---|---|
| `REDIS_ADDR` | `localhost:6379` | |
| `REDIS_DB` | `0` | Malformed values are silently ignored |
| `SERVER_PORT` | `:8080` | Passed straight to `ListenAndServe` — **the colon is required** |
| `POLL_INTERVAL` | `1s` | Any `time.ParseDuration` string |
| `TOPOLOGY_PATH` | `config/topology.json` | Startup fails if unreadable or invalid |
| `DEBUG` | unset | `true` or `1` |

There is no `REDIS_CHANNEL` and no pub/sub — that path was replaced by RediSearch polling.

**HTTP endpoints**:

| Route | Behavior |
|---|---|
| `GET /latest` | `{type, data}` snapshot of the current live frame |
| `GET /edge?src=&dest=` | Full sample arrays for one directed edge, latest frame |
| `GET /edge?src=&dest=&timestamp=` | Same, read directly from the `packet:` key at that second |
| `GET /history?start=&end=&limit=` | Paginated `HistoryFrame` list; `limit` 1–120, default 60 |
| `GET /ws` | Sends `{type:"snapshot", topology, data}` first, then a snapshot per poll |
| `GET /` | Health check — currently answers 200 for *any* unmatched path |

Only the initial WebSocket message carries `topology`; the frontend keeps it for
the life of the connection.

**Topology** is a static JSON file, not Redis-derived. `loadTopology` validates
that every key is a real IPv4 address matching its `ip` field and that `rack` is
non-empty. `simulator_v3.py` *also* writes `topology:node:{ip}` hashes into Redis,
but nothing reads them — a duplicated source of truth (TODO.md §B).

**Concurrency**: an RWMutex guards the `latest` frame (read-heavy); a separate
Mutex guards the WebSocket client map; a buffered channel (size 100) decouples
polling from fan-out and drops snapshots rather than blocking when full.

**Source files**: `main.go`, `config.go`, `state.go`, `types.go`, `utils.go`,
`topology.go`, `redis.go`, `redis_index.go`, `redis_document.go`, `live.go`,
`history.go`, `edge_history.go`, `handlers.go`, `websocket.go`, `broadcast.go`.

### Traffic Simulator (Python)

**Use `simulator_v3.py`.** It is the only version matching the current backend
contract — one aggregated record per directed edge per second, plus the topology
registration the older versions lack. `simulator_bk.py` and `simulator_v2.py`
predate the aggregation change (they write one key *per packet*, timestamp in
milliseconds) and are kept only for reference.

```bash
cd ld2606_daos_redis/traffic-simulator
./setup.sh && source venv/bin/activate

python simulator_v3.py --redis-host localhost --nodes 8 --duration 999999
```

Flags: `--redis-host/--redis-port/--redis-db`, `--nodes` (1–255),
`--nodes-per-rack`, `--samples-per-second`/`--sps`, `--duration`, `--ttl`,
`--stats-interval`.

Node *n* is assigned IP `192.168.110.{n+1}`, racked as `rack-{n // nodes_per_rack + 1}`.
These must line up with `backend/config/topology.json` or the endpoints render as
`external`. One `HPCNode` thread per node emits to every other node, so the write
rate is `N*(N-1)` records/second.

`traffic_simulator_mpi.py` is a separate distributed variant (`mpirun -n <N>`).

**Python deps**: `redis>=5.0.0`, `flask>=3.0.0`.

### DAOS drain worker

`daos-client/redis_daos_drain.py` scans `packet:*`, bulk-writes to a DAOS DDict via
`pydaos`, then **deletes the drained keys from Redis**. It needs DAOS client
libraries on `LD_LIBRARY_PATH`/`PYTHONPATH`; `--dry-run` skips the DAOS write but
still deletes.

```bash
python3 redis_daos_drain.py --daos-pool telemetry_pool --daos-cont telemetry
```

The delete-after-archive design is in direct tension with the backend's `/history`
endpoint, which reads the same keys. Resolve before running the two together
(TODO.md §A).

### Local stack (Docker Compose)

```bash
cd ld2606_daos_redis
docker compose -f compose.dev.yaml --profile tools up -d
docker compose -f compose.dev.yaml exec -d simulator \
  python simulator_v3.py --redis-host redis --nodes 8 --duration 999999
```

Three services: `redis` (redis-stack-server, healthchecked), `backend`
(`go run .` on `:8080`, hot-reloaded from a volume mount), and `simulator`
(idles on `sleep infinity` behind the `tools` profile — a shell to `exec` into,
not a generator on its own). The frontend is **not** in this compose file; run it
separately.

---

## Project 3: ldrd2606_frontend

React 19 + Vite + TypeScript, graph rendering by **Cytoscape.js**. There is no
charting library — the per-edge detail plots are hand-rolled inline SVG in
`TrafficGraph.tsx`. Nothing is loaded from a CDN.

```bash
cd ldrd2606_frontend
npm install
npm run dev      # Vite dev server, proxies /edge /history /ws → localhost:8080
npm test         # tsx --test src/*.test.ts
npm run lint     # oxlint
npm run build    # tsc -b && vite build
```

The dev server proxies to `localhost:8080`; `vite.config.local.ts` is an untracked
scratch variant pointing at `:8090`.

### Source map

| File | Purpose |
|---|---|
| `data-contract.ts` | Runtime validators for every payload — `parseGraphMessage`, `parseEdgeDetail`, `parseHistoryPage`. Strict: a page failing any invariant is rejected whole |
| `use-graph-stream.ts` | WebSocket lifecycle, snapshot/update merge, 1s reconnect |
| `use-paginated-history.ts` | `/history` paging, frame merge, loaded-range tracking |
| `history-client.ts` | Typed `/history` fetch |
| `history-playback.ts` | Scrubber math, 40-minute replay window |
| `edge-detail-loader.ts` | Debounced `/edge` fetch with per-`src:dest:timestamp` cache |
| `TrafficGraph.tsx` | Cytoscape setup, host/rack view modes, traffic-intensity filter, SVG detail charts |
| `App.tsx` | Live/history mode, playback transport |

Each of these has a colocated `*.test.ts` (49 tests total).

### Views

- **Host view** — one node per topology IP; endpoints absent from the topology are folded into a single `external` node by `collapseExternalSummaries`.
- **Rack view** — edges aggregated by rack; intra-rack traffic is dropped.
- **Layout** — Cytoscape `circle` layout, re-run only when the node set changes.
- **Edge color** — five-bucket log-scaled gradient over `total_bytes`; the legend doubles as a filter.
- **Detail panel** — hover previews, click pins. Non-aggregate edges fetch `/edge` for the full sub-second arrays and render two SVG charts (packet counts, byte totals).
- **History mode** — a 40-minute replay window scrubbed at 1 frame/second, paged from `/history` on demand.

`docs/data-contract.md` and `docs/graph-behavior.md` in that submodule document the
contract and interaction rules in more detail.

---

## Remaining feature gaps

1. **Multi-NIC** — the design assumes one collector per NIC with distinct IPs, but nothing in the simulator, topology model, or UI represents a node as a *set* of NIC IPs. One IP is one host throughout.
2. **Egress collection** — not working end to end; the egress kernel program does not compile and the collector opens a single map path.
3. **DAOS read path** — the drain archives data but nothing serves it back, so history is bounded by what survives in Redis.
4. **Observability** — no metrics on drop counts, poll overruns, Redis write latency, or client count. The collector measures write latency and discards it.
5. **CI** — none in any of the four repositories.
