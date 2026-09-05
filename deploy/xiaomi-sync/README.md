# xiaomi-sync deployment

This directory contains the server-side timer wrapper for
[`xiaomi-health-sync`](https://github.com/ridd1ot/xiaomi-health-sync).
It is the only health-data ingestion path used by the AstrBot phone plugin.
Android Health Bridge is not required for health monitoring.

The expected server layout is:

```text
/home/tauru/data/xiaomi_health_sync/
  .venv/bin/python
  data/token.json
  data/health.db
  sync-health.sh
```

`sync-health.sh` runs `health_vault.cli sync`, keeps the Xiaomi token file at
mode `600`, and uses `HEALTH_SYNC_DAYS` (default `7`) to control the lookback.
The supplied user systemd timer runs it every 30 minutes and catches up after
a reboot.

Install the units for the account that owns AstrBot:

```bash
mkdir -p ~/.config/systemd/user
cp sync-health.sh ~/data/xiaomi_health_sync/
cp xiaomi-health-sync.service xiaomi-health-sync.timer ~/.config/systemd/user/
chmod 700 ~/data/xiaomi_health_sync/sync-health.sh
systemctl --user daemon-reload
systemctl --user enable --now xiaomi-health-sync.timer
systemctl --user start xiaomi-health-sync.service
```

Set the AstrBot plugin `health_db_path` to the full path of `data/health.db`.
The plugin opens the database read-only, reports the most recent sync run, and
marks data stale when the last successful run is more than three hours old.
Credentials stay in `data/token.json`; do not put them in AstrBot config,
Git, or chat messages.
