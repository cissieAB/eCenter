# Real-traffic workflow with eCenter

This guide runs one measurement of real IPv4 TCP/UDP traffic on the `ebpf`
testbed using an eCenter checkout: the TC ingress collector on **each**
monitored node, and Redis, the backend, and the frontend on one central node.
The same steps work for any set of hosts; substitute your own names and IPs.

## The `ebpf` testbed

| Node | Traffic IPv4 | Role | Status |
| --- | --- | --- | --- |
| `ebpf2203` | `129.57.178.86` | Central (Redis, backend, frontend) **and** monitored | Active |
| `ebpf2201` | `129.57.178.85` | Monitored | Active |
| `ebpf2202` | — | Monitored | Not yet working; not in the topology |

The topology for this testbed is
`$ECENTER/ld2606_daos_redis/backend/config/topology.ebpf.json`, which lists the
two active nodes. When `ebpf2202` comes online, add it there (step 1), update
the table above, and run its hook in step 4 like the others.

Each host's ingress hook counts traffic **incoming** to that host, keyed by
source IP. Running the collector on every host that should appear in the
graph captures traffic in both directions between every pair:

- `ebpf2203`'s collector records `<any host> → ebpf2203` traffic.
- `ebpf2201`'s collector records `<any host> → ebpf2201` traffic.
- `ebpf2202`'s collector, once it is online, records `<any host> → ebpf2202` traffic.

Every collector writes to the same central Redis, so the backend sees the
full set of directed edges across all monitored hosts.

## Per-run workflow

Section 0 is one-time setup. Steps 1–6 are done **in this order for every
run**:

```
1. Define topology       central host   write a topology file for this run
        ↓
2. Central services      central host   Redis (starts empty) + backend loaded with that topology
        ↓
3. Frontend + tunnel     central host   Vite in a node:22 container (no npm on the host),
                         + your laptop  SSH tunnel; dashboard shows every host, no edges yet
        ↓
4. Launch eBPF hooks     each node      attach TC ingress, pin map, start tc_collector
        ↓
5. Traffic + verify      any node       edges appear between the named nodes
        ↓
6. Stop and clean up     each node      stop collector, detach hook, unpin map, confirm map deleted
```

The order matters:

- The backend reads the topology **only at startup**, and the browser
  receives it **only when it connects**. So the topology file must exist
  before the backend starts, and the backend must be up before the frontend.
- Starting the frontend before any hook is attached gives a known-good
  baseline — every host drawn, no edges — so any edge that appears afterwards
  comes from this run's collectors.
- Cleaning up in step 6 means the next run's step 4 starts from a clean
  kernel state: no leftover filter, no stale pin, no second map named
  `map_in_tc` for `bpftool map pin name` to trip over.

Each launch of Redis starts with an empty database; the previous run's data is
archived to `$ECENTER/ld2606_daos_redis/redis-archive/YYYYMMDD-HHMMSS/` first.

## Paths used in this guide

All commands refer to the eCenter checkout through `$ECENTER`. Set it in
**every** shell you use, on every host:

```bash
export ECENTER=/path/to/eCenter          # e.g. $HOME/eCenter
```

| Component | Path |
| --- | --- |
| Redis, backend, topology files | `$ECENTER/ld2606_daos_redis` |
| Frontend | `$ECENTER/ldrd2606_frontend` |
| eBPF kernel program and collector | `$ECENTER/dpu-telemetry-eBPF/eCounter/v1_userspace-poll` |

If eCenter is not yet cloned on a host:

```bash
git clone --recurse-submodules https://github.com/cissieAB/eCenter.git $ECENTER
```

If it is cloned but the submodules are empty or stale:

```bash
cd $ECENTER && git submodule update --init --recursive
```

## 0. One-time setup and known host quirks

Run all steps as the account that will later run `sudo`, using `bash`. If
your login shell defaults to something else (e.g. `tcsh`), start `bash`
first so the commands below work as written.

On every monitored host, note the NIC whose incoming traffic you want to
count; steps 4 and 6 use it:

```bash
export IFACE=enp1s0f1np1                 # this host's NIC
ip -4 -o addr show $IFACE                # its IPv4 address, needed in step 1
```

Three environment-specific blockers were hit while validating this workflow
and may recur on any monitored host:

1. **`hiredis-devel` not installed and no root to `dnf install` it.** Build
   hiredis from source into a user-writable prefix instead:
   ```bash
   git clone --depth 1 https://github.com/redis/hiredis.git /tmp/hiredis-src
   cd /tmp/hiredis-src
   make PREFIX=$HOME/.local-build install
   ```
2. **NFS home directories mounted with `root_squash`.** If `$HOME` (and so
   `$ECENTER`) is on NFS, `sudo` cannot execute (or even traverse into)
   anything under it — you'll see `sudo: unable to execute ...: Permission
   denied` even though the file's permission bits look correct. Build/copy
   the final `tc_collector` binary and its `libhiredis.so*` onto **local**
   disk (e.g. `/scratch/<user>/`, check with `mount | grep scratch`), and
   relink with an RPATH pointing there:
   ```bash
   mkdir -p /scratch/$USER/tc_collector_build/lib
   cp $HOME/.local-build/lib/libhiredis.so* /scratch/$USER/tc_collector_build/lib/
   cd $ECENTER/dpu-telemetry-eBPF/eCounter/v1_userspace-poll
   g++ -std=c++17 -O2 -I$HOME/.local-build/include tc_userspace.cpp \
       -o /scratch/$USER/tc_collector_build/tc_collector \
       -L/scratch/$USER/tc_collector_build/lib -lbpf -lhiredis -pthread \
       -Wl,-rpath,/scratch/$USER/tc_collector_build/lib
   ```
   The same applies to the kernel object in step 4: if `sudo tc` cannot read
   `kernel_ingress_tc.o`, copy it to local disk and pass that path to `obj`.
   Also note `/tmp` is commonly mounted `noexec` on these hosts — never build
   or run the final binary there.
3. **`CapEff` is all-zero for normal users and
   `sysctl kernel.unprivileged_bpf_disabled` is `2`.** Attaching the TC
   program, pinning and unpinning the map, and running the collector against
   a pinned map all require `sudo`. There is no way around this short of the
   host owner granting `CAP_BPF`/`CAP_NET_ADMIN`.

Each monitored host builds and runs its own `tc_collector` independently.
Without the quirks above, the standard build is:

```bash
cd $ECENTER/dpu-telemetry-eBPF/eCounter/v1_userspace-poll
cmake -B build && cmake --build build     # binary: ./build/tc_collector
```

For the dashboard (step 3), two more one-time items:

- **No `npm` on `ebpf2203`.** `node`/`npm` are not installed and need root to
  install. Step 3 runs the frontend inside the `docker.io/library/node:22`
  image instead; confirm it is present with `podman images node`, or fetch it
  once with `podman pull docker.io/library/node:22`.
- **SSH jump path from your computer.** `ebpf2203` is reachable only via
  `scilogin.jlab.org`. Add the `Host ebpf2203` entry from
  step 3.1 to `~/.ssh/config` on your computer and check it with
  `ssh ebpf2203 hostname`.

## 1. Define this run's topology

On the central node (`ebpf2203`), select the topology file for this run. For
the `ebpf` testbed that is `topology.ebpf.json`:

```bash
cd $ECENTER/ld2606_daos_redis
export RUN=ebpf                           # selects backend/config/topology.$RUN.json
cat backend/config/topology.$RUN.json
```

It currently contains the two active nodes:

```json
{
  "nodes": {
    "129.57.178.86": { "ip": "129.57.178.86", "rack": "ebpf", "hostname": "ebpf2203" },
    "129.57.178.85": { "ip": "129.57.178.85", "rack": "ebpf", "hostname": "ebpf2201" }
  }
}
```

(The real file spreads each node over several lines; the content is the same.)

The frontend has **no topology file of its own** — it renders whatever
topology the backend sends, and any traffic IP missing from this file is
collapsed into a single generic `external` node. So the file must list
**every** monitored node, keyed by the IPv4 address its traffic uses — the
address from `ip -4 -o addr show $IFACE` in section 0, which may differ from
its SSH address.

- **Adding `ebpf2202`:** run `ip -4 -o addr show $IFACE` on it and add an
  entry with `"rack": "ebpf"` and `"hostname": "ebpf2202"`.
- **A run with a different node set** (for example only `ebpf2201` and
  `ebpf2202`): copy the file under a new name rather than editing it, so the
  standard testbed file stays intact:
  ```bash
  cp backend/config/topology.ebpf.json backend/config/topology.ebpf-2201-2202.json
  export RUN=ebpf-2201-2202
  ```

`ip` must match the key exactly; `rack` groups nodes in the frontend's Rack
view (hosts on the same physical rack share a value); `hostname` is only a
display label. The file must be **JSON** — the backend rejects anything else,
including unknown or misspelled fields. Full rules:
`$ECENTER/ld2606_daos_redis/backend/config/README.md`.

## 2. Start central services with that topology

On `ebpf2203`, in the same shell (so `$RUN` is still set):

```bash
cd $ECENTER/ld2606_daos_redis
export TOPOLOGY_PATH=config/topology.$RUN.json
```

`TOPOLOGY_PATH` is relative to the backend's working directory, hence
`config/...` rather than `backend/config/...`. Export it rather than prefixing
a single command: every later `up` must see the same value, or compose treats
the configuration as changed and recreates the containers.

On `ebpf2203`, `docker` is Podman's Docker emulation and `docker compose`
runs `podman-compose` (hence the `Emulate Docker CLI using podman` banner).
Its behavior differs from Docker Compose in one way that matters here: it
hashes the **whole** project configuration, so changing `TOPOLOGY_PATH`
makes `up` tear down and recreate **every** container, Redis included — not
just the backend.

### 2.1 Check for services left from an earlier run

```bash
docker compose -f compose.dev.yaml ps
podman ps -a --filter name=ld2606_daos_redis --format '{{.ID}}  {{.Names}}  {{.Status}}'
podman inspect ld2606_daos_redis_backend_1 \
    --format '{{range .Config.Env}}{{println .}}{{end}}' | grep TOPOLOGY_PATH
```

The last command shows which topology the existing backend was created with.
Then pick one:

| What you see | Do |
| --- | --- |
| No containers | [2.2 Fresh start](#22-fresh-start) |
| Containers exist, `TOPOLOGY_PATH` matches `config/topology.$RUN.json` | [2.3 Reuse](#23-reuse-existing-services) (or 2.4 if you prefer a clean slate) |
| Containers exist with a **different** `TOPOLOGY_PATH` | [2.4 Kill all](#24-kill-all-and-start-fresh), then 2.2 |
| An earlier `up` failed part-way (errors below), or the state is unclear | [2.4 Kill all](#24-kill-all-and-start-fresh), then 2.2 |

Include the `simulator` container in this check. It is only started with
`--profile tools`, but once started it stays up (`sleep infinity`) and
depends on Redis, so it blocks any teardown that doesn't also remove it. If
a generator was ever `exec`'d into it, it may also still be writing
simulated `192.168.110.*` traffic into the Redis you are about to use for
real traffic.

A failed recreate looks like this — `podman-compose` could not remove the old
Redis container (`8b53...`) because another container (`78b3...`, here the
simulator) still depends on it, then tried to create a new Redis under the
same name:

```
Error: container 8b53... has dependent containers which must be removed before it: 78b3...
Error: not all containers could be removed from pod 4f76...: removing pod containers
Error: "ld2606_daos_redis_default" has associated containers with it. ...: network is being used
Error: creating container storage: the container name "ld2606_daos_redis_redis_1" is already in use by 8b53...
```

After this the old Redis (with the previous run's data) may still be running
next to a new or old backend. Do not continue from that state; use 2.4.

Containers from **other** projects (for example a `node:22` container) are not
part of this stack. Leave them alone unless they hold port 6379 or 8080, which
the check below catches.

Also check that nothing **outside** this compose project holds port 6379 or
8080 — a host Redis service, or containers started under `sudo podman`, which
a normal `podman ps` does not list:

```bash
ss -ltnp | grep -E ':(6379|8080)\b'
sudo podman ps -a --format '{{.Names}}  {{.Ports}}'
systemctl is-active redis redis-stack-server 2>/dev/null
```

Stop whatever you find (`sudo systemctl stop redis`, `sudo podman rm -f <name>`)
before starting this project's services.

### 2.2 Fresh start

```bash
docker compose -f compose.dev.yaml up -d redis backend
```

### 2.3 Reuse existing services

Only when the containers were created with **this run's** `TOPOLOGY_PATH`.
Restarting keeps the containers, pod, and network, and avoids the recreate
path entirely:

```bash
docker compose -f compose.dev.yaml restart redis     # archives the previous run's data, starts empty
docker compose -f compose.dev.yaml restart backend   # re-reads the topology file's contents
```

If they exist but are stopped (`Exited`), use `start` instead of `restart`;
Redis still archives and starts empty:

```bash
docker compose -f compose.dev.yaml start redis backend
```

Skip the Redis restart only if you deliberately want to keep the previous
run's data in this run. Reuse cannot switch to a different topology file:
the path is fixed when the backend container is created, so that case needs
2.4.

### 2.4 Kill all and start fresh

Try the normal teardown first. It removes the containers, the pod, and the
network, and **keeps** the volumes, so the previous run's Redis data is still
archived on the next launch:

```bash
docker compose -f compose.dev.yaml down
```

Plain `down` only removes services in the default profile, so it can leave
the `simulator` container (profile `tools`) running and still attached to
Redis. If `down` fails with `dependent containers` / `container state
improper` errors, or anything in this project is still listed afterwards,
force-remove. Dependents go first (simulator, backend), then Redis, then the
pod and network:

```bash
podman rm -f -t 0 ld2606_daos_redis_simulator_1 ld2606_daos_redis_backend_1 2>/dev/null
podman rm -f -t 0 ld2606_daos_redis_redis_1 2>/dev/null
podman ps -aq --filter name=ld2606_daos_redis | xargs -r podman rm -f -t 0   # anything else from this project
podman pod ls -q --filter name=ld2606_daos_redis | xargs -r podman pod rm -f
podman network rm ld2606_daos_redis_default 2>/dev/null
```

Confirm nothing is left, then start fresh with 2.2:

```bash
podman ps -a --filter name=ld2606_daos_redis                              # expect only the header
podman pod ls --filter name=ld2606_daos_redis                              # expect only the header
docker compose -f compose.dev.yaml up -d redis backend
```

#### Example: a failed `up`, a leftover simulator, and an unrelated container

This is what `ebpf2203` looked like after the failed recreate shown in 2.1
and a first kill-all attempt that removed only Redis and the backend:

```
$ podman ps
CONTAINER ID  IMAGE                                         COMMAND         CREATED     STATUS      NAMES
78b313d13015  localhost/ld2606_daos_redis_simulator:latest  sleep infinity  4 days ago  Up 4 days   ld2606_daos_redis_simulator_1
656462714e15  docker.io/library/node:22                     ...
```

- `78b313d13015` is the container named in the original
  `has dependent containers ... 78b3...` error. It is the simulator,
  started days earlier with `--profile tools`, and it was what kept Redis
  from being removed. It belongs to this project: remove it with the
  force-remove block above, which removes it before Redis.
- `656462714e15` (`node:22`) is **not** from this project; nothing in
  `compose.dev.yaml` uses that image. Look at it before touching it:
  ```bash
  podman ps -a --filter id=656462714e15 --format '{{.Names}}  {{.Ports}}  {{.Command}}'
  ```
  If it is `ecenter-frontend` (step 3.2, option A) or another stale frontend or
  dev server holding port 5173, 6379, or 8080, remove it
  (`podman rm -f 656462714e15`). Otherwise leave it running.

Then confirm and start fresh as above. Start with `up -d redis backend` and
**no** `--profile tools`, so the simulator is not brought back into a
real-traffic run.

Do **not** add `-v` to `down` or remove the `ld2606_daos_redis_redis-data`
volume unless you want to discard the previous run's data: the archive to
`redis-archive/` happens when Redis next starts, so deleting the volume first
loses that run without archiving it.

### 2.5 Confirm before continuing

```bash
docker compose -f compose.dev.yaml exec redis redis-cli ping     # expect PONG
docker compose -f compose.dev.yaml exec redis redis-cli DBSIZE   # expect 0
docker compose -f compose.dev.yaml logs backend --tail 5
# expect: Loaded N topology nodes from config/topology.<run>.json
```

The backend compiles with `go run .` on start, so give it a few seconds
before the log line appears.

Then confirm the backend **serves** this run's topology — the check that
catches a backend quietly running the default `config/topology.json`
(simulator nodes `sim-node-00`, ...). Its WebSocket's first message carries
the topology; this prints the hostnames in it:

```bash
podman inspect ld2606_daos_redis_backend_1 \
    --format '{{range .Config.Env}}{{println .}}{{end}}' | grep TOPOLOGY_PATH
# expect: TOPOLOGY_PATH=config/topology.<run>.json

curl -s -m 3 --http1.1 -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
    -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
    http://localhost:8080/ws | strings | grep -o '"hostname":"[^"]*"' | sort -u
# expect, for RUN=ebpf, exactly:
# "hostname":"ebpf2201"
# "hostname":"ebpf2203"
```

**If it lists `sim-node-*` (or any node set other than this run's):** the
backend container was created without this run's `TOPOLOGY_PATH` — for
example by an `up` from a shell where it was not exported, or a container
left from an earlier simulator session. `restart backend` does **not** fix
this; the path is fixed when the container is created. Recreate:

```bash
export TOPOLOGY_PATH=config/topology.$RUN.json    # in THIS shell
echo $TOPOLOGY_PATH                               # must not be empty
# then 2.4 (kill all) and 2.2 (up -d redis backend), and rerun the checks above
```

If `TOPOLOGY_PATH` is right but the hostnames are still wrong, something
else answers on port 8080 — check `ss -ltnp | grep 8080` and
`sudo podman ps -a`.

If the backend exits instead, the log line names the problem in the topology
file; fix it and run `docker compose -f compose.dev.yaml restart backend`
(the path is unchanged, so no recreate is needed).

If `DBSIZE` is not `0`, an old Redis container survived — typically a failed
recreate. Go back to 2.4.

Redis publishes on `0.0.0.0:6379`, reachable from every monitored host at
`ebpf2203.jlab.org:6379` (or its IP).

## 3. Start the frontend

`ebpf2203` has no `node`/`npm` installed (`bash: npm: command not found`),
and installing them system-wide needs root. Use one of these instead:

| Option | Frontend runs on | Needs | Tunnel forwards |
| --- | --- | --- | --- |
| **A** | `ebpf2203`, inside the `node:22` container image | Podman (already there) | 5173 (the page) |
| **B** | your own computer | Node.js ≥ 22.12 locally | 8080 (the backend) |

Either way the browser ends up on your computer at `http://localhost:5173`.

### 3.1 SSH path to `ebpf2203`

Your computer cannot reach `ebpf2203` directly; jump through `scilogin`:

```
your computer  →  scilogin.jlab.org  →  ebpf2203
```

Add this to `~/.ssh/config` on **your computer** once:

```
Host ebpf2203
    HostName ebpf2203.jlab.org
    User <user>
    ProxyJump <user>@scilogin.jlab.org
    ServerAliveInterval 30        # keeps the tunnel and the dashboard's WebSocket from idling out
```

Check it with `ssh ebpf2203 hostname` (expect `ebpf2203`). You are asked for a
password twice — once for `scilogin`, once for `ebpf2203` — every time, since
`scilogin` accepts only password/keyboard-interactive login. The commands
below use this `ebpf2203` alias. Without the config entry, spell the jump out:
`ssh -J <user>@scilogin.jlab.org <user>@ebpf2203.jlab.org ...`.

If you already have a **VS Code Remote-SSH** window open on `ebpf2203`, it
uses the same path and can forward ports for you instead of `ssh -L`; see
step 3.4.

### 3.2 Option A — frontend on `ebpf2203` in a container

On `ebpf2203`, in another terminal:

```bash
export ECENTER=/path/to/eCenter
cd $ECENTER/ldrd2606_frontend
podman run --rm -it --name ecenter-frontend --network host \
    -v "$PWD":/app:Z -w /app docker.io/library/node:22 \
    sh -c "npm install && npm run dev"
```

- `--network host` puts Vite on `ebpf2203`'s own `localhost:5173`, and lets
  Vite's proxy reach the backend at `localhost:8080` exactly as it does
  outside a container.
- `npm install` writes `node_modules/` into the checkout; later runs reuse it
  and finish quickly. Build it only from this container — a `node_modules/`
  copied from another OS has the wrong native binaries.
- `Ctrl+C` stops Vite, and `--rm` removes the container. If the terminal was
  lost instead, `podman rm -f ecenter-frontend` cleans it up. This named
  container is the `node:22` one you may see in `podman ps` (step 2.4).

Note the port in Vite's output (`Local: http://localhost:5173/`). If 5173 is
already taken, Vite silently picks the next free one (5174, ...).

On **your computer**, open the tunnel and leave it running:

```bash
ssh -N -L 5173:localhost:5173 ebpf2203
```

`-N` forwards without opening a shell, so the command appears to hang; that
is expected. Open `http://localhost:5173` in your local browser; stop the
tunnel with `Ctrl+C`. Only 5173 is needed — Vite forwards `/ws`, `/edge`, and
`/history` to the backend on `ebpf2203` itself.

### 3.3 Option B — frontend on your computer

No Node on `ebpf2203` at all: run Vite locally and tunnel the **backend**
port instead. Vite's dev proxy already targets `localhost:8080`, which the
tunnel maps to the backend on `ebpf2203`.

On **your computer**, in one terminal:

```bash
ssh -N -L 8080:localhost:8080 ebpf2203
```

and in another, from your local eCenter checkout:

```bash
cd /path/to/eCenter/ldrd2606_frontend
npm install          # first time, or after a submodule pull
npm run dev
```

Open `http://localhost:5173`. Check the tunnel with
`curl -s http://localhost:8080/latest | head -c 200`. Make sure nothing on
your computer already uses port 8080 (a local backend from
`compose.dev.yaml`, for example), or the proxy talks to that instead.

### 3.4 Tunnel variations and problems

- **`bind: Address already in use`** on your computer: forward a different
  local port and keep the remote one, e.g. `-L 15173:localhost:5173` then open
  `http://localhost:15173` (option A). For option B the local port must stay
  8080, so stop whatever holds it.
- **Vite on `ebpf2203` picked 5174**: change the remote side,
  `-L 5173:localhost:5174`.
- **Page loads but the connection indicator stays red:** the tunnel is fine;
  the backend is not answering on `ebpf2203`. Check step 2.5.
- **Page does not load:** the tunnel is down or Vite is not running.
  Re-run `ssh ebpf2203 hostname` to test the hop chain.
- **Only one path at a time.** Do not run option A's tunnel and option B's
  local Vite together, and watch for **VS Code Remote-SSH**: when a VS Code
  window is connected to a remote host, it auto-forwards ports it sees in use
  there (5173, 8080) onto your computer's `127.0.0.1`, silently alongside
  your own `ssh -L`. Your browser's `localhost` may then reach a different
  Vite or backend than you intended. A second `ssh -L` onto a port VS Code
  already holds only prints `Could not request local forwarding` and keeps
  running without forwarding anything. See what holds each port on your
  computer:
  ```bash
  lsof -nP -iTCP:5173 -iTCP:8080 -sTCP:LISTEN
  # node ... [::1]:5173         → a local Vite (option B)
  # Code Helper ... 127.0.0.1:* → VS Code port forwarding
  # ssh ... 127.0.0.1:*         → your tunnel
  ```
  Close the unwanted one: stop the local Vite, or remove the port in VS
  Code's **Ports** panel (and set `"remote.autoForwardPorts": false` to stop
  it coming back). Alternatively, if that VS Code window is connected to
  `ebpf2203` itself, keep its forwards and skip `ssh -L` entirely: they
  already reach `ebpf2203`'s `localhost:5173` and `localhost:8080`.
- **Dashboard shows an old or simulator topology** (`sim-node-00`, ...,
  instead of `ebpf2203`/`ebpf2201`): the browser reached a backend started
  without this run's `TOPOLOGY_PATH`. Ask the backend you are actually
  reaching which topology it serves — this reads the first WebSocket message:
  ```bash
  curl -s -m 3 --http1.1 -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
      -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
      http://localhost:5173/ws | strings | grep -o '"hostname":"[^"]*"' | sort -u
  ```
  Use `localhost:5173` to test the whole path, or `localhost:8080` (option B)
  to test only the backend tunnel; run it on `ebpf2203` against
  `localhost:8080` to test the backend itself. If `ebpf2203`'s own backend
  shows the wrong nodes, check its `TOPOLOGY_PATH` (step 2.1) and recreate it
  with 2.4 → 2.2, with `TOPOLOGY_PATH` exported. If only the tunneled path is
  wrong, a stray forward is in the way (previous item). Reload the page
  afterwards.
- **`[vite] ws proxy error: Error: write EPIPE`** in Vite's terminal: Vite
  lost the backend end of a WebSocket while forwarding it — the backend
  restarted, the tunnel dropped, or competing forwards on 8080 (previous
  items). Harmless once the frontend reconnects to the right backend; if it
  repeats continuously, fix the port conflict first.

**Check the baseline before launching any hook:** every host from this run's
topology file is drawn as a named node, there are no edges, and the
connection indicator is green. If a host is missing, its entry is missing
from the file — fix it, restart the backend, and reload the page.

If the frontend was already open from a previous run, reload the page: it
receives the topology only when it connects.

## 4. Launch eBPF hooks and collectors — on every monitored host

Run this sequence on **both** `ebpf2203` and `ebpf2201` (and on `ebpf2202`
once it is in the topology), each with its own `$IFACE`. `ebpf2203` is the
central node but is also monitored, so it needs a hook and a collector too.

First confirm the previous run was cleaned up (step 6). All three should
print nothing / "No such file":

```bash
sudo tc filter show dev $IFACE ingress
sudo bpftool map show name map_in_tc
ls /sys/fs/bpf/tc-ing
```

Then compile, attach, and pin:

```bash
cd $ECENTER/dpu-telemetry-eBPF/eCounter/v1_userspace-poll
clang -O2 -g -target bpf -c kernel_ingress_tc.c -o kernel_ingress_tc.o

sudo tc qdisc add dev $IFACE clsact
sudo tc filter add dev $IFACE ingress bpf da obj kernel_ingress_tc.o sec tc-ing
sudo tc filter show dev $IFACE ingress           # expect one bpf filter: kernel_ingress_tc.o:[tc-ing]

sudo bpftool map pin name map_in_tc /sys/fs/bpf/tc-ing
sudo bpftool map show pinned /sys/fs/bpf/tc-ing  # expect: lru_hash  name map_in_tc
```

`tc qdisc add ... clsact` fails with `Exclusivity flag on` if `clsact`
already exists on the interface; that is harmless, continue.

Start the collector and leave it running for the whole run:

```bash
sudo <path-to-tc_collector> \
    --redis-host ebpf2203.jlab.org --map-path /sys/fs/bpf/tc-ing --verbose
```

`<path-to-tc_collector>` is `./build/tc_collector`, or the local-disk binary
from section 0. Use `--redis-host localhost` only on the host that actually
runs Redis (`ebpf2203`); `ebpf2201` and every other node must use
`ebpf2203.jlab.org` or its reachable IP.

## 5. Generate traffic and verify

From one monitored node, drive traffic toward the other (see
[`iperf3.md`](../dpu-telemetry-eBPF/docs/iperf3.md) for high-throughput
options), e.g.:

```bash
# on ebpf2203 (server)
iperf3 -s -B 129.57.178.86
# on ebpf2201 (client): traffic ebpf2201 → ebpf2203
iperf3 -c 129.57.178.86 -B 129.57.178.85 -t 30 -P 4
```

An `ebpf2201 → ebpf2203` edge should appear in the frontend within a few
seconds, counted by `ebpf2203`'s collector. Swap the server and client roles
to see `ebpf2203 → ebpf2201`, counted by `ebpf2201`'s collector. An edge drawn to `external` means that IP is not in this run's
topology file.

### Pushing to 100 Gbps

The light example above won't saturate a 100G link on its own. To drive
line-rate traffic between two 100G-capable NICs, use ESnet's `iperf3` (see
[`iperf3.md`](../dpu-telemetry-eBPF/docs/iperf3.md) for the build steps) with
multiple parallel streams:

```bash
# on ebpf2203 (server), bind to the 100G interface's IP
iperf3 -s -B 129.57.178.86

# on ebpf2201 (client), bind to its 100G interface's IP
iperf3 -c 129.57.178.86 -B 129.57.178.85 -t 60 -P 8
```

`-P 8` runs 8 parallel TCP streams — a single stream rarely saturates a 100G
NIC. This combination reached 98.4 Gbps in prior testing between two 100G
DPU interfaces. If throughput falls short, apply these tuning steps on both
hosts before retrying:

```bash
sudo ip link set $IFACE mtu 9000                    # jumbo frames
sudo cpupower frequency-set -g performance           # avoid CPU frequency scaling
sudo sysctl -w net.core.rmem_max=134217728           # 128 MiB socket buffers
sudo sysctl -w net.core.rmem_default=134217728
sudo sysctl -w net.core.wmem_max=134217728
sudo sysctl -w net.core.wmem_default=134217728
```

For UDP at line rate, add `-u -b <target>G -l 8948` (packet length tuned for
a 9000-byte MTU); expect UDP throughput and stability to be more sensitive to
stream count than TCP — see `iperf3.md` for measured single- vs.
multi-stream UDP numbers.

### Verify

Cross-check the collector's counted bytes against `iperf3`'s reported
transfer size once the run finishes — they should agree closely. Compare
`sudo bpftool map dump pinned /sys/fs/bpf/tc-ing` on the receiving host, or
the `--verbose` collector output, against the client's `[SUM] ... sender`
line. Do this **before** step 6, which deletes the map.

Check the directed edge landed in Redis and the backend (on `ebpf2203`, in
`$ECENTER/ld2606_daos_redis`). Keys are `block:{dest_ip}:{source_ip}:{ts}`,
destination first:

```bash
# ebpf2201 -> ebpf2203 (key is dest first, then source)
docker compose -f compose.dev.yaml exec redis redis-cli --scan --pattern "block:129.57.178.86:129.57.178.85:*" | head
curl -s http://localhost:8080/latest | grep -o '"129.57.178.85:129.57.178.86"'
```

In `/latest`, edges are keyed `src:dest`, the reverse of the Redis key order.

## 6. Stop and clean up — on every monitored host

A BPF map is deleted by the kernel only when **nothing references it**:
no pin in `/sys/fs/bpf`, no attached program using it, and no process holding
it open. So all three references must go — the collector, the TC filter, and
the pin.

**6.1 Stop the collector.** Press `Ctrl+C` in its terminal. Confirm it is
gone:

```bash
pgrep -a tc_collector            # expect no output
```

**6.2 Detach the TC hook.** List the ingress filters and find this run's
filter — the one showing `kernel_ingress_tc.o:[tc-ing]` — and its `pref`:

```bash
sudo tc filter show dev $IFACE ingress
# filter protocol all pref 49152 bpf chain 0
# filter protocol all pref 49152 bpf chain 0 handle 0x1 kernel_ingress_tc.o:[tc-ing] direct-action ...
```

Delete only that filter, using its `pref`:

```bash
sudo tc filter del dev $IFACE ingress pref 49152          # use the pref you saw
sudo tc filter show dev $IFACE ingress                    # expect it gone
```

If this run added `clsact` in step 4 and no other filters remain on the
interface (ingress **or** egress), remove it too:

```bash
sudo tc filter show dev $IFACE egress                     # expect nothing
sudo tc qdisc del dev $IFACE clsact
```

On a dedicated interface, `sudo tc qdisc del dev $IFACE clsact` alone removes
every filter at once; do **not** use it on an interface shared with other
TC programs.

**6.3 Unpin the map.** A pin is a file in the BPF filesystem; removing it
drops the pin's reference:

```bash
sudo rm /sys/fs/bpf/tc-ing
```

**6.4 Confirm the map is deleted.** With the collector stopped, the filter
detached, and the pin removed, the kernel frees the map. Both checks should
come back empty:

```bash
ls /sys/fs/bpf/tc-ing                       # expect: No such file or directory
sudo bpftool map show name map_in_tc        # expect no output
```

If `bpftool map show name map_in_tc` still lists a map, something still
references it. Find what:

```bash
sudo bpftool prog show name tc_egress        # program still loaded → a filter is still attached (repeat 6.2)
sudo bpftool map show name map_in_tc -j | grep -o '"pids":[^]]*]'   # a process still holds it (repeat 6.1)
sudo find /sys/fs/bpf -type f                # another pin path to the same map (rm it)
```

(`bpftool prog show name` matches the C function name; in
`kernel_ingress_tc.c` the ingress program is, confusingly, named `tc_egress`.
If it prints nothing, use `sudo bpftool prog show` and
look for a `sched_cls` program with `map_ids` matching the map's `id`.)

**6.5 Central host.** Stop the frontend with `Ctrl+C` in its terminal
(option A also removes its container; if that terminal was lost, run
`podman rm -f ecenter-frontend`). Stop the SSH tunnel on your computer with
`Ctrl+C`. Redis and the backend
can keep running — the next run's step 2 recreates the backend with its own
topology, and restarting Redis archives this run's data. To stop them
instead:

```bash
cd $ECENTER/ld2606_daos_redis
docker compose -f compose.dev.yaml stop redis backend
```

This run's Redis data stays in the volume until the next Redis launch, which
moves it to `redis-archive/YYYYMMDD-HHMMSS/`.

## Troubleshooting

- **Edge drawn to `external`, or a node missing from the baseline:** its IP
  isn't in `backend/config/topology.$RUN.json`; add it, restart the backend,
  and reload the page. On this testbed, traffic from `ebpf2202` shows up as
  `external` until it is added.
- **Backend exits right after step 2:** the topology file failed validation;
  `docker compose -f compose.dev.yaml logs backend` names the problem.
- **`CRITICAL:podman_compose:missing files: ['compose.dev.yaml']`:** the
  command ran outside `$ECENTER/ld2606_daos_redis` (for example from
  `ldrd2606_frontend` after step 3). Nothing happened — `cd` there and rerun.
  In a chained paste, every later `docker compose` line fails the same way,
  so rerun the whole block.
- **Backend still reports `TOPOLOGY_PATH=config/topology.json` after
  recreating:** the recreate ran in a shell where `TOPOLOGY_PATH` was not
  exported. `echo $TOPOLOGY_PATH`, export it, and redo 2.4 → 2.2 in that same
  shell and directory.
- **`Error: no container with name or ID ... found` during the 2.4
  force-remove:** harmless; `down` already removed it.
- **`up -d` prints `has dependent containers`, `network is being used`, or
  `container name ... is already in use`:** `podman-compose` tried to recreate
  existing containers (usually because `TOPOLOGY_PATH` changed) and failed
  part-way. Use step 2.4 to remove everything, then 2.2.
- **`bash: npm: command not found` on `ebpf2203`:** expected — there is no
  Node.js on the host. Run the frontend in the `node:22` container (step 3.2)
  or on your own computer (step 3.3).
- **`the container name "ecenter-frontend" is already in use`:** a previous
  frontend container is still around; `podman rm -f ecenter-frontend`, then
  rerun step 3.2.
- **`bind: Address already in use` when opening the tunnel:** that port is
  taken on your computer; see step 3.4.
- **Dashboard page does not load through the tunnel:** check the hop chain
  with `ssh ebpf2203 hostname`, and that Vite is still running on `ebpf2203`
  (option A) or on your computer (option B).
- **Page loads but the connection indicator stays red:** the tunnel works but
  the backend is not answering; check step 2.5. For option B, also check that
  no other local process holds port 8080.
- **Dashboard shows the simulator topology (`sim-node-00`, ...) instead of
  this run's hosts:** first run the topology check at the end of step 2.5 on
  `ebpf2203`. If `ebpf2203`'s backend itself serves `sim-node-*`, recreate it
  as described there. If it serves the right nodes but your browser does
  not, see step 3.4.
- **Vite logs `ws proxy error: write EPIPE`:** the browser is reaching the
  wrong Vite or backend, often through VS Code's automatic port forwarding;
  see step 3.4.
- **`bpftool map pin name map_in_tc` fails with "multiple maps":** a previous
  run's map was never deleted. Clean it up with step 6, then redo step 4.
- **`bpftool map pin` fails with "File exists":** `/sys/fs/bpf/tc-ing` is left
  over from a previous run; do step 6.3, then pin again.
- **`Failed to open BPF map: Permission denied`:** the collector isn't
  running as root, or the map path doesn't match what was pinned.
- **`sudo: unable to execute ...`:** the binary or a shared library it needs
  lives on an NFS home subject to `root_squash`; rebuild/copy to local disk
  as in section 0.
- **One host's traffic never appears while others' does:** check that host's
  `--redis-host` points at the machine actually running Redis, not
  `localhost`, and that Redis's firewall allows inbound `6379` from it.
