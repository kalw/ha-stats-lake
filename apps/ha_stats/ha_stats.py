"""
ha_stats — long-term storage for Home Assistant sensor data.

Samples entities listed in a Group helper every 30 minutes, appends them
to per-entity monthly CSV files, then nightly:
  - consolidates new rows into a DuckLake (Parquet) on Cloudflare R2
  - syncs raw CSVs to OneDrive (via rclone) for cold backup

See README.md for setup instructions.
"""

import csv
import datetime
import subprocess
from pathlib import Path
from datetime import timezone

import adbase as ad


class HaStats(ad.ADBase):

    def initialize(self):
        self.adapi = self.get_ad_api()

        self.csv_dir = Path(self.args["csv_dir"])
        self.csv_dir.mkdir(parents=True, exist_ok=True)

        self.group_entity = self.args.get(
            "group_entity", "group.ha_stats_tracked_entities"
        )
        self.sample_interval = int(self.args.get("sample_interval_seconds", 1800))

        self.adapi.run_every(self.sample, "now", self.sample_interval)
        self.adapi.run_daily(self.consolidate, self.args.get("consolidate_time", "02:00:00"))
        self.adapi.run_daily(self.sync_onedrive, self.args.get("onedrive_sync_time", "03:00:00"))

        self.adapi.log(
            f"ha_stats initialized: csv_dir={self.csv_dir}, "
            f"group={self.group_entity}, interval={self.sample_interval}s"
        )

    # ── entity discovery via group helper ──────────────────────────────────

    def tracked_entities(self) -> list[dict]:
        """Resolve the group helper into a list of entities with inferred metadata."""
        group = self.adapi.get_state(self.group_entity, attribute="all")
        if not group:
            self.adapi.log(f"{self.group_entity} not found", level="WARNING")
            return []

        entity_ids = group.get("attributes", {}).get("entity_id", [])
        result = []
        for entity_id in entity_ids:
            state = self.adapi.get_state(entity_id, attribute="all")
            if not state:
                self.adapi.log(f"could not read state for {entity_id}", level="WARNING")
                continue

            a = state.get("attributes", {})
            domain = entity_id.split(".")[0]
            state_class = a.get("state_class", "")

            if domain in ("binary_sensor", "switch", "input_boolean"):
                etype = "binary"
            elif state_class in ("total", "total_increasing"):
                etype = "counter"
            else:
                etype = "gauge"

            result.append({
                "key": entity_id.replace(".", "_"),
                "ha_entity": entity_id,
                "type": etype,
                "unit": a.get("unit_of_measurement", ""),
                "label": a.get("friendly_name", entity_id),
            })
        return result

    # ── CSV append ────────────────────────────────────────────────────────

    def month_file(self, key: str, ts: datetime.datetime) -> Path:
        d = self.csv_dir / key
        d.mkdir(exist_ok=True)
        return d / ts.strftime("%Y-%m.csv")

    def sample(self, kwargs):
        ts = datetime.datetime.now(timezone.utc)
        entities = self.tracked_entities()
        if not entities:
            self.adapi.log("no tracked entities found, skipping sample", level="WARNING")
            return

        for meta in entities:
            raw = self.adapi.get_state(meta["ha_entity"])
            try:
                if meta["type"] == "binary":
                    value = 1 if str(raw).lower() in ("on", "true", "1") else 0
                else:
                    value = float(raw)
            except (TypeError, ValueError):
                self.adapi.log(
                    f"skipping {meta['ha_entity']}: unparseable value {raw!r}",
                    level="WARNING",
                )
                continue

            path = self.month_file(meta["key"], ts)
            write_header = not path.exists()
            with path.open("a", newline="") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(["ts", "value"])
                w.writerow([ts.isoformat(), value])

        self.adapi.log(f"sampled {len(entities)} entities")

    # ── DuckDB consolidation -> DuckLake on R2 ──────────────────────────────

    def consolidate(self, kwargs):
        if not self.args.get("r2_bucket"):
            self.adapi.log("r2_bucket not configured, skipping consolidation")
            return

        try:
            import duckdb
        except ImportError:
            self.adapi.log(
                "duckdb python package not installed, skipping consolidation",
                level="ERROR",
            )
            return

        r2 = self.args
        sql = f"""
INSTALL ducklake;
LOAD ducklake;

CREATE OR REPLACE SECRET r2 (
    TYPE S3,
    KEY_ID     '{r2["r2_key_id"]}',
    SECRET     '{r2["r2_secret"]}',
    ENDPOINT   '{r2["r2_endpoint"].replace("https://", "")}',
    REGION     'auto'
);

ATTACH IF NOT EXISTS 'ducklake:{r2["r2_bucket"]}catalog.duckdb' AS lake (
    DATA_PATH '{r2["r2_bucket"]}data/'
);

CREATE TABLE IF NOT EXISTS lake.stats (
    entity  VARCHAR,
    ts      TIMESTAMPTZ,
    value   DOUBLE
);

INSERT INTO lake.stats
SELECT
    regexp_extract(filename, '.*/([^/]+)/\\d{{4}}-\\d{{2}}\\.csv$', 1) AS entity,
    ts::TIMESTAMPTZ AS ts,
    value::DOUBLE   AS value
FROM read_csv(
    '{self.csv_dir}/*/*.csv',
    columns = {{'ts': 'VARCHAR', 'value': 'VARCHAR'}},
    filename = true
)
WHERE ts::TIMESTAMPTZ > (
    SELECT coalesce(max(ts), '1970-01-01'::TIMESTAMPTZ) FROM lake.stats
);
"""
        try:
            duckdb.execute(sql)
            self.adapi.log("R2 / DuckLake consolidation done")
        except Exception as e:
            self.adapi.log(f"consolidation failed: {e}", level="ERROR")

    # ── rclone sync -> OneDrive (cold backup) ───────────────────────────────

    def sync_onedrive(self, kwargs):
        remote = self.args.get("onedrive_remote")
        if not remote:
            return
        try:
            r = subprocess.run(
                ["rclone", "sync", str(self.csv_dir), remote],
                capture_output=True, text=True, timeout=300,
            )
            if r.returncode != 0:
                self.adapi.log(f"rclone error: {r.stderr}", level="ERROR")
            else:
                self.adapi.log("OneDrive sync done")
        except FileNotFoundError:
            self.adapi.log("rclone binary not found, skipping OneDrive sync", level="ERROR")
        except Exception as e:
            self.adapi.log(f"rclone failed: {e}", level="ERROR")
