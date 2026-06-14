# AGENTS.md

Guidance for AI coding agents (and humans) working in this repo.

## What this is

A Home Assistant add-on that samples sensor entities to flat per-entity monthly
CSVs and consolidates them nightly into a DuckLake (Parquet) on Cloudflare R2.
Optionally syncs the raw CSVs to any `rclone` remote as a cold backup.

## Repository layout

```
ha_stats/               ← the add-on directory (slug: ha_stats_lake)
  config.yaml           ← HA add-on manifest + options schema
  Dockerfile            ← multi-stage Alpine build
  run.sh                ← container entrypoint
  ha_stats.py           ← asyncio app; talks to HA via Supervisor REST API
  requirements.txt      ← aiohttp, duckdb

.github/workflows/
  ci.yaml               ← ruff lint + multi-arch Docker build-only on every PR
  publish-docker.yaml   ← push to GHCR on v* tags / workflow_dispatch
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

## Build, container & CI

`uv` is used for local dependency management. The add-on image is a two-stage
Alpine build: Python deps installed into a venv in the build stage, copied into
a slim runtime that also includes the `rclone` binary.

```bash
# local image build (amd64, for smoke-testing)
docker build -t ha-stats-lake:dev ha_stats/

# verify the app imports cleanly (no HA token needed for import check)
docker run --rm ha-stats-lake:dev python -c "import ha_stats; print('ok')"
```

GitHub Actions:

- `ci.yaml` — ruff lint + multi-arch Docker build (no push) on every PR and
  every push to `main`. Targets: `linux/amd64`, `linux/arm64`, `linux/arm/v7`.
- `publish-docker.yaml` — pushes
  `ghcr.io/<owner>/ha-stats:<version>` and `:latest` to GHCR on `v*` tags or
  `workflow_dispatch` (for manual re-publishes of a specific tag).

Dependabot tracks pip (inside `ha_stats/`) and github-actions.

The version in `ha_stats/config.yaml` must match the git tag pushed for a
release (`v0.2.0` → `version: "0.2.0"`). Keep them in sync; see release flow
below.

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

Releases are manual (no release-please bot). Steps:

1. Bump `version` in `ha_stats/config.yaml` to the next semver (e.g. `"0.2.0"`).
2. Commit: `chore: release 0.2.0`.
3. Open a PR, get it merged to `main`.
4. After merge, tag `main`:
   ```bash
   git fetch origin
   git checkout main && git pull
   git tag v0.2.0
   git push origin v0.2.0
   ```
5. `publish-docker.yaml` fires automatically:
   - pushes the multi-arch image to GHCR (`ha-stats:0.2.0` and `ha-stats:latest`)
   - dispatches an `update` event to `kalw/hassio-addons`, triggering the
     `repository-updater` there to pull the new `config.yaml` and refresh the
     addon listing
6. Create a GitHub Release from the tag with a short changelog bullet list.

**Do not** push version tags before the PR is merged to `main`.
**Do not** create GitHub Releases by hand before the Docker publish succeeds.

### One-time secrets setup (human steps, done once per repo)

`publish-docker.yaml` needs a `DISPATCH_TOKEN` secret — a GitHub PAT with
`repo` scope on `kalw/hassio-addons` — to trigger the repository updater.

Repository → Settings → Secrets and variables → Actions → **New repository secret**:

| Name             | Value                                         |
| ---------------- | --------------------------------------------- |
| `DISPATCH_TOKEN` | PAT with `repo` scope on `kalw/hassio-addons` |

`GITHUB_TOKEN` (auto-injected) covers only the GHCR push in this repo.
