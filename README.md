# HA Stats Lake — Home Assistant add-on repository

Long-term storage for Home Assistant sensor data — samples entities to
per-entity monthly CSVs, consolidates nightly into a DuckLake (Parquet)
on Cloudflare R2, and optionally syncs raw CSVs via rclone.

Add-on documentation: <https://github.com/kalw/ha-stats-lake/blob/main/ha_stats/DOCS.md>

Stable channel

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fkalw%2Fhassio-addons)

## Add-ons

This repository contains the following add-ons

### [HA Stats Lake](./ha_stats)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

Long-term storage for Home Assistant sensor data via DuckLake on Cloudflare R2.

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
