# HA Stats Lake — Documentation

Samples a configured list of entities every 30 minutes to flat per-entity
monthly CSV files, then nightly consolidates into a DuckLake (Parquet) on any
S3-compatible object store and optionally syncs raw CSVs to any rclone remote.

```
Home Assistant (ha_stats_lake add-on)
  ├─ every 30 min  → sample tracked entities → CSV (/data/ha_stats_data/)
  ├─ nightly        → consolidate CSV → DuckLake (Parquet) on S3-compatible store
  └─ nightly        → rclone sync CSV → any rclone remote (cold backup)

Your laptop (on demand)
  └─ duckdb -ui  → query the DuckLake on S3 directly
```

## Installation

1. Go to **Settings → Add-ons → Add-on store**.
2. Click the three-dot menu (⋮) → **Repositories**.
3. Add `https://github.com/kalw/hassio-addons` and click **Add**.
4. Find **HA Stats Lake** in the store and click **Install**.

## Configure tracked entities

1. Open the add-on **Configuration** tab in HA.
2. Under `tracked_entities`, add the entity IDs you want recorded, e.g.:
   ```yaml
   tracked_entities:
     - sensor.power_consumption
     - sensor.energy_total
     - sensor.temperature_living
     - binary_sensor.door_front
   ```
3. Click **Save** and restart the add-on.

To add or remove a tracked entity later, edit the list in the Configuration tab
and restart the add-on.

### How type/unit/label are determined

| HA attribute                                            | Result                    |
| ------------------------------------------------------- | ------------------------- |
| domain is `binary_sensor`, `switch`, or `input_boolean` | type = `binary`           |
| `state_class` is `total` or `total_increasing`          | type = `counter`          |
| anything else numeric                                   | type = `gauge`            |
| `unit_of_measurement`                                   | used as the unit          |
| `friendly_name`                                         | used as the display label |

Storage key is the entity*id with `.` replaced by `*`(e.g.`sensor.power_consumption`→`sensor_power_consumption`).

## Configuration

| Option                    | Description                                           | Default            |
| ------------------------- | ----------------------------------------------------- | ------------------ |
| `tracked_entities`        | List of HA entity IDs to sample                       | `[sensor.example]` |
| `sample_interval_seconds` | How often to sample (seconds)                         | `1800`             |
| `consolidate_time`        | When to run nightly DuckLake consolidation (HH:MM:SS) | `02:00:00`         |
| `rclone_sync_time`        | When to run nightly rclone sync (HH:MM:SS)            | `03:00:00`         |
| `s3_bucket`               | Bucket name (`ha-stats`) or full URI (`s3://ha-stats/lake/`) | —             |
| `s3_endpoint`             | S3-compatible endpoint URL                            | —                  |
| `s3_key_id`               | S3 Access Key ID                                      | —                  |
| `s3_secret`               | S3 Secret Access Key                                  | —                  |
| `rclone_remote`           | rclone remote path (empty = disable sync)             | —                  |
| `csv_retention_days`      | Delete CSVs older than N days after consolidation (0 = keep forever) | `90` |

CSV data is persisted to `/data/ha_stats_data/` inside the add-on's data volume.

### Disk usage

Each entity generates roughly **1 MB of CSV per year** at the default 30-minute
interval. With the default `csv_retention_days: 90`, CSV files are automatically
deleted after each successful nightly consolidation once they are older than 90
days — the data is already safe in DuckLake on S3 at that point.

If `s3_bucket` is not configured, consolidation never runs and CSVs accumulate
indefinitely. In that case either reduce `sample_interval_seconds` or set
`csv_retention_days` to a low value and accept that old data is discarded.

## (Optional) S3-compatible object store setup

Any S3-compatible store works: Cloudflare R2, AWS S3, MinIO, Backblaze B2, etc.

### Example: Cloudflare R2

1. Create a bucket (e.g. `ha-stats`) in the Cloudflare dashboard under **R2**.
2. Create an API token with **read & write** access. Note the Access Key ID,
   Secret Access Key, and your Account ID.
3. Set:
   - `s3_bucket` → `s3://ha-stats/lake/`
   - `s3_endpoint` → `https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com`
   - `s3_key_id` → your Access Key ID
   - `s3_secret` → your Secret Access Key

### Example: AWS S3

1. Create a bucket and an IAM user with `s3:GetObject`, `s3:PutObject`,
   `s3:ListBucket` permissions.
2. Set:
   - `s3_bucket` → `s3://my-bucket/ha-stats/lake/`
   - `s3_endpoint` → `https://s3.eu-west-1.amazonaws.com` (or leave empty for
     the default AWS endpoint)
   - `s3_key_id` / `s3_secret` → your IAM access key pair

The first nightly run creates the DuckLake catalog and table automatically.

## (Optional) rclone cold backup

Any rclone remote works: OneDrive, Google Drive, Dropbox, SFTP, etc.

1. On any machine, run `rclone config` and create a remote following the
   [rclone docs](https://rclone.org/docs/).
2. Copy `rclone.conf` into the add-on data volume at
   `/data/.config/rclone/rclone.conf`.
3. Set `rclone_remote` to the remote path, e.g.:
   - `onedrive:ha-backup` (OneDrive)
   - `gdrive:ha-backup` (Google Drive)
   - `sftp-nas:/backups/ha` (SFTP)

## Visualizing the data

On any machine with DuckDB:

```bash
duckdb -ui
```

In the SQL console:

```sql
INSTALL ducklake;
LOAD ducklake;

CREATE SECRET s3_store (
    TYPE S3,
    KEY_ID '<your-s3-access-key-id>',
    SECRET '<your-s3-secret-access-key>',
    ENDPOINT '<your-s3-endpoint-without-https>',
    REGION 'auto'
);

-- catalog.duckdb lives in the add-on data volume; copy it locally first:
-- docker cp <addon_container>:/data/ha_stats_data/catalog.duckdb ./catalog.duckdb
ATTACH 'ducklake:catalog.duckdb' AS lake (
    DATA_PATH 's3://your-bucket/lake/data/'
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
  `N` is 0, verify `tracked_entities` is set in the Configuration tab.
- **Consolidation errors** — usually S3 credentials or bucket path. Test the
  `ATTACH` statement manually in a local `duckdb` shell first.
- **rclone errors** — verify `rclone.conf` is at
  `/data/.config/rclone/rclone.conf` and the remote name matches `rclone_remote`.
