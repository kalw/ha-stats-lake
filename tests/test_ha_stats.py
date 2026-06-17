import datetime
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ha_stats"))
import ha_stats as hs  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────


def _make_csv(base: Path, entity: str, year: int, month: int) -> Path:
    d = base / entity
    d.mkdir(exist_ok=True)
    p = d / f"{year:04d}-{month:02d}.csv"
    p.write_text("ts,value\n")
    return p


@pytest.fixture
def instance(tmp_path):
    cfg = {
        "tracked_entities": [],
        "sample_interval_seconds": 1800,
        "consolidate_time": "02:00:00",
        "rclone_sync_time": "03:00:00",
        "s3_bucket": "",
        "s3_endpoint": "",
        "s3_key_id": "",
        "s3_secret": "",
        "rclone_remote": "",
        "csv_retention_days": 90,
    }
    with patch("ha_stats.DATA_DIR", tmp_path), patch.dict(
        os.environ, {"SUPERVISOR_TOKEN": "test-token"}
    ):
        obj = hs.HaStats(cfg)
    return obj


# ── load_config ────────────────────────────────────────────────────────────


def test_load_config_reads_json(tmp_path):
    f = tmp_path / "options.json"
    f.write_text(json.dumps({"s3_bucket": "my-bucket", "sample_interval_seconds": 1800}))
    cfg = hs.load_config(f)
    assert cfg["s3_bucket"] == "my-bucket"
    assert cfg["sample_interval_seconds"] == 1800


def test_load_config_env_overrides_string(tmp_path, monkeypatch):
    f = tmp_path / "options.json"
    f.write_text(json.dumps({"s3_bucket": "original"}))
    monkeypatch.setenv("HA_STATS_S3_BUCKET", "overridden")
    assert hs.load_config(f)["s3_bucket"] == "overridden"


def test_load_config_env_tracked_entities(tmp_path, monkeypatch):
    f = tmp_path / "options.json"
    f.write_text(json.dumps({"tracked_entities": []}))
    monkeypatch.setenv("HA_STATS_TRACKED_ENTITIES", "sensor.a, sensor.b , sensor.c")
    assert hs.load_config(f)["tracked_entities"] == ["sensor.a", "sensor.b", "sensor.c"]


def test_load_config_env_sample_interval_coerced(tmp_path, monkeypatch):
    f = tmp_path / "options.json"
    f.write_text(json.dumps({"sample_interval_seconds": 1800}))
    monkeypatch.setenv("HA_STATS_SAMPLE_INTERVAL", "600")
    result = hs.load_config(f)["sample_interval_seconds"]
    assert result == 600
    assert isinstance(result, int)


def test_load_config_env_not_set_uses_file(tmp_path, monkeypatch):
    f = tmp_path / "options.json"
    f.write_text(json.dumps({"s3_bucket": "file-value"}))
    monkeypatch.delenv("HA_STATS_S3_BUCKET", raising=False)
    assert hs.load_config(f)["s3_bucket"] == "file-value"


# ── _normalise_bucket ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ha-stats", "s3://ha-stats/"),
        ("ha-stats/lake", "s3://ha-stats/lake/"),
        ("s3://ha-stats/lake/", "s3://ha-stats/lake/"),
        ("s3://ha-stats/lake", "s3://ha-stats/lake/"),
        ("  ha-stats  ", "s3://ha-stats/"),
    ],
)
def test_normalise_bucket(raw, expected):
    assert hs._normalise_bucket(raw) == expected


# ── _entity_type ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "entity_id, attributes, expected",
    [
        ("binary_sensor.door", {}, "binary"),
        ("switch.light", {}, "binary"),
        ("input_boolean.flag", {}, "binary"),
        ("sensor.energy", {"state_class": "total"}, "counter"),
        ("sensor.energy", {"state_class": "total_increasing"}, "counter"),
        ("sensor.temperature", {"state_class": "measurement"}, "gauge"),
        ("sensor.temperature", {}, "gauge"),
        ("sensor.humidity", {"state_class": ""}, "gauge"),
    ],
)
def test_entity_type(entity_id, attributes, expected):
    assert hs._entity_type(entity_id, attributes) == expected


# ── _cleanup_old_csvs ──────────────────────────────────────────────────────


def test_cleanup_removes_old_files(instance, tmp_path):
    today = datetime.date.today()
    old = today - datetime.timedelta(days=200)
    recent = today - datetime.timedelta(days=30)
    old_file = _make_csv(tmp_path, "sensor_temp", old.year, old.month)
    recent_file = _make_csv(tmp_path, "sensor_temp", recent.year, recent.month)
    instance.cfg["csv_retention_days"] = 90
    instance._cleanup_old_csvs()
    assert not old_file.exists()
    assert recent_file.exists()


def test_cleanup_zero_retention_keeps_all(instance, tmp_path):
    today = datetime.date.today()
    old = today - datetime.timedelta(days=500)
    old_file = _make_csv(tmp_path, "sensor_temp", old.year, old.month)
    instance.cfg["csv_retention_days"] = 0
    instance._cleanup_old_csvs()
    assert old_file.exists()


def test_cleanup_keeps_current_month(instance, tmp_path):
    today = datetime.date.today()
    current_file = _make_csv(tmp_path, "sensor_temp", today.year, today.month)
    instance.cfg["csv_retention_days"] = 1
    instance._cleanup_old_csvs()
    assert current_file.exists()


def test_cleanup_ignores_non_csv_filenames(instance, tmp_path):
    d = tmp_path / "sensor_temp"
    d.mkdir()
    weird = d / "notes.txt"
    weird.write_text("hi")
    instance.cfg["csv_retention_days"] = 1
    instance._cleanup_old_csvs()
    assert weird.exists()


# ── CLI argument parsing ───────────────────────────────────────────────────


def test_parse_args_no_subcommand():
    with patch("sys.argv", ["ha_stats.py"]):
        args = hs._parse_args()
    assert args.cmd is None


def test_parse_args_sample():
    with patch("sys.argv", ["ha_stats.py", "sample"]):
        assert hs._parse_args().cmd == "sample"


def test_parse_args_consolidate():
    with patch("sys.argv", ["ha_stats.py", "consolidate"]):
        assert hs._parse_args().cmd == "consolidate"


def test_parse_args_export_flags():
    with patch(
        "sys.argv",
        ["ha_stats.py", "export", "--bucket", "s3://b/", "--key-id", "k", "--secret", "s"],
    ):
        args = hs._parse_args()
    assert args.cmd == "export"
    assert args.bucket == "s3://b/"
    assert args.key_id == "k"
    assert args.secret == "s"


def test_parse_args_sync_remote():
    with patch("sys.argv", ["ha_stats.py", "sync", "--remote", "onedrive:backup"]):
        args = hs._parse_args()
    assert args.cmd == "sync"
    assert args.remote == "onedrive:backup"


def test_parse_args_global_config():
    with patch("sys.argv", ["ha_stats.py", "--config", "/tmp/cfg.json", "consolidate"]):
        args = hs._parse_args()
    assert args.config == "/tmp/cfg.json"
    assert args.cmd == "consolidate"


# ── tracked_entities (mocked HA API) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_tracked_entities_returns_classified_entities(instance, tmp_path):
    instance.tracked_entity_ids = ["sensor.power", "binary_sensor.door"]

    async def fake_get_state(session, entity_id):
        return {
            "state": "42",
            "attributes": {
                "state_class": "measurement",
                "unit_of_measurement": "W",
                "friendly_name": entity_id,
            },
        }

    with patch.object(instance, "_get_state", side_effect=fake_get_state):
        result = await instance.tracked_entities(None)

    assert len(result) == 2
    assert result[0]["type"] == "gauge"
    assert result[1]["type"] == "binary"


@pytest.mark.asyncio
async def test_tracked_entities_skips_missing(instance):
    instance.tracked_entity_ids = ["sensor.missing", "sensor.present"]

    async def fake_get_state(session, entity_id):
        if entity_id == "sensor.missing":
            return None
        return {"state": "1", "attributes": {}}

    with patch.object(instance, "_get_state", side_effect=fake_get_state):
        result = await instance.tracked_entities(None)

    assert len(result) == 1
    assert result[0]["ha_entity"] == "sensor.present"


@pytest.mark.asyncio
async def test_tracked_entities_empty_list(instance):
    instance.tracked_entity_ids = []
    result = await instance.tracked_entities(None)
    assert result == []
