# Testing real traffic across multiple hosts

This guide captures real IPv4 TCP/UDP traffic between any number of hosts by
running the TC ingress collector on **each** monitored host, and views all of
it live in the frontend. One host (`ebpf2203` in the examples below) also
hosts Redis, the backend, and the frontend; it can be one of the monitored
hosts or a separate machine.

Each host's ingress hook counts traffic **incoming** to that host, keyed by
source IP. Running the collector on every host that should appear in the
graph captures traffic in both directions between every pair, with no
special-casing per pair:

- `ebpf2203`'s collector records `<any host> → ebpf2203` traffic.
- `ebpf2201`'s collector records `<any host> → ebpf2201` traffic.
- `ebpf2205`'s collector records `<any host> → ebpf2205` traffic.
- ...and so on for every host you add.

Every collector writes to the same central Redis, so the backend sees the
full set of directed edges across all monitored hosts.

## 0. Prerequisites and known host quirks

Run these steps as the account that will later run `sudo`, using `bash`:

```bash
export IFACE=enp1s0f1np1
echo $IFACE
```

If your login shell defaults to something else (e.g. `tcsh`), start `bash`
first so the commands below work as written.

Three environment-specific blockers were hit while validating this workflow
and may recur on any monitored host:

1. **`hiredis-devel` not installed and no root to `dnf install` it.** Build
   hiredis from source into a user-writable prefix instead:
   ```bash
   git clone --depth 1 https://github.com/redis/hiredis.git /tmp/hiredis-src
   cd /tmp/hiredis-src
   make PREFIX=$HOME/.local-build install
   ```
2. **NFS home directories mounted with `root_squash`.** If `$HOME` is on NFS,
   `sudo` cannot execute (or even traverse into) anything under it — you'll
   see `sudo: unable to execute ...: Permission denied` even though the file's
   permission bits look correct. Build/copy the final `tc_collector` binary
   and its `libhiredis.so*` onto **local** disk (e.g. `/scratch/<user>/`, check
   with `mount | grep scratch`) instead of `$HOME`, and relink with an RPATH
   pointing there:
   ```bash
   mkdir -p /scratch/$USER/tc_collector_build/lib
   cp $HOME/.local-build/lib/libhiredis.so* /scratch/$USER/tc_collector_build/lib/
   cd <telemetry-repo>/eCounter/v1_userspace-poll
   g++ -std=c++17 -O2 -I$HOME/.local-build/include tc_userspace.cpp \
       -o /scratch/$USER/tc_collector_build/tc_collector \
       -L/scratch/$USER/tc_collector_build/lib -lbpf -lhiredis -pthread \
       -Wl,-rpath,/scratch/$USER/tc_collector_build/lib
   ```
   Also note `/tmp` is commonly mounted `noexec` on these hosts — never build
   or run the final binary there either.
3. **`CapEff` is all-zero for normal users and
   `sysctl kernel.unprivileged_bpf_disabled` is `2`.** Attaching the TC
   program, pinning the map, and running the collector against a pinned map
   all require `sudo`. There is no way around this short of the host owner
   granting `CAP_BPF`/`CAP_NET_ADMIN`.

Repeat sections 0 and 3 (build + attach) once per monitored host; each host
builds and runs its own `tc_collector` independently.

## 1. Central services on ebpf2203

```bash
cd <backend-repo>   # eCenter/ld2606_daos_redis
docker compose -f compose.dev.yaml up -d redis backend
```

Redis publishes on `0.0.0.0:6379`, reachable from every other monitored host
at `ebpf2203.jlab.org:6379` (or its IP). Confirm before continuing:

```bash
docker compose -f compose.dev.yaml exec redis redis-cli ping   # expect PONG
```

## 2. Update the topology configuration

The frontend has **no topology file of its own** — it renders whatever
topology the backend sends in its WebSocket snapshot, sourced entirely from
`<backend-repo>/backend/config/topology.json`. Any edge whose src or dest IP
is missing from this file collapses into a single generic `external` node in
the graph, so **every** monitored host must be added here before its traffic
is visible as a named node.

Edit `backend/config/topology.json` and add one entry per host, keyed by its
real IPv4 address (find it with `ip -4 -o addr show` on each host):

```json
{
  "nodes": {
    "129.57.178.86": { "ip": "129.57.178.86", "rack": "rack-real", "hostname": "ebpf2203" },
    "129.57.xxx.x1": { "ip": "129.57.xxx.x1", "rack": "rack-real", "hostname": "ebpf2201" },
    "129.57.xxx.x2": { "ip": "129.57.xxx.x2", "rack": "rack-real", "hostname": "ebpf2205" }
  }
}
```

Add as many entries as you have monitored hosts. `ip` must match the map key
exactly; `rack` groups nodes in the frontend's rack view (assign hosts on the
same physical rack the same value); `hostname` is only a display label.
Restart the backend to pick up the change — it is read once at startup, not
hot-reloaded:

```bash
docker compose -f compose.dev.yaml restart backend
docker compose -f compose.dev.yaml logs backend --tail 5   # expect "Loaded N topology nodes"
```

## 3. Attach the ingress collector — repeat on every monitored host

Run this same sequence on `ebpf2203` and on every other host you want in the
graph (`ebpf2201`, `ebpf2205`, ...). Substitute that host's own outward-facing
NIC for `$IFACE`:

```bash
cd <telemetry-repo>/eCounter/v1_userspace-poll   # eCenter/dpu-telemetry-eBPF
export IFACE=<this-host's-NIC>
clang -O2 -g -target bpf -c kernel_ingress_tc.c -o kernel_ingress_tc.o
sudo tc qdisc add dev $IFACE clsact
sudo tc filter add dev $IFACE ingress bpf da obj kernel_ingress_tc.o sec tc-ing
sudo bpftool map pin name map_in_tc /sys/fs/bpf/tc-ing
sudo <path-to-tc_collector> \
    --redis-host ebpf2203.jlab.org --map-path /sys/fs/bpf/tc-ing --verbose
```

Use `--redis-host localhost` only on the host that actually runs Redis
(`ebpf2203` in this example); every other host must point at `ebpf2203`'s
reachable hostname or IP, not `localhost`.

## 4. Generate traffic and verify

From any monitored host, drive traffic toward any other (see
[`iperf3.md`](../dpu-telemetry-eBPF/docs/iperf3.md) for high-throughput
options), e.g.:

```bash
# on ebpf2203
iperf3 -s -B 129.57.178.86
# on ebpf2201
iperf3 -c 129.57.178.86 -B <ebpf2201-ip> -t 30 -P 4
```

Check the directed edge landed in Redis and the backend:

```bash
docker compose -f compose.dev.yaml exec redis redis-cli KEYS "packet:129.57.178.86:*"   # ebpf2201 -> ebpf2203
curl -s http://localhost:8080/latest
```

Repeat between any other pair of monitored hosts the same way; each pair
produces its own directed edge(s) once both ends have a collector running.

## 5. Start the frontend

```bash
cd <frontend-repo>   # eCenter/ldrd2606_frontend
npm install
npm run dev
```

Open `http://localhost:5173`. Every monitored host should appear as a named
node (per the topology file from step 2), with a directed edge for each
active flow between them.

## Troubleshooting

- **Edge missing from the graph but present in `/latest`:** its IP isn't in
  `backend/config/topology.json`; add it and restart the backend.
- **`Failed to open BPF map: Permission denied`:** the collector isn't
  running as root, or the map path doesn't match what was pinned.
- **`sudo: unable to execute ...`:** the binary or a shared library it needs
  lives on an NFS home subject to `root_squash`; rebuild/copy to local disk
  as in section 0.
- **One host's traffic never appears while others' does:** check that host's
  `--redis-host` points at the machine actually running Redis, not
  `localhost`, and that Redis's firewall allows inbound `6379` from it.
