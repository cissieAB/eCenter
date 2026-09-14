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
3. Frontend              central host   dashboard shows every host as a node, no edges yet
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
TOPOLOGY_PATH=config/topology.$RUN.json docker compose -f compose.dev.yaml up -d redis backend
```

`TOPOLOGY_PATH` is relative to the backend's working directory, hence
`config/...` rather than `backend/config/...`. `up -d` recreates the backend
when `TOPOLOGY_PATH` differs from the last run. If the services are already
running with the same file and you only edited its contents, use
`docker compose -f compose.dev.yaml restart backend` instead.

To guarantee an empty Redis for this run even if it was already running,
restart it too:

```bash
docker compose -f compose.dev.yaml restart redis
```

Confirm before continuing:

```bash
docker compose -f compose.dev.yaml exec redis redis-cli ping     # expect PONG
docker compose -f compose.dev.yaml exec redis redis-cli DBSIZE   # expect 0
docker compose -f compose.dev.yaml logs backend --tail 5
# expect: Loaded N topology nodes from config/topology.<run>.json
```

If the backend exits instead, the log line names the problem in the topology
file; fix it and rerun the `up -d` command.

Redis publishes on `0.0.0.0:6379`, reachable from every monitored host at
`ebpf2203.jlab.org:6379` (or its IP).

## 3. Start the frontend

On `ebpf2203`, in another terminal:

```bash
export ECENTER=/path/to/eCenter
cd $ECENTER/ldrd2606_frontend
npm install          # first time, or after a submodule pull
npm run dev
```

Open `http://localhost:5173`. From another machine, tunnel it first:
`ssh -L 5173:localhost:5173 ebpf2203.jlab.org`.

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
`$ECENTER/ld2606_daos_redis`). Keys are `packet:{dest_ip}:{source_ip}:{ts}`,
destination first:

```bash
# ebpf2201 -> ebpf2203 (key is dest first, then source)
docker compose -f compose.dev.yaml exec redis redis-cli --scan --pattern "packet:129.57.178.86:129.57.178.85:*" | head
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

**6.5 Central host.** Stop the frontend with `Ctrl+C`. Redis and the backend
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
