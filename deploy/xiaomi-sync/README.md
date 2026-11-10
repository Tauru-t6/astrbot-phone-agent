# xiaomi-sync deployment

This directory contains the server-side timer wrapper for
[`xiaomi-health-sync`](https://github.com/ridd1ot/xiaomi-health-sync).
It provides the SQLite health-data source and remains available alongside the
optional [Health Connect source](../../docs/HEALTH_CONNECT.md) in plugin 0.9.2
and App 0.3.27. An independent Android Health Bridge is not required.

Set `health_data_source=health_db` to always query this database, or use `auto`
to fall back to it when the current phone has no fresh Health Connect summary.
The plugin selects one source per query and never adds their metrics together.
Enabling Health Connect does not stop this timer.

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
