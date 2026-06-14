# ha-stats-lake

Long-term storage for Home Assistant sensor data — no extra server required.

An AppDaemon app running inside Home Assistant samples a list of entities
every 30 minutes, appends them to flat per-entity monthly CSV files, and
nightly:

- consolidates new rows into a [DuckLake](https://ducklake.select/) (Parquet
  on object storage) hosted on **Cloudflare R2**
- syncs the raw CSVs to **OneDrive** via `rclone` as a cold backup

Visualization happens later, on demand, using the
[DuckDB UI extension](https://duckdb.org/docs/extensions/ui) pointed directly
at R2 — no dashboard server to maintain.

```
Home Assistant
  └─ AppDaemon (ha_stats app)
       ├─ every 30 min  → sample tracked entities → CSV (local)
       ├─ nightly        → consolidate CSV → DuckLake (Parquet) on R2
       └─ nightly        → rclone sync CSV → OneDrive (cold backup)

Your laptop (on demand)
  └─ duckdb -ui  → query the DuckLake on R2 directly
```

## Why this design

- **Push, not pull** — HA pushes data out, no inbound connections to your
  home network.
- **Flat files** — CSV is human-readable, `grep`-able, trivially synced in
  either direction with `rclone`. No database to corrupt or migrate.
- **Typed automatically** — entity type (`gauge` / `counter` / `binary`),
  unit, and label are all inferred from the entity's own HA attributes.
  Nothing to maintain by hand.
- **Config lives in the HA UI** — which entities to track is a single
  **Group helper**. Add or remove members in
  `Settings → Helpers`, no YAML edits, no restarts.
- **No extra infrastructure** — everything runs inside your existing HA
  instance via AppDaemon. R2 and OneDrive are optional; both can be disabled.

## Repository layout

```
apps/
  ha_stats/
    ha_stats.py        # the AppDaemon app
  apps.yaml.example     # example app configuration — copy to apps.yaml
  requirements.txt       # python packages AppDaemon needs to install
```

## Prerequisites

- Home Assistant with the **AppDaemon** add-on installed
  (`Settings → Add-ons → Add-on store → AppDaemon 4`)
- (optional) A **Cloudflare R2** bucket, if you want a queryable Parquet
  lakehouse
- (optional) An **rclone** remote configured for OneDrive, if you want cold
  backups of the raw CSVs

---

## 1. Install the AppDaemon app

1. Enable the **Samba share** or **SSH/Terminal** add-on so you can reach
   `/addon_configs/a0d7b954_appdaemon/` (path may vary slightly by AppDaemon
   add-on version — check the add-on's "Config" tab for its config path).

2. Copy `apps/ha_stats/ha_stats.py` into the AppDaemon `apps/` directory:

   ```
   /addon_configs/a0d7b954_appdaemon/apps/ha_stats/ha_stats.py
   ```

3. Copy `apps/apps.yaml.example` to
   `/addon_configs/a0d7b954_appdaemon/apps/apps.yaml` (or append its content
   to your existing `apps.yaml`) and adjust the values — see configuration
   below.

4. Add `duckdb` to the AppDaemon add-on's Python packages. In the AppDaemon
   add-on configuration (Settings tab of the add-on), add:

   ```yaml
   python_packages:
     - duckdb
   ```

   If you don't plan to use R2/DuckLake, you can skip this and leave
   `r2_bucket` empty in `apps.yaml` — consolidation will simply be skipped.

5. If you want OneDrive backup, `rclone` needs to be available inside the
   AppDaemon container. The simplest route is a custom AppDaemon Docker
   image with `rclone` installed, or skip this feature (leave
   `onedrive_remote` empty) and rely on R2/DuckLake as your backup, which is
   versioned by design.

6. Restart the AppDaemon add-on. Check its log for:

   ```
   ha_stats initialized: csv_dir=/conf/ha_stats_data, group=group.ha_stats_tracked_entities, interval=1800s
   ```

---

## 2. Create the entity group helper

This is the **only configuration step you'll repeat** when adding or
removing tracked sensors — done entirely in the HA UI.

1. Go to `Settings → Devices & services → Helpers`
2. Click **+ Create helper → Group**
3. Choose type **Entity**
4. Name it `HA Stats tracked entities`
   (this creates `group.ha_stats_tracked_entities`)
5. In **Entities**, add every sensor / binary_sensor / switch you want
   recorded — e.g.:
   - `sensor.power_consumption`
   - `sensor.energy_total`
   - `sensor.temperature_living`
   - `binary_sensor.door_front`
6. Save

To add or remove a tracked entity later, just edit this group's member list.
No restart needed — the app re-reads it on every sample (every 30 minutes
by default).

### How type/unit/label are determined

| HA attribute | Result |
|---|---|
| domain is `binary_sensor`, `switch`, or `input_boolean` | type = `binary` |
| `state_class` is `total` or `total_increasing` | type = `counter` |
| anything else numeric | type = `gauge` |
| `unit_of_measurement` | used as the unit |
| `friendly_name` | used as the display label |

Storage key is the entity_id with `.` replaced by `_`
(e.g. `sensor.power_consumption` → `sensor_power_consumption`).

---

## 3. Configure `apps.yaml`

Edit the copy of `apps.yaml.example`:

```yaml
ha_stats:
  module: ha_stats
  class: HaStats
  csv_dir: /conf/ha_stats_data
  group_entity: group.ha_stats_tracked_entities
  sample_interval_seconds: 1800
  consolidate_time: "02:00:00"
  onedrive_sync_time: "03:00:00"

  # Leave empty to disable R2/DuckLake consolidation
  r2_bucket: "s3://ha-stats/lake/"
  r2_endpoint: "https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com"
  r2_key_id: "YOUR_R2_ACCESS_KEY_ID"
  r2_secret: "YOUR_R2_SECRET_ACCESS_KEY"

  # Leave empty to disable OneDrive backup
  onedrive_remote: "onedrive:ha-backup"
```

`csv_dir` must point to a path writable by AppDaemon and persistent across
restarts — `/conf/...` (the AppDaemon add-on's persistent config volume) is
recommended.

---

## 4. (Optional) Cloudflare R2 setup

1. Create a bucket, e.g. `ha-stats`, in the Cloudflare dashboard under **R2**.
2. Create an API token with **read & write** access to that bucket, note the
   Access Key ID, Secret Access Key, and your Account ID.
3. Fill in `r2_bucket`, `r2_endpoint`, `r2_key_id`, `r2_secret` in
   `apps.yaml` as shown above.

The first nightly run creates the DuckLake catalog and table automatically —
nothing to provision manually.

---

## 5. (Optional) OneDrive backup via rclone

1. On a machine with `rclone` installed, run `rclone config` and create a
   remote named `onedrive` (or any name — update `onedrive_remote`
   accordingly) following the
   [rclone OneDrive guide](https://rclone.org/onedrive/).
2. Copy the resulting `rclone.conf` into the AppDaemon container at the path
   `rclone` expects (`~/.config/rclone/rclone.conf`), or mount it as part of
   your AppDaemon add-on configuration.
3. Set `onedrive_remote: "onedrive:ha-backup"` in `apps.yaml`.

If `rclone` isn't available in your AppDaemon environment, leave this empty —
everything else keeps working, R2 just becomes your only off-site copy.

---

## 6. Visualizing the data

No dashboard to host. On any machine with DuckDB installed:

```bash
duckdb -ui
```

Then in the SQL console:

```sql
INSTALL ducklake;
LOAD ducklake;

CREATE SECRET r2 (
    TYPE S3,
    KEY_ID '<your-r2-access-key-id>',
    SECRET '<your-r2-secret-access-key>',
    ENDPOINT '<your-account-id>.r2.cloudflarestorage.com',
    REGION 'auto'
);

ATTACH 'ducklake:s3://ha-stats/lake/catalog.duckdb' AS lake (
    DATA_PATH 's3://ha-stats/lake/data/'
);
```

Example queries:

```sql
-- last 7 days, all entities
SELECT entity, ts, value
FROM lake.stats
WHERE ts > now() - INTERVAL 7 DAY
ORDER BY entity, ts;

-- daily average power
SELECT date_trunc('day', ts) AS day, avg(value) AS avg_w
FROM lake.stats
WHERE entity = 'sensor_power_consumption'
GROUP BY 1 ORDER BY 1;

-- daily energy delta from a cumulative counter
SELECT date_trunc('day', ts) AS day,
       max(value) - min(value) AS kwh
FROM lake.stats
WHERE entity = 'sensor_energy_total'
GROUP BY 1 ORDER BY 1;
```

The DuckDB UI lets you turn any query result into a chart directly in the
browser.

---

## Troubleshooting

- **No data appearing** — check the AppDaemon log for `ha_stats initialized`
  and `sampled N entities` lines. If `N` is 0, verify the group helper exists
  and has members.
- **Consolidation errors** — usually R2 credentials or bucket path. Test the
  `ATTACH` statement manually in a local `duckdb` shell first.
- **rclone errors** — verify `rclone listremotes` works inside the AppDaemon
  container and the remote name matches `onedrive_remote`.

## License

MIT — see [LICENSE](LICENSE).
