"""
ha_stats — long-term storage for Home Assistant sensor data.

Runs as a Home Assistant add-on. Talks to HA via the Supervisor REST API
using the SUPERVISOR_TOKEN injected by the add-on runtime.

Config is read from /data/options.json (written by the HA UI from config.yaml schema).
Data is persisted to /data/ha_stats_data (the add-on's /data volume).
"""

import asyncio
import csv
import datetime
import json
import logging
import os
import subprocess
from datetime import timezone
from pathlib import Path

import aiohttp

log = logging.getLogger("ha_stats")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CONFIG_PATH = Path("/data/options.json")
DATA_DIR = Path("/data/ha_stats_data")
HA_URL = "http://supervisor/core"


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


class HaStats:

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.csv_dir = DATA_DIR
        self.csv_dir.mkdir(parents=True, exist_ok=True)

        self.group_entity: str = cfg.get("group_entity", "group.ha_stats_tracked_entities")
        self.sample_interval: int = int(cfg.get("sample_interval_seconds", 1800))
        self.consolidate_time: str = cfg.get("consolidate_time", "02:00:00")
        self.rclone_sync_time: str = cfg.get("rclone_sync_time", "03:00:00")

        self._token = os.environ["SUPERVISOR_TOKEN"]
        self._headers = {"Authorization": f"Bearer {self._token}"}

    # ── HA REST API helpers ────────────────────────────────────────────────

    async def _get_state(self, session: aiohttp.ClientSession, entity_id: str) -> dict | None:
        url = f"{HA_URL}/api/states/{entity_id}"
        async with session.get(url, headers=self._headers) as resp:
            if resp.status != 200:
                return None
            return await resp.json()

    # ── entity discovery via group helper ─────────────────────────────────

    async def tracked_entities(self, session: aiohttp.ClientSession) -> list[dict]:
        group = await self._get_state(session, self.group_entity)
        if not group:
            log.warning("%s not found", self.group_entity)
            return []

        entity_ids: list[str] = group.get("attributes", {}).get("entity_id", [])
        result = []
        for entity_id in entity_ids:
            state = await self._get_state(session, entity_id)
            if not state:
                log.warning("could not read state for %s", entity_id)
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

    # ── CSV append ─────────────────────────────────────────────────────────

    def _month_file(self, key: str, ts: datetime.datetime) -> Path:
        d = self.csv_dir / key
        d.mkdir(exist_ok=True)
        return d / ts.strftime("%Y-%m.csv")

    async def sample(self) -> None:
        async with aiohttp.ClientSession() as session:
            ts = datetime.datetime.now(timezone.utc)
            entities = await self.tracked_entities(session)
            if not entities:
                log.warning("no tracked entities found, skipping sample")
                return

            for meta in entities:
                state = await self._get_state(session, meta["ha_entity"])
                raw = state.get("state") if state else None
                try:
                    if meta["type"] == "binary":
                        value: float = 1.0 if str(raw).lower() in ("on", "true", "1") else 0.0
                    else:
                        value = float(raw)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    log.warning("skipping %s: unparseable value %r", meta["ha_entity"], raw)
                    continue

                path = self._month_file(meta["key"], ts)
                write_header = not path.exists()
                with path.open("a", newline="") as f:
                    w = csv.writer(f)
                    if write_header:
                        w.writerow(["ts", "value"])
                    w.writerow([ts.isoformat(), value])

            log.info("sampled %d entities", len(entities))

    # ── DuckDB consolidation → DuckLake on S3-compatible store ────────────

    def consolidate(self) -> None:
        if not self.cfg.get("s3_bucket"):
            log.info("s3_bucket not configured, skipping consolidation")
            return
        try:
            import duckdb
        except ImportError:
            log.error("duckdb not installed, skipping consolidation")
            return

        cfg = self.cfg
        # Strip https:// from endpoint — DuckDB expects bare hostname
        endpoint = cfg["s3_endpoint"].replace("https://", "").replace("http://", "")
        sql = f"""
INSTALL ducklake;
LOAD ducklake;

CREATE OR REPLACE SECRET s3_store (
    TYPE S3,
    KEY_ID     '{cfg["s3_key_id"]}',
    SECRET     '{cfg["s3_secret"]}',
    ENDPOINT   '{endpoint}',
    REGION     'auto'
);

ATTACH IF NOT EXISTS 'ducklake:{cfg["s3_bucket"]}catalog.duckdb' AS lake (
    DATA_PATH '{cfg["s3_bucket"]}data/'
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
            log.info("S3 / DuckLake consolidation done")
        except Exception as e:
            log.error("consolidation failed: %s", e)

    # ── rclone sync → any remote (cold backup) ────────────────────────────

    def sync_rclone(self) -> None:
        remote = self.cfg.get("rclone_remote")
        if not remote:
            return
        try:
            r = subprocess.run(
                ["rclone", "sync", str(self.csv_dir), remote],
                capture_output=True, text=True, timeout=300,
            )
            if r.returncode != 0:
                log.error("rclone error: %s", r.stderr)
            else:
                log.info("rclone sync done → %s", remote)
        except FileNotFoundError:
            log.error("rclone binary not found, skipping sync")
        except Exception as e:
            log.error("rclone failed: %s", e)

    # ── scheduling ─────────────────────────────────────────────────────────

    def _seconds_until(self, time_str: str) -> float:
        """Seconds until the next wall-clock occurrence of HH:MM:SS (local time)."""
        h, m, s = map(int, time_str.split(":"))
        now = datetime.datetime.now()
        target = now.replace(hour=h, minute=m, second=s, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return (target - now).total_seconds()

    async def _run_sampler(self) -> None:
        while True:
            await self.sample()
            await asyncio.sleep(self.sample_interval)

    async def _run_consolidator(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self._seconds_until(self.consolidate_time))
            await loop.run_in_executor(None, self.consolidate)

    async def _run_rclone(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self._seconds_until(self.rclone_sync_time))
            await loop.run_in_executor(None, self.sync_rclone)

    async def run(self) -> None:
        log.info(
            "ha_stats starting: csv_dir=%s, group=%s, interval=%ds",
            self.csv_dir, self.group_entity, self.sample_interval,
        )
        await asyncio.gather(
            self._run_sampler(),
            self._run_consolidator(),
            self._run_rclone(),
        )


if __name__ == "__main__":
    cfg = load_config()
    asyncio.run(HaStats(cfg).run())
