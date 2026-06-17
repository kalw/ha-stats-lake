"""
ha_stats — long-term storage for Home Assistant sensor data.

Runs as a Home Assistant add-on. Talks to HA via the Supervisor REST API
using the SUPERVISOR_TOKEN injected by the add-on runtime.

Config is read from /data/options.json (written by the HA UI from config.yaml schema).
Data is persisted to /data/ha_stats_data (the add-on's /data volume).
"""

import argparse
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

# Environment variable → config key mapping (HA_STATS_* overrides options.json)
_ENV_MAP = {
    "HA_STATS_S3_BUCKET": "s3_bucket",
    "HA_STATS_S3_ENDPOINT": "s3_endpoint",
    "HA_STATS_S3_KEY_ID": "s3_key_id",
    "HA_STATS_S3_SECRET": "s3_secret",
    "HA_STATS_RCLONE_REMOTE": "rclone_remote",
    "HA_STATS_SAMPLE_INTERVAL": "sample_interval_seconds",
    "HA_STATS_CONSOLIDATE_TIME": "consolidate_time",
    "HA_STATS_RCLONE_SYNC_TIME": "rclone_sync_time",
    "HA_STATS_CSV_RETENTION_DAYS": "csv_retention_days",
    # comma-separated list: HA_STATS_TRACKED_ENTITIES=sensor.a,sensor.b
    "HA_STATS_TRACKED_ENTITIES": "tracked_entities",
}


def load_config(path: Path | None = None) -> dict:
    cfg = json.loads((path or CONFIG_PATH).read_text())
    for env_key, cfg_key in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        if cfg_key == "tracked_entities":
            cfg[cfg_key] = [e.strip() for e in val.split(",") if e.strip()]
        elif cfg_key == "sample_interval_seconds":
            cfg[cfg_key] = int(val)
        else:
            cfg[cfg_key] = val
    return cfg


class HaStats:

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.csv_dir = DATA_DIR
        self.csv_dir.mkdir(parents=True, exist_ok=True)

        self.tracked_entity_ids: list[str] = cfg.get("tracked_entities", [])
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

    # ── entity metadata from config list ──────────────────────────────────

    async def tracked_entities(self, session: aiohttp.ClientSession) -> list[dict]:
        if not self.tracked_entity_ids:
            log.warning("tracked_entities is empty — add entity IDs in the add-on configuration")
            return []

        result = []
        for entity_id in self.tracked_entity_ids:
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
        # Normalise bucket: accept bare name ("ha-stats"), name+path, or full URI
        bucket = cfg["s3_bucket"].strip()
        if not bucket.startswith("s3://"):
            bucket = f"s3://{bucket}/"
        if not bucket.endswith("/"):
            bucket += "/"
        # Strip https:// from endpoint — DuckDB expects bare hostname
        endpoint = cfg["s3_endpoint"].replace("https://", "").replace("http://", "")
        try:
            con = duckdb.connect()
            con.execute("INSTALL ducklake")
            con.execute("LOAD ducklake")
            con.execute(f"""
                CREATE OR REPLACE SECRET s3_store (
                    TYPE S3,
                    KEY_ID   '{cfg["s3_key_id"]}',
                    SECRET   '{cfg["s3_secret"]}',
                    ENDPOINT '{endpoint}',
                    REGION   'auto'
                )
            """)
            catalog = self.csv_dir / "catalog.duckdb"
            con.execute(f"""
                ATTACH IF NOT EXISTS 'ducklake:{catalog}' AS lake (
                    DATA_PATH '{bucket}data/'
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS lake.stats (
                    entity  VARCHAR,
                    ts      TIMESTAMPTZ,
                    value   DOUBLE
                )
            """)
            con.execute(f"""
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
                )
            """)
            con.execute("CHECKPOINT lake")
            con.close()
            log.info("S3 / DuckLake consolidation done → %s", bucket)
            self._cleanup_old_csvs()
        except Exception as e:
            log.error("consolidation failed: %s", e)

    def _cleanup_old_csvs(self) -> None:
        retention = int(self.cfg.get("csv_retention_days", 90))
        if retention == 0:
            return
        cutoff = datetime.date.today() - datetime.timedelta(days=retention)
        cutoff_ym = (cutoff.year, cutoff.month)
        removed = 0
        for csv_file in self.csv_dir.glob("*/*.csv"):
            try:
                y, m = map(int, csv_file.stem.split("-"))
            except ValueError:
                continue
            if (y, m) < cutoff_ym:
                csv_file.unlink()
                removed += 1
        if removed:
            log.info("removed %d CSV files older than %d days", removed, retention)

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
            "ha_stats starting: csv_dir=%s, entities=%d, interval=%ds",
            self.csv_dir, len(self.tracked_entity_ids), self.sample_interval,
        )
        await asyncio.gather(
            self._run_sampler(),
            self._run_consolidator(),
            self._run_rclone(),
        )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="HA Stats Lake add-on")
    p.add_argument(
        "--config",
        metavar="PATH",
        help="Path to options JSON (default: /data/options.json)",
    )
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="Start the full loop (default)")
    sub.add_parser("sample", help="Take one sample of all tracked entities and exit")
    sub.add_parser("consolidate", help="Run DuckLake consolidation once and exit")

    sync_p = sub.add_parser("sync", help="Run rclone sync once and exit")
    sync_p.add_argument("--remote", help="rclone remote path (overrides config)")

    exp = sub.add_parser("export", help="Export CSV data to S3/DuckLake")
    exp.add_argument("--bucket", help="S3 bucket URI (e.g. s3://my-bucket/lake/)")
    exp.add_argument("--endpoint", help="S3-compatible endpoint URL")
    exp.add_argument("--key-id", dest="key_id", help="S3 Access Key ID")
    exp.add_argument("--secret", help="S3 Secret Access Key")

    return p.parse_args()


def _cli_overrides(args: argparse.Namespace) -> dict:
    """Collect non-None CLI flags into a config patch dict."""
    candidates: dict = {}
    if args.cmd == "export":
        candidates = {
            "s3_bucket": args.bucket,
            "s3_endpoint": args.endpoint,
            "s3_key_id": args.key_id,
            "s3_secret": args.secret,
        }
    elif args.cmd == "sync":
        candidates = {"rclone_remote": args.remote}
    return {k: v for k, v in candidates.items() if v is not None}


if __name__ == "__main__":
    args = _parse_args()
    cfg_path = Path(args.config) if args.config else None
    # Priority: CLI flags > HA_STATS_* env vars > options.json
    cfg = {**load_config(cfg_path), **_cli_overrides(args)}
    ha = HaStats(cfg)

    if args.cmd == "sample":
        asyncio.run(ha.sample())
    elif args.cmd in ("consolidate", "export"):
        ha.consolidate()
    elif args.cmd == "sync":
        ha.sync_rclone()
    else:
        asyncio.run(ha.run())
