# eCenter

Real-time multi-node network traffic monitoring and visualization for JLab LDRD 100Gbps testbeds.

See `CLAUDE.md` for the full architecture design and decisions.

## Repository Structure

```
eCenter/
├── dpu-telemetry-eBPF/   # eBPF traffic counter (submodule → JeffersonLab/dpu-telemetry-eBPF)
├── ld2606_daos_redis/    # Redis backend + simulator + DAOS client (submodule → cissieAB/ld2606_daos_redis)
├── viz/                  # Go viz backend + browser UI (vis.js + uPlot)
└── CLAUDE.md             # Architecture documentation
```

## Clone

Always clone with `--recurse-submodules` to pull both sub-projects:

```bash
git clone --recurse-submodules https://github.com/cissieAB/eCenter.git
```

If you already cloned without it:

```bash
git submodule update --init
```

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

## Viz Prototype

A standalone browser demo (mock data, no Redis required) lives in `viz/demo.html`. Open it directly in any browser:

```bash
open viz/demo.html
```
