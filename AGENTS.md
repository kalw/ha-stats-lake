# AGENTS.md

Guidance for AI coding agents (and humans) working in this repo.

## What this is

A Home Assistant add-on that samples sensor entities to flat per-entity monthly
CSVs and consolidates them nightly into a DuckLake (Parquet) on Cloudflare R2.
Optionally syncs the raw CSVs to any `rclone` remote as a cold backup.

## Repository layout

```
ha_stats/                                  ← the add-on directory (slug: ha_stats_lake)
  config.yaml                              ← HA add-on manifest + options schema
  build.yaml                               ← per-arch base image mapping
  Dockerfile                               ← single-stage Alpine build
  ha_stats.py                              ← asyncio app; talks to HA via Supervisor REST API
  requirements.txt                         ← aiohttp, duckdb
  rootfs/etc/services.d/ha-stats/run       ← s6-overlay service script (replaces CMD)

.github/workflows/
  ci.yaml                                  ← delegates to hassio-addons/workflows addon-ci on every PR/push
  deploy.yaml                              ← delegates to hassio-addons/workflows addon-deploy on published releases
```

## Add-on runtime contract

The HA Supervisor injects two things into the container:

| Name                 | Value                                                     |
| -------------------- | --------------------------------------------------------- |
| `SUPERVISOR_TOKEN`   | bearer token for `http://supervisor/core/api/`            |
| `/data/options.json` | user config from the HA UI (matches `config.yaml` schema) |

Data is persisted to `/data/ha_stats_data/` (the add-on's writable `/data` volume).
Never hardcode paths outside `/data/`.

## Key design rules

- **No AppDaemon** — the add-on talks to HA directly via `aiohttp`; do not
  re-introduce `adbase` or AppDaemon scheduler primitives.
- **Asyncio for scheduling** — `_run_sampler`, `_run_consolidator`, and
  `_run_onedrive` are long-running coroutines gathered in `HaStats.run()`.
  Blocking work (DuckDB, rclone) is offloaded to `run_in_executor`.
- **Config from `/data/options.json`** — all tunable values live there; never
  add env-var config that bypasses the HA UI.
- **Optional features degrade gracefully** — if `r2_bucket` or
  `onedrive_remote` is empty the corresponding path logs an info message and
  returns; it must never crash the main loop.
- **s6-overlay entrypoint** — the container is started by
  `rootfs/etc/services.d/ha-stats/run`, not a `CMD` or `ENTRYPOINT` in the
  Dockerfile. Do not add a `CMD` instruction; s6 is baked into the HA base
  images.

## Build, container & CI

The add-on image is a **single-stage Alpine** build. `build-base` and `cmake`
are installed before pip (required to compile `duckdb` from source on aarch64,
which has no pre-built musl wheel) and removed in the same `RUN` layer. `rclone`
is installed via apk and stays at runtime.

```bash
# local image build (amd64, for smoke-testing)
docker build --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.18 \
  -t ha-stats-lake:dev ha_stats/

# verify the app imports cleanly (no HA token needed for import check)
docker run --rm ha-stats-lake:dev python -c "import ha_stats; print('ok')"
```

GitHub Actions:

- `ci.yaml` — delegates to `hassio-addons/workflows/.github/workflows/addon-ci.yaml@main`.
  Runs linters (yamllint, prettier, hadolint, shellcheck), validates the HA addon
  manifest, and performs Docker builds (no push) for `aarch64` and `amd64` on
  every PR and push to `main`.
- `deploy.yaml` — delegates to `hassio-addons/workflows/.github/workflows/addon-deploy.yaml@main`.
  Fires only on **published** GitHub Releases; pushes the multi-arch image to
  GHCR and triggers `kalw/hassio-addons` via `repository_dispatch`.

Supported architectures: **aarch64**, **amd64**. The deprecated arches
(`armhf`, `armv7`, `i386`) were dropped in HA 2025.12 and must not be
re-added.

## Git workflow

**All changes must go through a pull request. Never push directly to `main`.**

### Branch naming

```
<type>/<short-description>
```

| Type       | When to use                           |
| ---------- | ------------------------------------- |
| `feat`     | new user-facing behaviour             |
| `fix`      | bug fix                               |
| `ci`       | workflow / pipeline changes           |
| `docs`     | documentation only                    |
| `refactor` | code restructure, no behaviour change |
| `chore`    | maintenance (deps, config, tooling)   |

Examples: `feat/add-label-column`, `fix/consolidation-dedup`, `ci/arm-matrix`.

### Commit messages — Conventional Commits

Every commit on a PR should follow this format:

```
<type>(<optional scope>): <subject>

<optional body>
```

Examples:

```
feat(csv): add entity label column to monthly files
fix(consolidate): skip insert when no new CSV rows found
ci: cache pip downloads between matrix jobs
chore: bump duckdb to 1.2.0
```

### Standard agent flow

```bash
# 1. Always start on a branch.
git checkout -b <type>/<description>

# 2. Make changes; commit with a conventional message.
git add <specific files>     # never `git add -A`
git commit -m "type(scope): subject"

# 3. Push and open a PR.
git push -u origin HEAD
gh pr create --fill
```

**Do not** `git push origin main`.

### Before every push — check the PR is still open

```bash
git fetch origin
gh pr view --head "$(git branch --show-current)" \
  --json state,mergedAt --jq '"state: \(.state)  mergedAt: \(.mergedAt)"'
```

| Output          | What to do                                                                                   |
| --------------- | -------------------------------------------------------------------------------------------- |
| `state: OPEN`   | Safe to push — continue normally.                                                            |
| `state: MERGED` | **Stop.** Create a new branch from `origin/main`, cherry-pick your commit(s), open a new PR. |
| `state: CLOSED` | Same as MERGED — start fresh.                                                                |

If `gh pr view` errors (no PR found), the branch was never opened — push and
run `gh pr create --fill`.

### Release flow

Releases are manual (no release-please bot). The HA addon linter requires
`version: "dev"` in the repo at all times; the version is only bumped for the
release commit and immediately reset afterward.

Steps:

1. Bump `version` in `ha_stats/config.yaml` from `"dev"` to the next semver
   (e.g. `"0.2.0"`).
2. Commit: `chore: release 0.2.0`.
3. Open a PR, get it merged to `main`.
4. After merge, tag `main`:
   ```bash
   git fetch origin
   git checkout main && git pull
   git tag v0.2.0
   git push origin v0.2.0
   ```
5. `deploy.yaml` fires automatically on the published release:
   - pushes the multi-arch image to GHCR (`ghcr.io/kalw/ha_stats_lake/<arch>:<version>`)
   - dispatches an `update` event to `kalw/hassio-addons` via `DISPATCH_TOKEN`,
     triggering the `repository-updater` to refresh the add-on listing
6. Reset `version` back to `"dev"` in `ha_stats/config.yaml`, commit and merge
   via PR.
7. Create a GitHub Release from the tag with a short changelog bullet list.

**Do not** push version tags before the PR is merged to `main`.
**Do not** create GitHub Releases by hand before the Docker publish succeeds.

### One-time secrets setup (human steps, done once per repo)

`deploy.yaml` needs a `DISPATCH_TOKEN` secret — a GitHub PAT with `repo` scope
on `kalw/hassio-addons` — to trigger the repository updater.

```bash
gh secret set DISPATCH_TOKEN --repo kalw/ha-stats-lake --body "$(gh auth token)"
```

Or via the UI: Repository → Settings → Secrets and variables → Actions →
**New repository secret**:

| Name             | Value                                         |
| ---------------- | --------------------------------------------- |
| `DISPATCH_TOKEN` | PAT with `repo` scope on `kalw/hassio-addons` |

`GITHUB_TOKEN` (auto-injected) covers only the GHCR push in this repo.
