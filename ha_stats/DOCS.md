# HA Stats Lake — Documentation

Samples a Group helper's entities every 30 minutes to flat per-entity monthly
CSV files, then nightly consolidates into a DuckLake (Parquet) on Cloudflare R2
and optionally syncs raw CSVs via rclone.

```
Home Assistant (ha_stats_lake add-on)
  ├─ every 30 min  → sample tracked entities → CSV (/data/ha_stats_data/)
  ├─ nightly        → consolidate CSV → DuckLake (Parquet) on R2
  └─ nightly        → rclone sync CSV → cold backup remote

Your laptop (on demand)
  └─ duckdb -ui  → query the DuckLake on R2 directly
```

## Installation

1. Go to **Settings → Add-ons → Add-on store**.
2. Click the three-dot menu (⋮) → **Repositories**.
3. Add `https://github.com/kalw/hassio-addons` and click **Add**.
4. Find **HA Stats Lake** in the store and click **Install**.

## Create the entity group helper

This is the **only step you'll repeat** when adding or removing tracked sensors.

1. Go to `Settings → Devices & services → Helpers`
2. Click **+ Create helper → Group → Entity**
3. Name it `HA Stats tracked entities`
   (creates `group.ha_stats_tracked_entities`)
4. Add every sensor / binary_sensor / switch you want recorded, e.g.:
   - `sensor.power_consumption`
   - `sensor.energy_total`
   - `sensor.temperature_living`
   - `binary_sensor.door_front`
5. Save

To add or remove a tracked entity later, edit this group — no restart needed.

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

## Configuration

| Option | Description | Default |
|--------|-------------|---------|
| `group_entity` | Entity ID of the Group helper | `group.ha_stats_tracked_entities` |
| `sample_interval_seconds` | How often to sample | `1800` |
| `consolidate_time` | When to run nightly DuckLake consolidation | `02:00:00` |
| `onedrive_sync_time` | When to run nightly rclone sync | `03:00:00` |
| `r2_bucket` | S3 URI of the R2 bucket (empty = disable) | — |
| `r2_endpoint` | R2 endpoint URL | — |
| `r2_key_id` | R2 Access Key ID | — |
| `r2_secret` | R2 Secret Access Key | — |
| `onedrive_remote` | rclone remote path (empty = disable) | — |

CSV data is persisted to `/data/ha_stats_data/` inside the add-on's data volume.

## (Optional) Cloudflare R2 setup

1. Create a bucket (e.g. `ha-stats`) in the Cloudflare dashboard under **R2**.
2. Create an API token with **read & write** access. Note the Access Key ID,
   Secret Access Key, and your Account ID.
3. Set `r2_bucket` to `s3://ha-stats/lake/`, `r2_endpoint` to
   `https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com`, and fill in the key fields.

The first nightly run creates the DuckLake catalog and table automatically.

## (Optional) rclone cold backup

1. On any machine, run `rclone config` and create a remote following the
   [rclone docs](https://rclone.org/docs/).
2. Copy `rclone.conf` into the add-on data volume at
   `/data/.config/rclone/rclone.conf`.
3. Set `onedrive_remote` to e.g. `onedrive:ha-backup`.

## Visualizing the data

On any machine with DuckDB:

```bash
duckdb -ui
```

In the SQL console:

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
SELECT entity, ts, value FROM lake.stats
WHERE ts > now() - INTERVAL 7 DAY
ORDER BY entity, ts;

-- daily average power
SELECT date_trunc('day', ts) AS day, avg(value) AS avg_w
FROM lake.stats
WHERE entity = 'sensor_power_consumption'
GROUP BY 1 ORDER BY 1;

-- daily energy delta from a cumulative counter
SELECT date_trunc('day', ts) AS day, max(value) - min(value) AS kwh
FROM lake.stats
WHERE entity = 'sensor_energy_total'
GROUP BY 1 ORDER BY 1;
```

## Troubleshooting

- **No data appearing** — check the add-on log for `sampled N entities`. If
  `N` is 0, verify the group helper exists and has members.
- **Consolidation errors** — usually R2 credentials or bucket path. Test the
  `ATTACH` statement manually in a local `duckdb` shell first.
- **rclone errors** — verify `rclone.conf` is at
  `/data/.config/rclone/rclone.conf` and the remote name matches `onedrive_remote`.
