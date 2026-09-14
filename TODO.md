 # TODO — bugs and improvements

Audit of the parent repo plus all three submodules (`dpu-telemetry-eBPF`,
`ld2606_daos_redis`, `ldrd2606_frontend`), 2026-09-10.
Ordered roughly by severity within each section.

---

## A. Blockers / correctness bugs

### eBPF kernel programs (`dpu-telemetry-eBPF/eCounter/v1_userspace-poll/`)

- [ ] `kernel_egress_tc.c:55` does not compile — it initializes `.ip = ip->daddr`,
      but `tc_common.h:15-23` renamed the field to `source_ip`/`destination_ip`.
      Only `kernel_ingress_tc.c` was migrated to the two-IP key.
- [ ] `kernel_ingress_xdp.c:55` has the same break (`.ip = ip->saddr`). This means
      `compile_kernel.sh` fails on 2 of the 3 objects it claims to build, and the
      "All kernel programs compiled successfully" message is unreachable.
- [ ] `kernel_egress_tc.c` is missing `char _license[] SEC("license") = "GPL";`
      (both other kernel files have it) — the program will be rejected or
      restricted at load time.
- [ ] Once egress/XDP are migrated to the two-IP key, they must also apply the
      same `saddr`-and-`daddr` semantics the collector assumes; today egress keys
      on destination only, so a migrated egress map would carry a zero source IP.

### Go backend (`ld2606_daos_redis/backend/`)

- [ ] `history.go:66-74` + `history.go:158-190` disagree: `paginateHistoryTimestamps`
      computes `next_start` from the *selected timestamps*, but `buildHistoryFrames`
      **drops** frames that are empty or contain an invalid packet. When the last
      selected timestamp is dropped, `next_start != last_frame.timestamp + 1`, and
      the frontend's `parseHistoryPage` (`data-contract.ts:208-219`) throws
      `Invalid history continuation` — the whole page fails, not just one frame.
- [ ] Same path: the backend can return `has_more: true` with `frames: []`, which
      the frontend also rejects (`finalFrame === undefined`). Either derive
      `next_start` from the frames actually returned, or keep empty frames.
- [ ] `history.go:170-181` — one invalid packet marks the *entire* second invalid
      and discards every other edge at that timestamp. Drop the bad edge instead.
- [ ] `getHistoryPackets` (`history.go:116-156`) pages with `LimitOffset += 10000`,
      but Redis's default `MAXSEARCHRESULTS` is 10000, so any window with more than
      10k documents errors out instead of paginating. Use a cursor
      (`FT.AGGREGATE ... WITHCURSOR`) or page per timestamp.
- [ ] `redis_document.go:37-45` silently swallows `json.Unmarshal` errors, so a
      corrupt array becomes `nil`, which `validHistoryPacket` then rejects with no
      log line anywhere. Return the decode error (`docToPacket` never returns one
      today, so the caller's error handling at `history.go:142` is dead code).
- [ ] `utils.go:7-22` — `parseIntField` uses `fmt.Sscanf("%d")`, which happily
      parses `"12abc"` as `12`. Use `strconv.Atoi` and fail loudly.

### DAOS drain worker (`ld2606_daos_redis/daos-client/redis_daos_drain.py`)

- [ ] **Design conflict**: the drain deletes `packet:*` keys after archiving
      (`_drain_cycle`, step 4), but the Go backend serves both `/history` and
      `/edge?timestamp=` from those same keys. With the default `--interval 10`,
      the frontend's 40-minute replay window (`HISTORY_DURATION_SECONDS`) can
      never be satisfied. Decide the contract: either drain-and-keep (rely on the
      3600s TTL, as `CLAUDE.md` states) or shorten the UI history window.
- [ ] The drain has no age filter — it will pick up keys for the second currently
      being written, racing the collector/simulator mid-write.
- [ ] `self.redis.scan(cursor=0, ...)` always restarts at cursor 0 and ignores the
      returned cursor, so a full keyspace iteration never completes and keys that
      hash late are starved indefinitely under sustained load.
- [ ] `--dry-run` still **deletes** keys from Redis (`DryRunWriter` only fakes the
      DAOS write). A flag named "dry run" that destroys data is a foot-gun —
      either skip the delete or rename it.
- [ ] `signal.signal()` is called from `RedisDAOSDrain.__init__`, making the class
      unusable outside the main thread and untestable.

### C++ collector (`dpu-telemetry-eBPF/eCounter/v1_userspace-poll/tc_userspace.cpp`)

- [ ] `print_in_json` iterates and then **zeroes** `gBuffer[window_id]`
      (lines ~600, ~648) with its `std::unique_lock` commented out, while the poll
      loop writes into `gBuffer` under a lock in `append_snapshot_to_metric_bins`.
      The lock in `get_diff_vector` guards the wrong thing (the diff arithmetic,
      not the shared buffer). Hold `data_mutex` across the whole reporter pass.
- [ ] The main loop `join()`s the reporter thread before spawning the next one, so
      Redis write latency stalls eBPF polling and drops sub-second bins. Hand the
      completed window to a queue and let a dedicated writer thread drain it.
- [ ] No Redis reconnect: if the connection drops, `redisAppendCommand` fails for
      the rest of the run. Worse, when `redisGetReply` fails mid-batch the
      remaining queued replies are never drained, leaving the context permanently
      desynchronized. Reconnect and rebuild the context on error.
- [ ] Second boundaries come from `system_clock` (`now_sec()`), so an NTP step
      backwards reuses or skips a ring-buffer slot. Also, if the loop stalls for
      more than a second, the skipped slots are never reported *or* cleared, and
      their stale values resurface 60 seconds later.
- [ ] `get_diff_vector`'s `snapshot[i] < pre` branch (line ~518) silently truncates
      the rest of the window — this is the documented trailing-zeros case, and it
      makes the emitted array claim `samples_per_second` bins while the tail is
      fabricated zeros. The frontend charts those zeros as real samples.
- [ ] `first_report` seeding only covers edges present in the very first window;
      any edge that appears later starts from `last_seen = 0`, so its first
      published second is the whole cumulative counter — a spike. (Acknowledged
      as a `TODO/@bug` in the source.)
- [ ] LRU eviction resets a counter below `last_seen` and there is no way to tell
      eviction from a genuine restart. Consider a generation counter in the map
      value, or switch away from LRU for the collector's map.
- [ ] `gBuffer` never evicts edges — every flow ever seen stays in all 60 slots
      forever. Unbounded memory and wasted work on a long-running collector.
- [ ] `max_entries` is 2048 in all three kernel programs, but the key is now
      (src, dst, proto) instead of (ip, proto) — cardinality is squared. On a
      100 Gbps testbed this will silently evict. Size it from expected flow count.
- [ ] Byte counts use `bpf_ntohs(ip->tot_len)` (L3 and above), excluding the
      14-byte Ethernet header, so reported throughput under-counts wire bytes by
      ~1 % at 1500 MTU. Use `skb->len` on the TC path if wire-accurate is wanted.

---

## B. Robustness and operational gaps

### Go backend

- [ ] `main.go:31-35` logs a failed Redis ping and then starts serving anyway, so
      the service comes up "healthy" with no data. Either fail fast or expose a
      real readiness endpoint that reflects Redis state.
- [ ] `http.ListenAndServe` with the default mux and no `ReadTimeout` /
      `WriteTimeout` / `IdleTimeout` (`main.go:60`) — Slowloris-exposed.
- [ ] No graceful shutdown: `ctx` is `context.Background()` and never cancelled;
      no `SIGTERM` handling, so the container is always killed mid-poll.
- [ ] `websocket.go:30-46` writes to every client while holding `clientsMu`, with
      no write deadline — one stalled TCP client blocks the broadcast to all of
      them. Give each client its own send goroutine + buffered channel, and set
      `SetWriteDeadline`.
- [ ] No WebSocket ping/pong or read deadline, so half-open connections accumulate
      in the `clients` map indefinitely.
- [ ] `upgrader.CheckOrigin` returns `true` unconditionally (`websocket.go:21-26`).
      Fine for dev; gate it on an allowlist env var before any shared deployment.
- [ ] `/latest`, `/edge`, `/history` send no CORS headers, so they only work
      through the Vite proxy. Add an explicit, configurable CORS policy.
- [ ] `handleRoot` is registered on `/` and answers **every** unmatched path with
      `200 "Hello, World!"`. Return 404 for unknown paths and make `/` a real
      health endpoint reporting Redis connectivity and last successful poll.
- [ ] `handleEdge` (live path) accepts any string for `src`/`dest`, while the
      timestamped path validates with `net.ParseIP`. Make them consistent.
- [ ] `initConfig` silently ignores malformed `POLL_INTERVAL` and `REDIS_DB`.
      Log or reject bad values.
- [ ] No Redis auth support (`Password: ""` hardcoded, `main.go:27`). Add
      `REDIS_PASSWORD` / TLS options before this leaves a lab network.
- [ ] `safetyWindow = 2` is a hardcoded const (`state.go:9`); make it configurable
      alongside `POLL_INTERVAL`, since it directly sets clock-skew tolerance.
- [ ] Add structured logging (`log/slog`) instead of `fmt.Printf` — everything
      currently goes to stdout, including errors.

### Frontend

- [ ] `use-graph-stream.ts:103-107` reconnects on a flat 1 s timer forever. Add
      exponential backoff with a cap, and surface the retry count in the UI.
- [ ] `isStale` is only set on a *malformed* message — a WebSocket that goes quiet
      or drops leaves the last snapshot on screen looking live. Add a
      last-message-age watchdog and mark the graph stale after ~3 poll intervals.
- [ ] `EdgeDetailLoader`'s `cache` (`edge-detail-loader.ts:12`) is unbounded, keyed
      by `src:dest:timestamp`. Scrubbing through a 40-minute replay grows it
      without limit. Use an LRU with a fixed cap.
- [ ] `edge-detail-loader.ts:29-30` caches a live (timestamp-less) response under
      `summary.timestamp`, but the backend may return a different second — a
      wrong-key cache entry. Cache under `detail.timestamp` only, or skip caching
      live responses.
- [ ] `trafficRange` (`TrafficGraph.tsx:90-93`) uses `Math.min(...values)` — spread
      over a large edge set risks a call-stack overflow. Use a reduce.
- [ ] The `limit` bound `120` is hardcoded in both `history-client.ts:19` and
      `handlers.go:135` (`maxHistoryLimit`). Have the backend advertise it, or at
      minimum add a comment tying the two together.
- [ ] `App.tsx:36` uses `useMemo(() => new Date(), [status])` to capture a
      timestamp. React does not guarantee memo retention; use a `useEffect` +
      state instead.
- [ ] `App.tsx:176` builds the history range from the browser's `Date.now()`,
      which is compared against server-generated Redis timestamps. Clock skew
      between browser and collector node silently yields an empty replay.
- [ ] The `TrafficGraph` effect at line ~683 both depends on `preview` and calls
      `setPreview`. It currently converges, but it is one condition change away
      from a render loop — split the refresh trigger out of the effect's deps.
- [ ] Pinned edges can only be released via the `×` button; clicking the canvas
      background or the same edge again should also unpin.
- [ ] No `cy.resize()` on container resize, so the graph does not reflow when the
      window changes size.
- [ ] `vite.config.local.ts` is an untracked scratch file its own header says to
      delete. Either delete it or make the target port an env var in the real
      `vite.config.ts`.

### Simulator (`ld2606_daos_redis/traffic-simulator/simulator_v3.py`)

- [ ] Each `HPCNode` thread takes its own `int(time.time())` and drifts
      independently, so different nodes can land on different timestamps within
      the same wall second. The backend picks a **single** latest timestamp
      (`live.go:18-49`), so the live graph shows only the subset of nodes that
      happened to agree. Align all threads to the second boundary from one shared
      clock.
- [ ] `setup()` does `delete(*keys)` on the full `scan_iter` result — with a large
      keyspace this is one enormous command. Delete in batches.
- [ ] `setup()` cleans `packet:*` but never `topology:*`, so stale nodes from a
      previous run with a different `--nodes` linger.
- [ ] `node_id_to_ip` maps node 254 to `192.168.110.255` (a broadcast address) at
      the documented `--nodes` maximum of 255. Cap at 254 or shift the range.
- [ ] `SimulatorConfig.key_format` exists but no CLI flag sets it.
- [ ] Three simulators are checked in (`simulator_bk.py`, `_v2`, `_v3`) and only
      v3 matches the current backend contract. Delete or clearly deprecate the
      older two — the root README already has to warn users away from them.

### Deployment / configuration

- [ ] `backend/config/topology.json` contains `192.168.110.0` (a network address,
      hostname `v2-node-00`), left over from v2's IP scheme; v3 starts at `.1`.
      Remove the orphan entry.
- [ ] `backend/config/topology.ebpf.json` hardcodes Docker bridge IPs
      (`172.18.0.2-6`), which Docker assigns dynamically — this file breaks
      whenever the network is recreated. Use static IPs in `compose.dev.yaml` or
      generate the file at startup.
- [ ] Topology is defined **twice**: as a JSON file the Go backend reads
      (`TOPOLOGY_PATH`) and as `topology:node:{ip}` Redis hashes the simulator
      writes (`register_topology`). Only the file is actually consumed. Pick one
      source of truth and delete the other.
- [ ] `compose.dev.yaml` has no frontend and no drain-worker service, so "run the
      full stack" always needs extra terminals. Add both (frontend behind a
      `tools` profile if it should stay opt-in).
- [ ] Redis in compose has no password and publishes `6379` on all interfaces with
      no persistence policy configured. Bind to localhost at minimum.
- [ ] `CMakeLists.txt` — `include_directories(include)` points at a directory that
      does not exist; there is no `find_library` for `bpf`/`hiredis` (a missing
      dependency fails at link with an opaque error); no default
      `CMAKE_BUILD_TYPE`, so the `-O2` the file header advertises is not applied;
      no `-Wall -Wextra`.
- [ ] `compile_kernel.sh` runs `sudo clang`, producing root-owned `.o` files for no
      reason. Drop the `sudo`.
- [ ] `tc_userspace.cpp` requires `1000000 % poll_hz == 0` (so 3 Hz, 7 Hz, 300 Hz
      are rejected) and this is documented nowhere; `print_usage` also omits `-v`.

---

## C. Testing, CI, and documentation

- [ ] **No CI in any of the four repos.** Add a workflow that at minimum runs
      `go vet` + `go test` (backend), `npm run lint` + `npm test` (frontend), and
      `compile_kernel.sh` / `cmake --build` (eBPF) — the last would have caught
      the two kernel compile breaks above on the commit that introduced them.
- [ ] Go test coverage is one file (`handlers_test.go`, 83 lines). Nothing covers
      `history.go` pagination, `live.go` window selection, `state.go`,
      `topology.go`, or `redis_document.go` decoding — which is exactly where the
      correctness bugs in section A live. Add table tests with a fake Redis.
- [ ] No Python tests for the simulator or the drain worker.
- [ ] Add one contract test that asserts the Go `/history` response satisfies the
      frontend's `parseHistoryPage` invariants (shared fixtures), so the
      `next_start` class of bug cannot recur.
- [x] `CLAUDE.md` rewritten against the actual tree (2026-09-10). Corrected: the
      `packet:{dest}:{src}:{ts}` key schema and RediSearch polling (was
      `traffic:{src}:{dst}` + `SCAN` + a pub/sub `REDIS_CHANNEL`); Cytoscape.js
      with inline SVG charts (was vis.js Network + uPlot via CDN); the
      `ldrd2606_frontend` submodule as a first-class project (was a planned
      `viz/` directory and a `demo.html` prototype that does not exist);
      `eCounter/v1_userspace-poll/` paths (was `traffic_counter/`); the
      collector's real CLI including the Redis flags and the
      divisor-of-1000000 poll constraint (was `-p 10–4000`); `SERVER_PORT`
      needing its leading colon; the Cytoscape `circle` layout (was a
      rack-aligned canvas with NIC port boxes); and `simulator_v3.py` as the
      current simulator, replacing the stale v2 "Simulator Gaps" table.
- [ ] The root README claims the frontend "silently drops every edge whose
      endpoints aren't in that topology", but `collapseExternalSummaries` folds
      unknown endpoints into an `external` node. Re-verify and correct whichever
      is wrong.
- [ ] Commit the pending working-tree changes or explain them: modified
      `ld2606_daos_redis` + `ldrd2606_frontend` submodule pointers, modified
      `backend/config/topology.json`, untracked `backend/config/topology.ebpf.json`
      and `ldrd2606_frontend/vite.config.local.ts`.

---

## D. Feature gaps against the target design

- [ ] **Multi-NIC support** — the whole design rests on one `tc_collector` per NIC,
      but nothing in the simulator, topology model, or UI represents a node as a
      set of NIC IPs. Both `topology.json` and the frontend treat one IP as one
      host.
- [ ] Egress collection is unimplemented end to end: the egress kernel program
      does not build, and the collector only ever opens one map path.
- [ ] The DAOS side is a drain script with no read path — nothing serves archived
      data back to the UI, so history is bounded by whatever survives in Redis.
- [ ] No metrics/observability on any component (drop counts, poll overruns, Redis
      write latency, WebSocket client count). The collector already measures write
      latency and throws it away.
- [ ] Playback speed is fixed at 1 frame/second (`App.tsx:171`), so replaying the
      40-minute window takes 40 minutes. Add speed controls.
