# Simulated-traffic guide

This guide runs the whole eCenter stack on one machine with synthetic traffic from
`simulator_v3.py` instead of eBPF collectors. For real traffic on the testbed, see
[guide_real-traffic.md](guide_real-traffic.md).

## Local Test

This launches the `ldrd2606_frontend` dashboard talking live to the `ld2606_daos_redis`
Go backend, fed by `simulator_v3.py`, backed by Redis Stack. Everything runs from the
project's own dev Compose file, plus one terminal for the frontend.

```
simulator_v3.py → Redis Stack → Go backend (:8080) → Vite dev server (:5173) → browser
```

### 0. Prerequisites

- Docker (or Podman) with the daemon running. Redis, the backend, and the simulator run
  in containers, so no local Go or Python toolchain is required.
- Node.js + npm, for the frontend only.
- Initialized submodules: `git submodule update --init` from the eCenter root.

### 1. Start Redis + backend

```bash
cd ld2606_daos_redis
docker compose -f compose.dev.yaml --profile tools up -d
```

This starts three containers:

- `redis`: Redis Stack, healthchecked. Each launch starts with an empty database; the
  previous run's data is first moved to `ld2606_daos_redis/redis-archive/<UTC timestamp>/`.
- `backend`: `go run .` on `:8080`, hot-reloaded from `./backend` via a volume mount.
- `simulator`: idles on `sleep infinity`. It is a shell to `exec` into, not a traffic
  generator on its own.

The `--profile tools` flag is required, or the `simulator` container is not created.

Check the backend logs:

```bash
docker compose -f compose.dev.yaml logs -f backend
```

Expect `Starting server on :8080`. On a fresh Redis you will also see
`Index 'idx:packets' created successfully`.

### 2. Feed live traffic with `simulator_v3.py`

```bash
docker compose -f compose.dev.yaml exec -d simulator \
  python simulator_v3.py --redis-host redis --nodes 8 --duration 999999
```

- **Use `simulator_v3.py`.** It is the only simulator that writes the current backend
  contract: one aggregated hash per directed edge per second. `simulator_bk.py` and
  `simulator_v2.py` write one key per packet and are kept for reference only.
- **`--redis-host redis`** is the Compose service name. The default, `localhost`, would
  point at the simulator container itself.
- **`--duration` must be an explicit number of seconds** (minimum 1; default 10). Pass a
  large value such as `999999` for an interactive session.
- **The simulator deletes existing `packet:*` keys when it starts.** Do not point it at a
  Redis instance holding real telemetry you want to keep.

Other flags: `--nodes` (1–255, default 5), `--nodes-per-rack` (default 4),
`--samples-per-second`/`--sps` (default 100), `--ttl` (default 3600),
`--stats-interval`, `--redis-port`, `--redis-db`.

#### Node IPs must match the topology

Simulated node *n* gets IP `192.168.110.{n+1}`, so `--nodes 8` uses `192.168.110.1`
through `192.168.110.8`. The backend's node list comes from the static file
`ld2606_daos_redis/backend/config/topology.json`, which already covers `192.168.110.0`–`.8`.

Endpoints missing from that file are intentionally folded into a single `external` node,
which represents traffic to or from hosts outside the monitored topology. Its edges are
kept, not dropped. If you raise `--nodes` above 8, the extra simulated nodes appear under
`external`. To show them as individual hosts, add their IPs to `topology.json` and
restart the backend (`docker compose -f compose.dev.yaml restart backend`).

The simulator also writes `topology:node:<ip>` hashes into Redis, but the backend does
not read them.

#### Verify data landed

```bash
docker compose -f compose.dev.yaml exec redis redis-cli --scan --pattern 'packet:*' | head
curl -s http://localhost:8080/latest | head -c 300
```

Keys have the form `packet:{dest_ip}:{source_ip}:{timestamp}`. Note that the destination
comes first.

### 3. Start the frontend

In another terminal:

```bash
cd ldrd2606_frontend
npm install          # first time, or after a submodule pull
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/ws`, `/edge`, and `/history` to
`http://localhost:8080`, so no environment configuration is needed.

### Stop everything

```bash
# Ctrl+C in the frontend terminal, then:
cd ld2606_daos_redis
docker compose -f compose.dev.yaml --profile tools down
```

`down` removes the containers and network. The `redis-data`, `go-mod-cache`, and
`go-build-cache` named volumes persist until you add `-v`.

For Podman, use `podman compose` in place of `docker compose` throughout.
