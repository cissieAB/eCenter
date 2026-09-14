# eCenter

Real-time multi-node network traffic monitoring and visualization for JLab LDRD 100Gbps testbeds.

See `CLAUDE.md` for the full architecture design and decisions, and `TODO.md` for known bugs.

## Guides

| Guide | Use it for |
|---|---|
| [docs/guide_simulator.md](docs/guide_simulator.md) | **Local Test**: the full stack on one machine with simulated traffic |
| [docs/guide_real-traffic.md](docs/guide_real-traffic.md) | Per-run workflow with eBPF collectors on the `ebpf` testbed |
| [docs/setup.md](docs/setup.md) | Backend, Redis, frontend, and TC ingress collector setup reference |

## Repository Structure

eCenter is a thin parent repo. Its code lives in three git submodules, each with its own
history and upstream:

```
eCenter/                  # parent → cissieAB/eCenter
├── dpu-telemetry-eBPF/   # eBPF traffic counter        → JeffersonLab/dpu-telemetry-eBPF
├── ld2606_daos_redis/    # Go backend, simulator, DAOS → cissieAB/ld2606_daos_redis
├── ldrd2606_frontend/    # React + Cytoscape dashboard → cissieAB/ldrd2606_frontend
├── docs/                 # guides
├── CLAUDE.md             # architecture documentation
└── TODO.md               # known bugs and open work
```

The parent repo does not store submodule code. It stores a **commit pointer** (SHA) per
submodule, and `.gitmodules` sets `branch = main` for all three so that
`git submodule update --remote` follows each submodule's `main`.

## Clone

```bash
git clone --recurse-submodules git@github.com:cissieAB/eCenter.git
cd eCenter
git config submodule.recurse true     # make pull/checkout also update submodules
```

If you already cloned without `--recurse-submodules`:

```bash
git submodule update --init
```

## Pull

There are two meanings of "latest" with submodules:

- **Pinned**: the submodule commits the parent repo records. This is what a plain
  `git pull` gives you.
- **Upstream tips**: the newest commit on each submodule's `main`, which may be ahead of
  what the parent records.

### Pull the pinned versions

```bash
git pull
git submodule update --init --recursive   # not needed if submodule.recurse is true
```

`git pull` updates the parent and its recorded pointers but, without
`submodule.recurse`, leaves the submodule working trees where they were. The second
command checks each submodule out at the recorded commit, which leaves it in
**detached HEAD**.

### Pull the upstream tips of every submodule

```bash
git pull
git submodule update --init
git submodule foreach 'git switch main && git pull --ff-only'
```

This puts every submodule on its `main` branch at `origin/main`, so you can also commit
there directly. To make this one command, add a local alias once:

```bash
git config alias.pull-all "!git pull && git submodule update --init && git submodule foreach 'git switch -q main && git pull -q --ff-only'"
git pull-all
```

If a submodule's `main` has moved past the pointer the parent records, `git status` in
the parent shows it as `modified (new commits)`. Record the new pointer so everyone else
gets it with a plain `git pull`:

```bash
git add dpu-telemetry-eBPF ld2606_daos_redis ldrd2606_frontend
git commit -m "bump submodules to latest main"
git push
```

## Develop and push

Always commit inside the submodule first, push it, and only then record the new pointer
in the parent. Pushing the parent first publishes a pointer to a commit nobody else can
fetch.

```bash
# 1. Work on a branch inside the submodule (never on a detached HEAD)
cd ld2606_daos_redis
git switch main
git pull --ff-only
# ... edit ...
git commit -am "your change"
git push                              # push the submodule to its own upstream

# 2. Record the new pointer in the parent
cd ..
git add ld2606_daos_redis
git commit -m "bump ld2606_daos_redis: your change"
git push
```

Changes that touch only parent files (`README.md`, `docs/`, `CLAUDE.md`, `TODO.md`) are
committed and pushed from the eCenter root as in any normal repo.

As a safety net, have `git push` in the parent refuse to push when a submodule commit it
points to has not been pushed:

```bash
git config push.recurseSubmodules check
```

### Frontend repository

`ldrd2606_frontend` points at `cissieAB/ldrd2606_frontend`, a standalone repository (not
a GitHub fork) created from `RaiqaRasool/ldrd2606_frontend` with its full history. Push
to it directly; no outside approval is involved. To bring in new commits from the
original repo, add it once as `upstream` and merge:

```bash
cd ldrd2606_frontend
git remote add upstream https://github.com/RaiqaRasool/ldrd2606_frontend.git   # once
git switch main
git pull upstream main
git push
cd .. && git add ldrd2606_frontend && git commit -m "bump ldrd2606_frontend" && git push
```

Existing clones made before the switch need to pick up the new submodule URL once:

```bash
git pull
git submodule sync ldrd2606_frontend
```

### Check submodule status

```bash
git submodule status
git submodule foreach 'git status -sb'
```

`git submodule status` prefixes:

- `-` → not initialized yet (`git submodule update --init`)
- `+` → checked-out commit differs from what the parent records (commit a bump, or run
  `git submodule update` to go back)
- `U` → merge conflicts inside the submodule
- no prefix → in sync

`git diff --submodule` shows which commits a pointer moved across.

### Refresh dependencies after a pull

Moving submodule code does not update anything built or installed from it:

```bash
(cd ldrd2606_frontend && npm install)                                  # frontend
(cd dpu-telemetry-eBPF/eCounter/v1_userspace-poll && cmake --build build)  # collector
```
