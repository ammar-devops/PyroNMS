# PyroNMS Deployment Guide

## Infrastructure

| Component | Location | Port |
|-----------|----------|------|
| API server | `/opt/ont-monitor/api/server.py` | `8088` |
| Web frontend | `/var/www/html/index.html` | `80` (via nginx) |
| InfluxDB | Docker container `influxdb` | `8086` |
| GenieACS CWMP | systemd `genieacs-cwmp` | `7547` |
| GenieACS NBI | systemd `genieacs-nbi` | `7557` |
| GenieACS UI | systemd `genieacs-ui` | `3000` |
| Grafana | Docker container `pyronms_grafana` | `4000` |
| SNMP data | SQLite at `/opt/pyronms/data/ont_map.db` | — |

## Services

```bash
# Check all PyroNMS services
systemctl status ont-api ont-worker@1 ont-worker@2 ont-worker@4 ont-worker@5 nginx

# Restart API
systemctl restart ont-api

# Check API logs
journalctl -u ont-api -f

# Check slot 1 worker logs
journalctl -u ont-worker@1 -f

# Restart a worker
systemctl restart ont-worker@5
```

## Service Files

| File | Path |
|------|------|
| API service | `/etc/systemd/system/ont-api.service` |
| Worker template | `/etc/systemd/system/ont-worker@.service` |

After editing service files:
```bash
systemctl daemon-reload
systemctl restart ont-api
```

## Config

Main config: `/opt/ont-monitor/config/config.py`

```python
OLT_HOST    = "172.20.101.101"    # Huawei MA5603T
OLT_PORT    = 22
POLL_INTERVAL = 7200              # Worker poll interval (seconds)
API_PORT    = 8088
```

Slot-specific users (`SLOT_USERS`) are defined per slot to allow independent SSH sessions per worker.

## Deployment Steps

To deploy updated files:
```bash
# From Windows dev machine (PuTTY tools):
pscp -pw "PASS" api/server.py      root@172.20.101.160:/opt/ont-monitor/api/server.py
pscp -pw "PASS" api/olt_helpers.py root@172.20.101.160:/opt/ont-monitor/api/olt_helpers.py
pscp -pw "PASS" web/index.html     root@172.20.101.160:/var/www/html/index.html

# Then restart API:
plink -pw "PASS" root@172.20.101.160 "systemctl restart ont-api"
```

## Database

InfluxDB holds:
- `ont_status` — ONT online/offline state, poll timestamps
- `ont_optical` — RX power (dBm), temperature per ONT
- `pon_traffic` — RX/TX Mbps per PON port (SNMP)
- `olt_temperature` — OLT slot temperatures

SQLite (`ont_map.db`) holds SNMP-discovered ONT → FSP mapping used by:
- `/ont/traffic` → FSP lookup for `pon_traffic` query
- `/ont/snmp` → ONT SNMP cache lookup

## Backup

Backups are stored at `/opt/backups/pyronms-final-YYYYMMDD-HHMM.tar.gz`.

To create a new backup:
```bash
STAMP=$(date +%Y%m%d-%H%M)
BDIR="/opt/backups/pyronms-final-${STAMP}"
mkdir -p "$BDIR"
cp -r /opt/ont-monitor/ "$BDIR/ont-monitor/"
cp -r /opt/pyronms/     "$BDIR/pyronms/"
cp -r /var/www/html/    "$BDIR/www-html/"
cp /etc/systemd/system/ont-api.service "$BDIR/"
cd /opt/backups && tar czf "${BNAME}.tar.gz" "${BNAME}/"
```

## Known Limitations

- **No HTTPS** — nginx serves on port 80 only. Add Let's Encrypt or self-signed cert + nginx SSL config for production HTTPS.
- **Firewall open** — iptables INPUT chain has no rules (policy ACCEPT). Restrict ports 8088 and 8086 to LAN-only access.
- **Secrets in config** — OLT SSH passwords and SNMP communities are in plaintext `config.py`. Do not commit this file with real credentials to public repos.
- **Single-node** — no HA, no replica. If VM goes down, monitoring stops.
