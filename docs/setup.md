# Traffic monitoring setup

This guide covers the Go backend, Redis Stack, React frontend, and either the V2 simulator or the `eCounter/v1_userspace-poll` TC ingress collector.

Run Redis, the backend, and the frontend on the same machine. For real telemetry, run the kernel program and userspace collector on each Linux node whose incoming traffic you want to observe. That can include the machine hosting Redis.

Choose one traffic source for a run:

- **Simulator:** generated records → Redis → backend → frontend.
- **Real telemetry:** incoming IPv4 TCP/UDP traffic → TC ingress map → userspace collector → Redis → backend → frontend.

## Directories and prerequisites

Replace all `<...>` placeholders before running commands. Commands that change directory use absolute placeholders so they work with either separate clones or eCenter submodules.

| Placeholder | eCenter checkout | Separate checkout |
| --- | --- | --- |
| `<backend-repo>` | `eCenter/ld2606_daos_redis` | Your `ld2606_daos_redis` directory |
| `<frontend-repo>` | `eCenter/ldrd2606_frontend` | Your frontend directory, which may be named `frontend` |
| `<telemetry-repo>` | `eCenter/dpu-telemetry-eBPF` | Your `dpu-telemetry-eBPF` directory |

For an existing eCenter clone, initialize the recorded submodule versions from its root:

```bash
git submodule update --init --recursive
```

On the central machine, install Docker with Compose or Podman with a Compose provider, plus Node.js and npm compatible with the frontend's Vite version. Go and Python dependencies are installed inside the development images.

For a DNF-based Linux system where Podman is preferred, the installation used by this workflow is:

```bash
sudo dnf install -y podman podman-docker podman-compose
```

Package names and availability depend on the distribution and enabled repositories. `podman-docker` provides Docker command compatibility; the commands below use `podman` explicitly. [Podman Compose delegates to an external Compose provider](https://docs.podman.io/en/latest/markdown/podman-compose.1.html).

## Option A: Simulated traffic

In the first terminal, start Redis, the backend, and the simulator container:

```bash
cd <backend-repo>
docker compose -f compose.dev.yaml --profile tools up --build
```

Keep this terminal running. The simulator container starts idle; it does not generate records until you run the script. In a second terminal:

```bash
cd <backend-repo>
docker compose -f compose.dev.yaml exec simulator python3 simulator_v2.py --redis-host redis --duration 3600 --mode 1
```

[Compose exec targets the service name](https://docs.docker.com/reference/cli/docker/compose/exec/), so looking up a container ID is unnecessary. To enter a shell instead, use `docker compose -f compose.dev.yaml exec simulator /bin/bash`, then run the same Python command from `/app`. The image already installs dependencies globally; no virtual environment activation is needed inside it.

Use `--redis-host redis` because `redis` is the Compose service name; the script defaults to `localhost`, which would refer to the simulator container itself. `--duration 3600` runs for one hour; the script default is 10 seconds. Mode 1 stores the hashes consumed by the backend. The default five simulated nodes use `192.168.110.0` through `192.168.110.4`; ensure those IPs appear in the backend topology.

The simulator clears existing `packet:*` records in its selected Redis database when starting. Use this workflow separately from real telemetry when its records need to be retained.

For Podman, use `podman compose` in place of `docker compose` in both commands. Continue with [Start the frontend](#start-the-frontend).

## Option B: Real telemetry

### 1. Start central services

On the machine that will also run the frontend:

```bash
cd <backend-repo>
podman compose -f compose.dev.yaml up
```

Use `docker compose -f compose.dev.yaml up` if using Docker. Omit the `tools` profile; real telemetry does not need the simulator. Keep this terminal running.

Compose publishes Redis on port `6379` and the backend on `8080`. Collectors need a hostname or IP that reaches this machine on port `6379`; the internal Compose hostname `redis` is only for containers on that network. Use the Redis machine's reachable address even when observed traffic uses a different data-network interface.

### 2. Install collector prerequisites on each monitored node

The TC collector requires Linux with BPF/TC support, administrator privileges to attach programs and access maps, Clang/LLVM with a BPF target, Linux headers, libbpf development files, `bpftool`, `tc`, CMake, a C++17 compiler, a build tool, and hiredis development files.

For DNF-based systems, an example is:

```bash
sudo dnf install -y clang llvm libbpf-devel bpftool iproute kernel-headers cmake gcc-c++ make hiredis-devel
```

On other Linux distributions, install the equivalent packages using that distribution's package manager. The BPF filesystem must be mounted at `/sys/fs/bpf` before pinning the map.

### 3. Compile and attach TC ingress

On each monitored node, set its interface and map path in the terminal you will use for the following steps:

```bash
cd <telemetry-repo>/eCounter/v1_userspace-poll
ip link show
TELEMETRY_IFACE='<interface>'
TELEMETRY_MAP_PATH='/sys/fs/bpf/tc-ing'
clang -O2 -g -target bpf -c kernel_ingress_tc.c -o kernel_ingress_tc.o
```

Select the interface carrying the traffic you want to measure. If compilation cannot find architecture-specific headers such as `asm/types.h`, install the appropriate headers and add their actual include directory with `-I`; do not copy another machine's architecture path.

Inspect existing TC state first:

```bash
sudo tc qdisc show dev "$TELEMETRY_IFACE"
sudo tc filter show dev "$TELEMETRY_IFACE" ingress
```

If `clsact` is absent, add it:

```bash
sudo tc qdisc add dev "$TELEMETRY_IFACE" clsact
```

Attach the program once, then verify it:

```bash
sudo tc filter add dev "$TELEMETRY_IFACE" ingress bpf da obj kernel_ingress_tc.o sec tc-ing
sudo tc filter show dev "$TELEMETRY_IFACE" ingress
sudo bpftool map show name map_in_tc
```

The section `tc-ing` and map name `map_in_tc` come from `kernel_ingress_tc.c`. If this program is already attached, inspect the existing attachment instead of adding another copy. See the [TC BPF reference](https://man7.org/linux/man-pages/man8/tc-bpf.8.html) for attachment options.

### 4. Pin the map for userspace

For a single map named `map_in_tc`:

```bash
sudo bpftool map pin name map_in_tc "$TELEMETRY_MAP_PATH"
sudo bpftool map show pinned "$TELEMETRY_MAP_PATH"
sudo bpftool map dump pinned "$TELEMETRY_MAP_PATH"
```

If multiple maps have that name, identify the map belonging to the intended attached program and pin it by ID instead of name. If the pin path already exists, verify that it points to the current attachment's map before reusing it. Use a distinct pin path for each interface when monitoring more than one.

### 5. Build and run the userspace collector

In the same terminal and directory:

```bash
cmake -S . -B build
cmake --build build
sudo ./build/tc_collector --redis-host <redis-host> --poll-hz 20 --map-path "$TELEMETRY_MAP_PATH"
```

Replace `<redis-host>` with the central machine's reachable hostname or IP. `--poll-hz 20` sets 20 samples per second. The default Redis port is `6379`; use `--redis-port` if needed. The map path must match the pin created above.

For later runs, the existing binary can be started directly:

```bash
cd <telemetry-repo>/eCounter/v1_userspace-poll
sudo ./build/tc_collector --redis-host <redis-host> --poll-hz 20 --map-path /sys/fs/bpf/tc-ing
```

Adjust the map path if you chose a different one. Rebuild after source changes. Keep each collector running while observing traffic; attaching the program alone does not send records to Redis.

## Start the frontend

On the same machine as the backend, in another terminal:

```bash
cd <frontend-repo>
npm install
npm run dev
```

Install dependencies on first setup and after dependency changes. Open the local URL printed by the development server. The existing proxy forwards `/ws`, `/edge`, and `/history` to `localhost:8080`; no proxy change is needed for this setup.

## Topology and verification

Configure `backend/config/topology.json` in the backend repository with the traffic's source/destination IPs and rack assignments. For real telemetry, these are the addresses observed in packets, which may differ from the Redis machine's management address. Optional `hostname` values are display labels; IPs remain node identities. Restart the backend and reconnect the frontend after topology changes.

From the backend repository, check central services with the engine used to start them:

```bash
docker compose -f compose.dev.yaml ps
docker compose -f compose.dev.yaml exec redis redis-cli ping
curl http://localhost:8080/latest
```

For Podman, substitute `podman compose`. Expect `PONG` from Redis. Check backend and collector terminal output for connection or record errors. For real telemetry, generate IPv4 TCP/UDP traffic into the selected interface and inspect the pinned map to confirm counters change. The simulator generates Redis records directly and does not exercise TC.

If the graph remains empty, check the producer is running, Redis is reachable from it, records use the expected IPs, and topology contains those IPs. If a collector cannot open its map, verify the pin path and attachment. An empty map points to the interface or incoming traffic; changing map counters with no Redis records points to the collector or Redis connection.

## Stop a run

Stop the simulator or each userspace collector with `Ctrl+C`, then stop the frontend with `Ctrl+C`. Stop the foreground Compose process with `Ctrl+C`. Existing Redis data remains subject to its TTL.

Stopping a collector leaves the TC attachment and map pin in place for reuse. For full telemetry teardown, inspect the interface's filters and remove only the attachment and pin created for this run. Remove `clsact` only if it was created for this run and no other filters need it. Avoid deleting all ingress filters on a shared interface.
