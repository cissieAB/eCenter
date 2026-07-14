# eCenter

Real-time multi-node network traffic monitoring and visualization for JLab LDRD 100Gbps testbeds.

See `CLAUDE.md` for the full architecture design and decisions.

## Repository Structure

```
eCenter/
├── dpu-telemetry-eBPF/   # eBPF traffic counter (submodule → JeffersonLab/dpu-telemetry-eBPF)
├── ld2606_daos_redis/    # Redis backend + simulator + DAOS client (submodule → cissieAB/ld2606_daos_redis)
├── ldrd2606_frontend/    # React + Vite + Cytoscape.js dashboard (submodule → RaiqaRasool/ldrd2606_frontend)
└── CLAUDE.md             # Architecture documentation
```

## Clone

Always clone with `--recurse-submodules` to pull all three sub-projects:

```bash
git clone --recurse-submodules https://github.com/cissieAB/eCenter.git
```

If you already cloned without it:

```bash
git submodule update --init
```

## Running the Full Stack Locally

This launches the `ldrd2606_frontend` dashboard talking live to the `ld2606_daos_redis` Go backend, fed by `simulator_v3.py`, backed by Redis Stack — all via the project's own dev Compose file, plus one terminal for the frontend.

### 0. Prerequisites

- Docker (or Podman) with the daemon running — Redis and the backend run in containers, so no local Go toolchain is required
- Node.js + npm (for the frontend only)

### 1. Start Redis + backend

```bash
cd ld2606_daos_redis
docker compose -f compose.dev.yaml --profile tools up -d
```

This starts three containers: `redis` (Redis Stack, healthchecked), `backend` (`go run .` on `:8080`, hot-reloaded from `./backend` via a volume mount), and `simulator` (idles on `sleep infinity` — a shell to `exec` into, not a traffic generator itself). The `--profile tools` flag is required or the `simulator` container won't be created at all.

Check backend logs: `docker compose -f compose.dev.yaml logs -f backend`. Expect `Index 'idx:packets' created successfully` then `Starting server on :8080`.

### 2. Feed live traffic — use `simulator_v3.py`, not `simulator_bk.py`

```bash
docker compose -f compose.dev.yaml exec -d simulator python simulator_v3.py --redis-host redis --nodes 8 --duration 999999
```

This matters more than it looks: only `simulator_v3.py` registers the `topology:nodes` / `topology:node:<ip>` keys that `GET /ws` needs to build its node topology. The frontend **silently drops every edge whose endpoints aren't in that topology** (see `ldrd2606_frontend/docs/data-contract.md`) — so running `simulator_bk.py` or `simulator_v2.py` instead will make the backend report real data (`/latest` populated, poll logs show `pairs=N`) while the dashboard graph stays empty, with no error anywhere to explain why.

Also note `simulator_v3.py` has **no `--publish` flag and no infinite-duration mode** — `--duration` must be an explicit number of seconds (minimum 1), so pass something long-running like `999999` for an interactive session rather than relying on a default that stops after 10s. The backend doesn't need pub/sub anyway; it polls Redis directly every second.

Verify topology + data landed:
```bash
docker exec ld2606_daos_redis-redis-1 redis-cli SMEMBERS topology:nodes
curl -s http://localhost:8080/latest | head -c 300
```

### 3. Terminal — Frontend

```bash
cd ldrd2606_frontend
npm install          # first time, or after a submodule pull
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/ws`, `/edge`, and `/history` to `http://localhost:8080` — no env config needed.

### Stopping everything

```bash
# Ctrl+C in the frontend terminal, then:
cd ld2606_daos_redis
docker compose -f compose.dev.yaml --profile tools down
```

`down` removes the containers and network; the `redis-data`/`go-mod-cache`/`go-build-cache` named volumes persist until you add `-v`.

## Pull Latest Updates

### Update everything at once

```bash
# Pull parent repo changes + advance all submodules to their latest remote commit
git pull
git submodule update --remote --merge
```

### Update a specific submodule only

```bash
git submodule update --remote --merge dpu-telemetry-eBPF
# or
git submodule update --remote --merge ld2606_daos_redis
# or
git submodule update --remote --merge ldrd2606_frontend
```

### Check submodule status

```bash
git submodule status
```

A `-` prefix means not yet initialized. A `+` prefix means the checked-out commit differs from what the parent repo tracks (i.e., you have a newer or older commit locally).

## How to Work with This Repo

The parent repo stores a **commit pointer** (SHA) for each submodule, not the code itself. Any time a submodule's commit advances — whether you made the change or pulled from upstream — you need to record the new pointer in the parent with a commit. This keeps the parent always tracking exactly which versions of the sub-projects work together.

### Make changes inside a submodule

Submodules start in **detached HEAD** state. Check out a branch before making changes:

```bash
cd dpu-telemetry-eBPF
git checkout main          # or whichever branch you want
# ... make changes ...
git commit -am "your change"
git push
cd ..
git add dpu-telemetry-eBPF # stage the new commit pointer
git commit -m "bump dpu-telemetry-eBPF to <sha>"
git push
```

### Pull upstream changes to a submodule

```bash
git submodule update --remote --merge dpu-telemetry-eBPF
git add dpu-telemetry-eBPF
git commit -m "bump dpu-telemetry-eBPF to <sha>"
git push
```

### Check what needs recording

```bash
git submodule status
```

- `-` prefix → submodule not initialized yet (`git submodule update --init`)
- `+` prefix → submodule is ahead of what the parent tracks → needs a bump commit
- no prefix → in sync
