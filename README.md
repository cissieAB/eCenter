# eCenter

Real-time multi-node network traffic monitoring and visualization for JLab LDRD 100Gbps testbeds.

See `CLAUDE.md` for the full architecture design and decisions.

For backend, Redis, and React frontend setup using simulated traffic or the TC ingress collector, see the [setup guide](docs/setup.md).

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

There are two different meanings of "latest" with submodules, and mixing them up is the
usual source of confusion:

- **The pinned versions** — the commits the parent repo records as known-good together.
  This is what you want almost always.
- **The upstream tips** — whatever is newest on each submodule's own `main`. Use this
  when you deliberately want to move the project forward to newer sub-project code.

### Get the pinned versions (the common case)

```bash
git pull
git submodule update --init --recursive
```

`git pull` updates the parent repo — including the recorded submodule pointers — but it
does **not** touch the submodule working trees. The second command is what actually moves
each submodule to the commit the parent just recorded, and initializes any submodule added
since your last pull. Run both, always.

To make that automatic for future pulls:

```bash
git config --global submodule.recurse true
```

With that set, `git pull` alone also checks out the recorded submodule commits.

### Advance to the upstream tips

```bash
git submodule update --remote --recursive
```

This ignores the recorded pointers and fetches each submodule's default branch tip. The
submodules then differ from what the parent tracks, so `git status` reports them as
modified and `git submodule status` prefixes them with `+`. That is expected — record the
new pointers to finish the job:

```bash
git add dpu-telemetry-eBPF ld2606_daos_redis ldrd2606_frontend
git commit -m "bump submodules to latest upstream"
git push
```

Until you commit that, the bump exists only on your machine.

For a single submodule:

```bash
git submodule update --remote --merge ld2606_daos_redis
```

### Pull while you have local work in a submodule

`git submodule update` checks out a specific commit and leaves the submodule in **detached
HEAD**, which will discard uncommitted work there. If you have local changes in a
submodule, commit or stash them inside that submodule first, then pull on a real branch:

```bash
cd ld2606_daos_redis
git checkout main          # attach to a branch first
git pull
cd ..
```

### Check submodule status

```bash
git submodule status
```

- `-` prefix → not initialized yet (`git submodule update --init`)
- `+` prefix → the checked-out commit differs from what the parent records → needs a bump commit
- `U` prefix → merge conflicts inside the submodule
- no prefix → in sync

`git diff --submodule` shows which commits the pointer moved across.

### Also refresh dependencies

Submodule code moving does not update anything installed from it:

```bash
cd ldrd2606_frontend && npm install          # after a frontend bump
cd dpu-telemetry-eBPF/eCounter/v1_userspace-poll && cmake --build build   # after a collector bump
```

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

### Pull upstream changes into a submodule

See [Advance to the upstream tips](#advance-to-the-upstream-tips) above — the bump commit
in the parent is the part people forget.
